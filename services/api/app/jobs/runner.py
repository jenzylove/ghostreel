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
from app.models import Beat, Job, JobStatus, QaAttempt, StylePreset
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


def _friendly_error(e: Exception) -> str:
    """Map internal exceptions to user-readable messages; keep raw detail in server logs."""
    msg = str(e)
    m = msg.lower()
    t = type(e).__name__
    if "tts returned no audio" in msg:
        return "Voice generation returned no audio — check ElevenLabs key and selected voice"
    if "assemblyai returned no assets" in msg:
        return "Transcription returned no results — audio may be empty or too short"
    if ("401" in msg or "403" in msg) and ("eleven" in m or "voice" in m or "tts" in m):
        return "Voice generation failed — ElevenLabs key invalid or voice not on your plan"
    if "eleven" in m or ("tts" in m and "fail" in m):
        return "Voice generation failed — check your ElevenLabs API key"
    if "assemblyai" in m or ("transcri" in m and "fail" in m):
        return "Transcription failed — check your AssemblyAI API key"
    if "imagen" in m or ("image" in m and "generat" in m):
        return "Image generation failed — check your Gemini/Imagen API key"
    if "gemini" in m or "script generation" in m or "narration" in m:
        return "Script generation failed — check your Gemini API key"
    if "b2" in m or "backblaze" in m or "bucket" in m:
        return "Storage error — check your B2 credentials and bucket name"
    if "timeout" in m:
        return "A pipeline step timed out — try a shorter video length"
    if t == "RuntimeError":
        return msg[:200]  # RuntimeErrors are already written to be readable
    return f"Unexpected error ({t}) — check server logs for details"


def submit(job_id: str, job: Job | None = None) -> None:
    _executor.submit(_safe_run, job_id, job)


def _safe_run(job_id: str, job: Job | None = None) -> None:
    try:
        run_job(job_id, job)
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
) -> dict[int, bytes]:
    """Generate + QA images for all pending beats using a 4-worker thread pool.

    Results are written back to the beat objects in-place; job state is saved after each
    completion so the UI progress updates in real time.

    Returns a cache of {beat_index: image_bytes} so the assembler can skip re-downloading
    files that are already in memory from the QA fetch.
    """
    total = len(job.script.beats)
    image_cache: dict[int, bytes] = {}

    def _worker(beat: Beat) -> tuple[int, str | None, bytes | None, list[QaAttempt]]:
        url, data, attempts = generate_and_qa(beat.image_prompt, beat, preset)
        return beat.index, url, data, attempts

    with ThreadPoolExecutor(max_workers=_BEAT_WORKERS) as pool:
        futures = {pool.submit(_worker, b): b for b in beats_todo}
        for future in as_completed(futures):
            try:
                idx, url, data, attempts = future.result()
            except Exception:  # noqa: BLE001
                beat = futures[future]
                logger.exception("beat %s image worker failed", beat.index)
                beat.attempts = [
                    QaAttempt(attempt=1, url=None, passed=False, score=0.0, reason="image generation failed")
                ]
                store.save(job)
                continue
            beat = next(b for b in job.script.beats if b.index == idx)
            beat.image_url = url
            beat.attempts = attempts
            if data:
                image_cache[idx] = data
            done = sum(1 for b in job.script.beats if b.image_url)
            job.step = f"image {done}/{total}"
            store.save(job)

    return image_cache


def run_job(job_id: str, job: Job | None = None) -> None:  # noqa: C901
    job = job or store.load(job_id)
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
            store.update_session_index(job)
            store.remove_pending(job.job_id)
            return

        # 2. Audio.
        #    TTS: generate_voice_full handles any length via sentence-boundary chunking.
        #    BYO: recording is already stored in B2 under job.audio_key by /record endpoint.
        if not byo and not job.script.audio_url:
            job.step = "voice"
            store.save(job)
            job.script.audio_url = generate_voice_full(job.script.narration, voice_id=voice_id)
            if not job.script.audio_url:
                raise RuntimeError("TTS returned no audio — check ELEVEN_API_KEY and voice_id")
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
            job.script.beats = group_into_beats(job.script.word_timings, beat_duration_s=job.beat_duration_s)
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
        #    Cache holds raw bytes already fetched during QA so assemble skips re-downloading.
        image_cache: dict[int, bytes] = {}
        beats_todo = [b for b in job.script.beats if b.image_prompt and not b.image_url]
        if beats_todo:
            image_cache = _generate_beat_images_parallel(job, beats_todo, preset)

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
            job.video_url = assemble_slideshow(job.script, audio_bytes, captions=job.captions, image_cache=image_cache)
            store.save(job)

        job.retries = sum(max(0, len(b.attempts) - 1) for b in job.script.beats)
        job.status = JobStatus.DONE
        job.step = "done"
        store.save(job)
        store.update_session_index(job)
        store.remove_pending(job.job_id)

    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = _friendly_error(e)
        store.save(job)
        store.update_session_index(job)
        store.remove_pending(job.job_id)
        raise


def submit_regenerate(job_id: str, beat_index: int) -> None:
    _executor.submit(_safe_regenerate, job_id, beat_index)


def _safe_regenerate(job_id: str, beat_index: int) -> None:
    try:
        regenerate_beat(job_id, beat_index)
    except Exception:
        logger.exception("regenerate beat %s of job %s crashed", beat_index, job_id)


def regenerate_beat(job_id: str, beat_index: int) -> None:
    """Regenerate one beat's image (fresh QA), then re-assemble the video.

    Used after a job is DONE when the user dislikes a single still. Only the one image
    is regenerated; every other beat image is reused from B2 during reassembly.
    """
    job = store.load(job_id)
    if not job.script:
        return
    beat = next((b for b in job.script.beats if b.index == beat_index), None)
    if beat is None or not beat.image_prompt:
        return

    preset = job.style or DEFAULT_STYLE
    byo = job.voice_mode == "byo"
    job.status = JobStatus.RUNNING
    job.error = None
    job.step = f"regenerating image {beat_index + 1}"
    store.save(job)
    store.update_session_index(job)
    store.add_pending(job.job_id)

    try:
        url, data, attempts = generate_and_qa(beat.image_prompt, beat, preset)
        if url:
            beat.image_url = url
            beat.attempts = attempts
            store.save(job)

        # Re-assemble: only the regenerated image is passed in-memory; the rest come from B2.
        job.step = "reassembling video"
        job.video_url = None
        store.save(job)
        audio_bytes = backend().get(job.audio_key) if byo else get_by_url(job.script.audio_url)
        cache = {beat_index: data} if data else None
        job.video_url = assemble_slideshow(
            job.script, audio_bytes, captions=job.captions, image_cache=cache
        )

        job.retries = sum(max(0, len(b.attempts) - 1) for b in job.script.beats)
        job.status = JobStatus.DONE
        job.step = "done"
        store.save(job)
        store.update_session_index(job)
        store.remove_pending(job.job_id)

    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = _friendly_error(e)
        store.save(job)
        store.update_session_index(job)
        store.remove_pending(job.job_id)
        raise


def submit_batch_regenerate(job_id: str, beat_indices: list[int]) -> None:
    _executor.submit(_safe_batch_regenerate, job_id, beat_indices)


def _safe_batch_regenerate(job_id: str, beat_indices: list[int]) -> None:
    try:
        batch_regenerate_and_assemble(job_id, beat_indices)
    except Exception:
        logger.exception("batch regen job %s crashed", job_id)


def batch_regenerate_and_assemble(job_id: str, beat_indices: list[int]) -> None:
    """Regenerate the selected beats' images in parallel, then reassemble the video.

    If beat_indices is empty, skips image generation and just reassembles with the
    current images already stored in B2.
    """
    job = store.load(job_id)
    if not job.script:
        return

    preset = job.style or DEFAULT_STYLE
    byo = job.voice_mode == "byo"
    n = len(beat_indices)
    job.status = JobStatus.RUNNING
    job.error = None
    job.step = f"regenerating {n} image(s)" if n else "reassembling video"
    store.save(job)
    store.update_session_index(job)
    store.add_pending(job.job_id)

    try:
        image_cache: dict[int, bytes] = {}
        if beat_indices:
            idx_set = set(beat_indices)
            beats_todo = [b for b in job.script.beats if b.index in idx_set and b.image_prompt]
            image_cache = _generate_beat_images_parallel(job, beats_todo, preset)

        job.step = "reassembling video"
        job.video_url = None
        store.save(job)
        audio_bytes = backend().get(job.audio_key) if byo else get_by_url(job.script.audio_url)
        job.video_url = assemble_slideshow(
            job.script, audio_bytes, captions=job.captions, image_cache=image_cache
        )

        job.retries = sum(max(0, len(b.attempts) - 1) for b in job.script.beats)
        job.status = JobStatus.DONE
        job.step = "done"
        store.save(job)
        store.update_session_index(job)
        store.remove_pending(job.job_id)

    except Exception as e:  # noqa: BLE001
        job.status = JobStatus.FAILED
        job.error = _friendly_error(e)
        store.save(job)
        store.update_session_index(job)
        store.remove_pending(job.job_id)
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
