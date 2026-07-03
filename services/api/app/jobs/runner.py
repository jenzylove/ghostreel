"""Async job runner: resumable state machine + startup resume scan.

Flow for both modes:
  1  generate_script       → single continuous narration
  1b generate_yt_metadata  → title / description / tags
  1c [review gate]         → optional human edit of narration
  2  audio                 → TTS (chunked, full narration) OR BYO upload already stored
  3  transcribe            → AssemblyAI word-level timings on the full audio
  4  group_into_beats      → visual windows every ~10 seconds
  5  derive_beat_visuals   → one visual description per beat (single LLM call)
  6  images                → parallel QA+retry per beat (4 workers)
  7  thumbnail             → extra Imagen call
  8  assemble_slideshow    → beat images + single audio → MP4 in B2

Every step writes to B2 before moving on; completed steps are skipped on resume.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.config import settings
from app.jobs import store
from app.models import Beat, Job, JobStatus, QaAttempt, Segment, StylePreset
from app.pipeline.assemble import assemble_slideshow
from app.pipeline.metadata import generate_thumbnail, generate_yt_metadata
from app.pipeline.script import derive_beat_visuals, generate_script
from app.pipeline.style import DEFAULT_STYLE
from app.pipeline.transcribe import group_into_beats, transcribe_url
from app.pipeline.visuals import generate_and_qa
from app.pipeline.voice import generate_voice_full
from app.storage.b2 import backend, get_by_url, key_from_url

logger = logging.getLogger("ghostreel.runner")
_executor = ThreadPoolExecutor(max_workers=2)
_BEAT_WORKERS = 4


def submit(job_id: str) -> None:
    _executor.submit(_safe_run, job_id)


def _safe_run(job_id: str) -> None:
    try:
        run_job(job_id)
    except Exception:
        logger.exception("job %s crashed", job_id)


def _presigned(url: str) -> str:
    """Convert a durable B2 URL to a short-lived presigned one (for AssemblyAI)."""
    try:
        return backend().presigned_get_url(key_from_url(url), expires_in=3600)
    except Exception:
        return url


def _generate_beat_images_parallel(
    job: Job, beats_todo: list[Beat], preset: StylePreset
) -> None:
    """Generate + QA images for all pending beats using a 4-worker thread pool.

    Results are written back to the beat objects in-place; job state is saved after each
    completion so the UI progress updates in real time.
    """
    total = len(job.script.beats)

    def _worker(beat: Beat) -> tuple[int, str | None, list[QaAttempt]]:
        # Adapter: generate_and_qa takes a Segment-like object; QA doesn't use its fields.
        fake_seg = Segment(index=beat.index, narration=beat.text, visual=beat.visual)
        url, attempts = generate_and_qa(beat.image_prompt, fake_seg, preset)
        return beat.index, url, attempts

    with ThreadPoolExecutor(max_workers=_BEAT_WORKERS) as pool:
        futures = {pool.submit(_worker, b): b for b in beats_todo}
        for future in as_completed(futures):
            try:
                idx, url, attempts = future.result()
            except Exception as e:  # noqa: BLE001
                beat = futures[future]
                beat.attempts = [
                    QaAttempt(attempt=1, url=None, passed=False, score=0.0, reason=f"worker error: {e}")
                ]
                store.save(job)
                continue
            beat = next(b for b in job.script.beats if b.index == idx)
            beat.image_url = url
            beat.attempts = attempts
            done = sum(1 for b in job.script.beats if b.image_url)
            job.step = f"image {done}/{total}"
            store.save(job)


def run_job(job_id: str) -> None:  # noqa: C901
    job = store.load(job_id)
    preset = job.style or DEFAULT_STYLE
    voice_id = job.voice_id or settings.voice_id
    byo = job.voice_mode == "byo"
    job.status = JobStatus.RUNNING
    job.error = None
    store.save(job)

    try:
        # 1. Script — single continuous narration.
        if job.script is None:
            job.step = "script"
            store.save(job)
            job.script = generate_script(job.topic, target_minutes=job.target_minutes)
            store.save(job)

        # 1b. YouTube metadata — generated right after script so it's visible during review.
        if job.script and job.yt_title is None:
            job.step = "metadata"
            store.save(job)
            meta = generate_yt_metadata(job.script)
            job.yt_title = meta["title"]
            job.yt_description = meta["description"]
            job.yt_tags = meta["tags"]
            store.save(job)

        # 1c. Review gate — optional human edit of the narration text.
        if job.review and not job.approved:
            job.status = JobStatus.AWAITING_REVIEW
            job.step = "awaiting review"
            store.save(job)
            return

        # 2. Audio.
        #    TTS: generate_voice_full handles any length via sentence-boundary chunking.
        #    BYO: recording is already stored in B2 under job.audio_key by /record endpoint.
        if not byo and not job.script.audio_url:
            job.step = "voice"
            store.save(job)
            job.script.audio_url = generate_voice_full(job.script.narration, voice_id=voice_id)
            store.save(job)

        # 3. Word-level timings (AssemblyAI on the full audio).
        if not job.script.word_timings:
            job.step = "transcribe"
            store.save(job)
            if byo:
                audio_url_for_stt = backend().presigned_get_url(job.audio_key, expires_in=3600)
            else:
                audio_url_for_stt = _presigned(job.script.audio_url)
            job.script.word_timings = transcribe_url(audio_url_for_stt)["words"]
            store.save(job)

        # 4. Beat grouping — visual windows derived from timing, not word count.
        if not job.script.beats:
            job.step = "beats"
            store.save(job)
            job.script.beats = group_into_beats(job.script.word_timings)
            store.save(job)

        # 5. Visual descriptions — one LLM call for all beats that don't have one yet.
        beats_no_visual = [b for b in job.script.beats if not b.visual]
        if beats_no_visual:
            job.step = "visuals"
            store.save(job)
            visuals = derive_beat_visuals(beats_no_visual, job.topic)
            for beat, vis in zip(beats_no_visual, visuals):
                beat.visual = vis
                beat.image_prompt = preset.apply(vis)
            store.save(job)

        # 6. Image generation — parallel across all beats that don't have an image yet.
        beats_todo = [b for b in job.script.beats if b.image_prompt and not b.image_url]
        if beats_todo:
            _generate_beat_images_parallel(job, beats_todo, preset)

        # 7. Thumbnail — extra Imagen call (adds to Genblaze usage).
        if not job.thumbnail_url:
            job.step = "thumbnail"
            store.save(job)
            job.thumbnail_url = generate_thumbnail(job.topic, preset)
            store.save(job)

        # 8. Assemble.
        if not job.video_url:
            job.step = "assemble"
            store.save(job)
            if byo:
                audio_bytes = backend().get(job.audio_key)
            else:
                audio_bytes = get_by_url(job.script.audio_url)
            job.video_url = assemble_slideshow(job.script, audio_bytes, captions=job.captions)
            store.save(job)

        job.retries = sum(max(0, len(b.attempts) - 1) for b in job.script.beats)
        job.status = JobStatus.DONE
        job.step = "done"
        store.save(job)

    except Exception as e:  # noqa: BLE001
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
