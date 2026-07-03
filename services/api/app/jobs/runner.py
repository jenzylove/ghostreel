"""Async job runner: resumable state machine + startup resume scan.

Two modes:
- TTS  : generate script -> per-segment image + generated voiceover -> assemble.
- BYO  : transcribe the uploaded recording -> beats + word timings -> per-segment image
         (no voiceover) -> assemble images over the ORIGINAL audio with karaoke captions.

Each job's state is persisted to B2 after every step and steps are skipped when their output
already exists, so a crash/restart resumes from the last completed segment.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.jobs import store
from app.models import JobStatus, Script, Segment
from app.pipeline.assemble import assemble_byo, assemble_video
from app.pipeline.script import generate_script, visuals_for_narration
from app.pipeline.style import DEFAULT_STYLE
from app.pipeline.transcribe import segment_words, transcribe_url
from app.pipeline.visuals import generate_and_qa
from app.pipeline.voice import generate_voice
from app.storage.b2 import backend

logger = logging.getLogger("ghostreel.runner")
_executor = ThreadPoolExecutor(max_workers=2)


def submit(job_id: str) -> None:
    _executor.submit(_safe_run, job_id)


def _safe_run(job_id: str) -> None:
    try:
        run_job(job_id)
    except Exception:
        logger.exception("job %s crashed", job_id)


def run_job(job_id: str) -> None:
    job = store.load(job_id)
    preset = job.style or DEFAULT_STYLE
    voice_id = job.voice_id or settings.voice_id
    byo = job.voice_mode == "byo"
    job.status = JobStatus.RUNNING
    job.error = None
    store.save(job)

    try:
        # 1. Build the script/segments (skip if already built).
        if job.script is None:
            if byo:
                job.step = "transcribe"
                store.save(job)
                audio_url = backend().presigned_get_url(job.audio_key, expires_in=3600)
                result = transcribe_url(audio_url)
                job.word_timings = result["words"]
                beats = segment_words(result["words"], job.segment_count)
                narrations = [b["text"] for b in beats]
                visuals = visuals_for_narration(narrations, job.topic)
                job.script = Script(
                    topic=job.topic or "",
                    segments=[
                        Segment(index=i, narration=narrations[i], visual=visuals[i],
                                duration_s=beats[i]["end"] - beats[i]["start"])
                        for i in range(len(beats))
                    ],
                )
            else:
                job.step = "script"
                store.save(job)
                job.script = generate_script(job.topic, n=job.segment_count)
            store.save(job)

        # 1b. Review gate: pause for human approval/edit before spending on images.
        if job.review and not job.approved:
            job.status = JobStatus.AWAITING_REVIEW
            job.step = "awaiting review"
            store.save(job)
            return

        total = len(job.script.segments)

        # 2. Per segment: image (QA + retry) then, for TTS only, the voiceover.
        for seg in job.script.segments:
            if not seg.image_url:
                job.step = f"image {seg.index + 1}/{total}"
                store.save(job)
                seg.image_prompt = preset.apply(seg.visual)
                seg.image_url, seg.attempts = generate_and_qa(seg.image_prompt, seg, preset)
                store.save(job)
            if not byo and not seg.audio_url:
                job.step = f"voice {seg.index + 1}/{total}"
                store.save(job)
                seg.audio_url = generate_voice(seg.narration, voice_id=voice_id)
                store.save(job)

        # 3. Assemble (skip if already done).
        if not job.video_url:
            job.step = "assemble"
            store.save(job)
            if byo:
                audio_bytes = backend().get(job.audio_key)
                job.video_url = assemble_byo(job.script, audio_bytes, job.word_timings, captions=job.captions)
            else:
                job.video_url = assemble_video(job.script, captions=job.captions)
            store.save(job)

        job.retries = sum(max(0, len(s.attempts) - 1) for s in job.script.segments)
        job.status = JobStatus.DONE
        job.step = "done"
        store.save(job)
    except Exception as e:  # noqa: BLE001 - record failure in the durable job record
        job.status = JobStatus.FAILED
        job.error = f"{type(e).__name__}: {e}"
        store.save(job)
        raise


def resume_pending() -> None:
    """On startup, re-enqueue any job left PENDING/RUNNING (interrupted mid-flight).

    AWAITING_REVIEW is intentionally excluded — it's waiting for a human, not a crash.
    """
    try:
        pending = store.list_incomplete()
    except Exception:
        logger.exception("resume scan failed")
        return
    for job in pending:
        logger.info("resuming job %s (was %s at step '%s')", job.job_id, job.status, job.step)
        submit(job.job_id)
