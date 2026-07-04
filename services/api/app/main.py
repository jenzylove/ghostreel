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
from fastapi.responses import FileResponse, StreamingResponse

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


def _counter_key(d: str) -> str:
    return f"{settings.asset_prefix}/counters/{d}.json"


def _load_daily_count(today: str) -> int:
    """Read today's job count from B2 — called at startup so the limit survives restarts."""
    try:
        return json.loads(backend().get(_counter_key(today))).get("count", 0)
    except Exception:
        return 0


def _save_daily_count(today: str, count: int) -> None:
    try:
        backend().put(
            _counter_key(today),
            json.dumps({"count": count}).encode(),
            content_type="application/json",
        )
    except Exception:
        pass  # best-effort — a failed counter write must never block job creation


@asynccontextmanager
async def lifespan(app: FastAPI):
    today = date.today().isoformat()
    _daily["date"] = today
    _daily["count"] = _load_daily_count(today)
    runner.resume_pending()
    yield


app = FastAPI(title="Ghostreel API", version="0.5.0", lifespan=lifespan)


# --- rate gate ---------------------------------------------------------------------------
_daily = {"date": None, "count": 0}


def _require_code(code: str | None) -> None:
    if settings.access_code and code != settings.access_code:
        raise HTTPException(status_code=401, detail="invalid or missing access code")


def _gate_job(code: str | None) -> None:
    _require_code(code)
    today = date.today().isoformat()
    if _daily["date"] != today:
        _daily["date"] = today
        _daily["count"] = _load_daily_count(today)
    if _daily["count"] >= settings.daily_job_limit:
        raise HTTPException(
            status_code=429,
            detail=f"daily generation limit reached ({settings.daily_job_limit}); try again tomorrow",
        )
    _daily["count"] += 1
    _save_daily_count(today, _daily["count"])


def _view(url: str | None) -> str | None:
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
    try:
        r = httpx.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": settings.eleven_api_key},
            timeout=10,
        )
        r.raise_for_status()
        out = []
        for v in r.json().get("voices", []):
            if v.get("category") not in ("premade", None):
                continue  # skip cloned/professional voices — they 401 on most plans
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
    _require_code(x_access_code)
    return GenerateResponse(**generate_image(req.prompt))


@app.get("/jobs")
def list_jobs(x_session_id: str | None = Header(default=None)) -> list[dict]:
    """Return the current session's job history — lightweight summaries, no full scan."""
    if not x_session_id:
        return []
    return store.list_by_session(x_session_id)


@app.post("/jobs")
def create_job(
    req: VideoRequest,
    x_access_code: str | None = Header(default=None),
    x_session_id: str | None = Header(default=None),
) -> dict:
    """Enqueue a video job. Returns immediately with the job id."""
    _gate_job(x_access_code)

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
        target_minutes=req.target_minutes or settings.default_minutes,
        voice_id=req.voice_id or settings.voice_id,
        captions=req.captions,
        review=req.review or byo,
        user_id=x_session_id or "",
        models={
            "script": settings.chat_model,
            "image": settings.image_model,
            "qa": settings.qa_model,
            "tts": settings.tts_model if not byo else settings.stt_model,
            "voice_id": req.voice_id or settings.voice_id,
        },
    )
    store.save(job)
    store.update_session_index(job)
    store.add_pending(job.job_id)
    runner.submit(job.job_id)
    return {"job_id": job.job_id, "status": job.status.value}


@app.post("/jobs/{job_id}/record")
async def record_job(
    job_id: str,
    audio: UploadFile = File(...),
    narration: str = Form(""),
    x_access_code: str | None = Header(default=None),
) -> dict:
    """BYO: after reviewing the script, upload your recording — then rendering starts."""
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

    if narration.strip():
        job.script.narration = narration.strip()

    data = await audio.read()
    ext = (audio.filename or "audio.mp3").rsplit(".", 1)[-1].lower()
    if ext not in ("mp3", "wav", "m4a", "aac", "ogg", "webm", "mp4", "flac"):
        ext = "mp3"
    key = f"{settings.asset_prefix}/uploads/{uuid.uuid4().hex}.{ext}"
    backend().put(key, data, content_type=audio.content_type or "audio/mpeg")

    job.audio_key = key
    job.script.word_timings = []
    job.script.beats = []
    job.approved = True
    job.status = JobStatus.PENDING
    job.step = "queued"
    store.save(job)
    store.add_pending(job.job_id)
    runner.submit(job.job_id)
    return {"job_id": job.job_id, "status": job.status.value}


@app.post("/jobs/{job_id}/approve")
def approve_job(
    job_id: str, req: ApproveRequest, x_access_code: str | None = Header(default=None)
) -> dict:
    """Approve (optionally with an edited narration) and start rendering."""
    _require_code(x_access_code)
    try:
        job = store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e
    if job.status != JobStatus.AWAITING_REVIEW or job.script is None:
        raise HTTPException(status_code=409, detail=f"job {job_id} is not awaiting review")

    if req.narration and req.narration.strip():
        job.script.narration = req.narration.strip()
        # Clear downstream state so the pipeline re-derives everything from the edited text.
        job.script.audio_url = None
        job.script.word_timings = []
        job.script.beats = []

    job.approved = True
    job.status = JobStatus.PENDING
    job.step = "queued"
    store.save(job)
    store.add_pending(job.job_id)
    runner.submit(job.job_id)
    return {"job_id": job.job_id, "status": job.status.value}


@app.get("/jobs/{job_id}/download")
def download_video(job_id: str) -> StreamingResponse:
    """Proxy the finished video through the server so the browser can download it."""
    try:
        job = store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e
    if not job.video_url:
        raise HTTPException(status_code=404, detail="no video yet for this job")
    data = get_by_url(job.video_url)
    slug = job.topic[:40].replace(" ", "-").lower() if job.topic else job_id[:8]
    filename = f"ghostreel-{slug}.mp4"
    return StreamingResponse(
        iter([data]),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str) -> Job:
    try:
        return store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e


@app.get("/jobs/{job_id}/media")
def job_media(job_id: str) -> dict:
    """Same job, but with presigned browser-viewable URLs — for the UI."""
    try:
        job = store.load(job_id)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from e

    beats = []
    for b in (job.script.beats if job.script else []):
        last = b.attempts[-1] if b.attempts else None
        beats.append({
            "index": b.index,
            "start_s": b.start_s,
            "end_s": b.end_s,
            "text": b.text,
            "visual": b.visual,
            "image_prompt": b.image_prompt,
            "image": _view(b.image_url),
            "attempts": len(b.attempts),
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
        "narration": job.script.narration if job.script else None,
        "beat_count": len(job.script.beats) if job.script else 0,
        "video": _view(job.video_url),
        "beats": beats,
        "yt_title": job.yt_title,
        "yt_description": job.yt_description,
        "yt_tags": job.yt_tags,
        "thumbnail": _view(job.thumbnail_url),
    }
