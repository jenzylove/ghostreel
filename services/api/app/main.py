"""Ghostreel API + UI.

Phase 0: POST /generate            - single image -> B2 (the seam).
Phase 3: POST /jobs                - enqueue an async video job, returns {job_id} instantly.
         GET  /jobs/{job_id}        - status/progress + the full provenance manifest.
Phase 4: GET  /                     - the single-page UI (served same-origin, no CORS).
         GET  /jobs/{id}/media      - presigned, browser-viewable URLs for a job's assets.
         POST /jobs/{id}/approve    - approve/edit the reviewed script, then render.
         GET  /presets, /voices     - options for the UI controls.

Jobs run in a background worker, persist state to B2 after every step, and resume on restart.
"""
from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.config import VOICES, settings
from app.jobs import runner, store
from app.models import (
    ApproveRequest,
    GenerateRequest,
    GenerateResponse,
    Job,
    JobStatus,
    StylePreset,
    VideoRequest,
)
from app.pipeline.style import extract_style_preset, get_preset, list_presets
from app.pipeline.visuals import generate_image
from app.storage.b2 import backend, key_from_url

logging.basicConfig(level=logging.INFO)
logging.getLogger("genblaze").setLevel(logging.INFO)

_STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    runner.resume_pending()
    yield


app = FastAPI(title="Ghostreel API", version="0.4.0", lifespan=lifespan)


# --- rate gate ---------------------------------------------------------------------------
_daily = {"date": None, "count": 0}


def _require_code(code: str | None) -> None:
    """Reject when a gate is configured and the X-Access-Code header is missing/wrong."""
    if settings.access_code and code != settings.access_code:
        raise HTTPException(status_code=401, detail="invalid or missing access code")


def _gate_job(code: str | None) -> None:
    """Access-code check + a hard per-day ceiling on jobs started (budget backstop)."""
    _require_code(code)
    today = date.today().isoformat()
    if _daily["date"] != today:
        _daily["date"], _daily["count"] = today, 0
    if _daily["count"] >= settings.daily_job_limit:
        raise HTTPException(
            status_code=429,
            detail=f"daily generation limit reached ({settings.daily_job_limit}); try again tomorrow",
        )
    _daily["count"] += 1


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
        "gated": bool(settings.access_code),
    }


@app.get("/presets")
def presets() -> list[dict]:
    return list_presets()


@app.get("/voices")
def voices() -> list[dict]:
    """The account's real ElevenLabs voices (so every option actually works, incl. female
    voices). Falls back to the static list if the API call fails."""
    try:
        r = httpx.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": settings.eleven_api_key},
            timeout=10,
        )
        r.raise_for_status()
        out = []
        for v in r.json().get("voices", []):
            gender = (v.get("labels") or {}).get("gender")
            name = v["name"] + (f" ({gender})" if gender else "")
            out.append({"name": name, "id": v["voice_id"]})
        if out:
            return out
    except Exception:
        pass
    return [{"name": name, "id": vid} for name, vid in VOICES.items()]


@app.post("/style/extract")
async def style_extract(
    files: list[UploadFile] = File(...), x_access_code: str | None = Header(default=None)
) -> dict:
    """Extract a reusable style preset from uploaded reference image(s) via Gemini vision."""
    _require_code(x_access_code)
    images = [await f.read() for f in files]
    if not images:
        raise HTTPException(status_code=400, detail="no images uploaded")
    try:
        preset = extract_style_preset(images)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"style extraction failed: {e}") from e
    return {"positive": preset.positive, "negatives": preset.negatives}


@app.post("/generate", response_model=GenerateResponse)
def generate(req: GenerateRequest, x_access_code: str | None = Header(default=None)) -> GenerateResponse:
    """Phase 0 seam: synchronous single-image generation to B2."""
    _require_code(x_access_code)
    return GenerateResponse(**generate_image(req.prompt))


@app.post("/jobs")
def create_job(req: VideoRequest, x_access_code: str | None = Header(default=None)) -> dict:
    """Enqueue a video job with the chosen controls. Returns immediately with its id."""
    _gate_job(x_access_code)

    # Custom style (extracted from a reference image) overrides the named preset.
    if req.style_positive:
        style = StylePreset(positive=req.style_positive, negatives=req.style_negatives or "")
        style_id = "custom"
    else:
        style = get_preset(req.style_id)
        style_id = req.style_id or "doodle"

    byo = req.voice_mode == "byo"
    job = Job(
        job_id=uuid.uuid4().hex,
        topic=req.topic,
        voice_mode=req.voice_mode,
        style_id=style_id,
        style=style,
        segment_count=req.segment_count or settings.segment_count,
        voice_id=req.voice_id or settings.voice_id,
        captions=req.captions,
        review=req.review or byo,   # BYO always reviews the script, then records it
        models={
            "script": settings.chat_model,
            "image": settings.image_model,
            "qa": settings.qa_model,
            "tts": settings.tts_model if not byo else settings.stt_model,
            "voice_id": req.voice_id or settings.voice_id,
        },
    )
    store.save(job)
    runner.submit(job.job_id)
    return {"job_id": job.job_id, "status": job.status.value}


@app.post("/jobs/{job_id}/record")
async def record_job(
    job_id: str,
    audio: UploadFile = File(...),
    segments: str = Form(""),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """BYO: after reviewing the script, upload your recording of it — then rendering starts."""
    _require_code(x_access_code)
    try:
        job = store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e
    if job.status != JobStatus.AWAITING_REVIEW or job.script is None:
        raise HTTPException(status_code=409, detail=f"job {job_id} is not awaiting review")
    if job.voice_mode != "byo":
        raise HTTPException(status_code=400, detail="record is only for bring-your-own-voice jobs")
    if not settings.assemblyai_api_key:
        raise HTTPException(status_code=400, detail="BYO voice requires ASSEMBLYAI_API_KEY")

    # Apply any script edits made in the review panel.
    if segments:
        by_index = {s.index: s for s in job.script.segments}
        for edit in json.loads(segments):
            seg = by_index.get(edit.get("index"))
            if seg:
                seg.narration = edit.get("narration", seg.narration)
                seg.visual = edit.get("visual", seg.visual)

    data = await audio.read()
    ext = (audio.filename or "audio.mp3").rsplit(".", 1)[-1].lower()
    if ext not in ("mp3", "wav", "m4a", "aac", "ogg", "webm", "mp4", "flac"):
        ext = "mp3"
    key = f"{settings.asset_prefix}/uploads/{uuid.uuid4().hex}.{ext}"
    backend().put(key, data, content_type=audio.content_type or "audio/mpeg")

    job.audio_key = key
    job.word_timings = []          # runner transcribes in the background
    job.approved = True
    job.status = JobStatus.PENDING
    job.step = "queued"
    store.save(job)
    runner.submit(job.job_id)
    return {"job_id": job.job_id, "status": job.status.value}


@app.post("/jobs/{job_id}/approve")
def approve_job(
    job_id: str, req: ApproveRequest, x_access_code: str | None = Header(default=None)
) -> dict:
    """Approve (optionally with edits) a script that's awaiting review, then start rendering."""
    _require_code(x_access_code)
    try:
        job = store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e
    if job.status != JobStatus.AWAITING_REVIEW or job.script is None:
        raise HTTPException(status_code=409, detail=f"job {job_id} is not awaiting review")

    if req.segments:
        by_index = {s.index: s for s in job.script.segments}
        for edit in req.segments:
            seg = by_index.get(edit.index)
            if seg:
                seg.narration = edit.narration
                seg.visual = edit.visual

    job.approved = True
    job.status = JobStatus.PENDING
    job.step = "queued"
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
        "style_id": job.style_id,
        "voice_mode": job.voice_mode,
        "video": _view(job.video_url),
        "segments": segments,
    }
