"""Ghostreel API + UI.

Phase 0: POST /generate       - single image -> B2 (the seam).
Phase 3: POST /jobs           - enqueue an async video job, returns {job_id} instantly.
         GET  /jobs/{job_id}   - status/progress + the full provenance manifest.
Phase 4: GET  /                - the single-page UI (served same-origin, no CORS).
         GET  /jobs/{id}/media - presigned, browser-viewable URLs for a job's assets.

Jobs run in a background worker, persist state to B2 after every step, and resume on restart.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.jobs import runner, store
from app.models import GenerateRequest, GenerateResponse, Job, VideoRequest
from app.pipeline.visuals import generate_image
from app.storage.b2 import backend, key_from_url

logging.basicConfig(level=logging.INFO)
logging.getLogger("genblaze").setLevel(logging.INFO)

_STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-enqueue any job interrupted mid-flight by the last shutdown/crash.
    runner.resume_pending()
    yield


app = FastAPI(title="Ghostreel API", version="0.4.0", lifespan=lifespan)


def _view(url: str | None) -> str | None:
    """Turn a durable (private, non-fetchable) asset URL into a short-lived viewable one."""
    if not url:
        return None
    try:
        return backend().presigned_get_url(key_from_url(url), expires_in=3600)
    except Exception:
        return url


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


@app.get("/health")
def health() -> dict:
    missing = settings.missing_keys()
    return {
        "status": "ok" if not missing else "missing_env",
        "bucket": settings.b2_bucket_name or None,
        "missing_env": missing,
    }


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """Phase 0 seam: synchronous single-image generation to B2."""
    return GenerateResponse(**generate_image(req.prompt))


@app.post("/jobs")
def create_job(req: VideoRequest) -> dict:
    """Enqueue a video job and return immediately with its id (no long blocking request)."""
    job = Job(
        job_id=uuid.uuid4().hex,
        topic=req.topic,
        models={
            "script": settings.chat_model,
            "image": settings.image_model,
            "qa": settings.qa_model,
            "tts": settings.tts_model,
            "voice_id": settings.voice_id,
        },
    )
    store.save(job)
    runner.submit(job.job_id)
    return {"job_id": job.job_id, "status": job.status.value}


@app.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str) -> Job:
    """Full job record — status, step, and the provenance manifest (durable asset URLs)."""
    try:
        return store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e


@app.get("/jobs/{job_id}/media")
def job_media(job_id: str) -> dict:
    """Same job, but with presigned browser-viewable URLs for each asset — for the UI."""
    try:
        job = store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e

    segments = []
    for s in (job.script.segments if job.script else []):
        last = s.attempts[-1] if s.attempts else None
        segments.append({
            "index": s.index,
            "narration": s.narration,
            "visual": s.visual,
            "image_prompt": s.image_prompt,
            "image": _view(s.image_url),
            "audio": _view(s.audio_url),
            "attempts": len(s.attempts),
            "qa_passed": last.passed if last else None,
            "qa_reason": last.reason if last else None,
        })

    return {
        "job_id": job.job_id,
        "topic": job.topic,
        "status": job.status.value,
        "step": job.step,
        "retries": job.retries,
        "error": job.error,
        "models": job.models,
        "video": _view(job.video_url),
        "segments": segments,
    }
