"""Ghostreel API.

Phase 0: POST /generate  - single image -> B2 (the seam).
Phase 1: POST /video     - topic -> script -> style-locked images + voice -> assembled MP4.

The async job runner, evaluate-retry loop, provenance, and resumable jobs arrive in
later phases.
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from app.config import settings
from app.models import GenerateRequest, GenerateResponse, VideoRequest, VideoResponse
from app.pipeline.video import create_video
from app.pipeline.visuals import generate_image

# Dev diagnostic: surface Genblaze's own phase logging. Drop to WARNING before the demo.
logging.basicConfig(level=logging.INFO)
logging.getLogger("genblaze").setLevel(logging.INFO)

app = FastAPI(title="Ghostreel API", version="0.1.0")


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
    """Phase 0 seam: synchronous single-image generation to B2."""
    return GenerateResponse(**generate_image(req.prompt))


@app.post("/video", response_model=VideoResponse)
def video(req: VideoRequest) -> VideoResponse:
    """Phase 1: topic -> finished MP4 in B2.

    Synchronous (a full video is a multi-minute job). Phase 3 moves this behind the async
    job runner with progress/cancel/resume.
    """
    return create_video(req.topic)
