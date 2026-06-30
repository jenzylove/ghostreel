"""Ghostreel API - Phase 0 scaffold.

Proves the seam end to end: HTTP request -> Genblaze image generation -> Backblaze B2.
The full async job pipeline (script -> segments -> visuals -> voice -> assemble), the
evaluate-retry loop, provenance, and resumable jobs arrive in later phases.
"""
from __future__ import annotations

from fastapi import FastAPI

from app.config import settings
from app.models import GenerateRequest, GenerateResponse
from app.pipeline.visuals import generate_image

app = FastAPI(title="Ghostreel API", version="0.0.0")


@app.get("/health")
def health() -> dict:
    missing = settings.missing_keys()
    return {
        "status": "ok" if not missing else "missing_env",
        "bucket": settings.b2_bucket_name or None,
        "image_model": settings.image_model,
        "missing_env": missing,
    }


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest) -> GenerateResponse:
    """Phase 0 seam: synchronous single-image generation to B2.

    Intentionally synchronous - one image is ~55s end to end. Phase 1+ replaces this with
    POST /jobs that enqueues an async job handled by jobs/runner.py.
    """
    return GenerateResponse(**generate_image(req.prompt))
