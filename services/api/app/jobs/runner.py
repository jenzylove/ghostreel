"""Async job runner: resumable state machine + startup resume scan.

Each job's state is persisted to B2 after every step, and every step is skipped when its
output already exists in the loaded state. So a crash/restart resumes from the last completed
segment instead of restarting. When review is on, the job pauses after the script for human
approval/edit (Phase 4) — that state is NOT auto-resumed (it waits for a human).
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from app.config import settings
from app.jobs import store
from app.models import JobStatus
from app.pipeline.assemble import assemble_video
from app.pipeline.script import generate_script
from app.pipeline.style import DEFAULT_STYLE
from app.pipeline.visuals import generate_and_qa
from app.pipeline.voice import generate_voice

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
    job.status = JobStatus.RUNNING
    job.error = None
    store.save(job)

    try:
        # 1. Script (skip if already generated).
        if job.script is None:
            job.step = "script"
            store.save(job)
            job.script = generate_script(job.topic, n=job.segment_count)
            store.save(job)

        # 1b. Review gate: pause for human approval/edit before spending on images/voice.
        if job.review and not job.approved:
            job.status = JobStatus.AWAITING_REVIEW
            job.step = "awaiting review"
            store.save(job)
            return

        total = len(job.script.segments)

        # 2. Per segment: image (QA + retry) then voice. Persist after each so resume is cheap.
        for seg in job.script.segments:
            if not seg.image_url:
                job.step = f"image {seg.index + 1}/{total}"
                store.save(job)
                seg.image_prompt = preset.apply(seg.visual)
                seg.image_url, seg.attempts = generate_and_qa(seg.image_prompt, seg, preset)
                store.save(job)
            if not seg.audio_url:
                job.step = f"voice {seg.index + 1}/{total}"
                store.save(job)
                seg.audio_url = generate_voice(seg.narration, voice_id=voice_id)
                store.save(job)

        # 3. Assemble (skip if already done).
        if not job.video_url:
            job.step = "assemble"
            store.save(job)
            job.video_url = assemble_video(job.script)
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
