"""Ghostreel API.

Phase 0: POST /generate       - single image -> B2 (the seam).
Phase 3: POST /jobs           - enqueue an async video job, returns {job_id} instantly.
         GET  /jobs/{job_id}   - status/progress + the full provenance manifest.

Jobs run in a background worker, persist state to B2 after every step, and resume on restart.
"""
from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app.config import settings
from app.jobs import runner, store
from app.models import GenerateRequest, GenerateResponse, Job, VideoRequest
from app.pipeline.visuals import generate_image

logging.basicConfig(level=logging.INFO)
logging.getLogger("genblaze").setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-enqueue any job interrupted mid-flight by the last shutdown/crash.
    runner.resume_pending()
    yield


app = FastAPI(title="Ghostreel API", version="0.3.0", lifespan=lifespan)


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
    """Enqueue a video job and return immediately with its id (no 7-minute blocking request)."""
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
    """Full job record — status, current step, and the provenance manifest (script, prompts,
    per-segment QA verdicts, models, timestamps, video URL)."""
    try:
        return store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e
