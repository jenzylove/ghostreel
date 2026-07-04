"""Request/response + domain schemas."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- Phase 0 seam ---
class GenerateRequest(BaseModel):
    prompt: str


class GenerateResponse(BaseModel):
    asset_url: str | None
    took_s: float
    raw: str | None = None


# --- Style ---
class StylePreset(BaseModel):
    """A reusable style-lock string appended to every image prompt."""

    positive: str
    negatives: str = ""

    def apply(self, subject: str) -> str:
        prompt = ", ".join(p for p in (subject.strip(), self.positive.strip()) if p)
        if self.negatives.strip():
            prompt += f". Avoid: {self.negatives.strip()}"
        return prompt


# --- QA ---
class Verdict(BaseModel):
    passed: bool
    score: float
    reason: str


class QaAttempt(BaseModel):
    """One generate+evaluate cycle for a beat image (the self-healing audit trail)."""
    attempt: int
    url: str | None
    passed: bool
    score: float
    reason: str


# --- Slideshow beat ---
class Beat(BaseModel):
    """One visual beat: the time window during which one image is shown on screen."""
    index: int
    start_s: float                 # seconds into the audio where this image appears
    end_s: float                   # seconds into the audio where the next image takes over
    text: str                      # words spoken during this beat (drives the image prompt)
    visual: str = ""               # derived visual description
    image_prompt: str | None = None
    image_url: str | None = None
    attempts: list[QaAttempt] = []


# --- Script (new: single continuous narration) ---
class Script(BaseModel):
    topic: str
    narration: str = ""            # full continuous spoken text (600-900 words)
    audio_url: str | None = None   # single TTS audio stored in B2 (None for BYO — use Job.audio_key)
    word_timings: list[dict] = []  # AssemblyAI word-level timings [{word, start, end}, ...]
    beats: list[Beat] = []         # populated after word timings; one image per beat


# --- Job states ---
class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"
    DONE = "done"
    FAILED = "failed"


class Job(BaseModel):
    """A video job. Persisted to B2 as JSON — doubles as the provenance manifest."""

    job_id: str
    topic: str
    status: JobStatus = JobStatus.PENDING
    step: str = "queued"
    script: Script | None = None
    video_url: str | None = None
    retries: int = 0
    error: str | None = None
    models: dict[str, str] = {}
    # Bring-your-own-voice: user uploads their own recording; pipeline transcribes it.
    voice_mode: str = "tts"            # "tts" | "byo"
    audio_key: str | None = None       # B2 key of BYO uploaded recording
    # Phase 4 controls
    style_id: str = "doodle"
    style: StylePreset | None = None
    target_minutes: float = 4.0        # target video length — determines script word count
    voice_id: str = ""
    captions: bool = True
    review: bool = False
    approved: bool = False
    # Phase 5: YouTube upload package
    yt_title: str | None = None
    yt_description: str | None = None
    yt_tags: list[str] = []
    thumbnail_url: str | None = None
    # Anonymous session (localStorage UUID) — links jobs to a browser without requiring login
    user_id: str = ""
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# --- API bodies ---
class VideoRequest(BaseModel):
    topic: str
    voice_mode: str = "tts"
    style_id: str | None = None
    style_positive: str | None = None
    style_negatives: str | None = None
    target_minutes: float | None = None   # 2 / 4 / 6 / 8 — determines script length
    voice_id: str | None = None
    captions: bool = True
    review: bool = False


class ApproveRequest(BaseModel):
    narration: str | None = None   # if provided, replaces the generated narration before render


class VideoResponse(BaseModel):
    video_url: str | None
    beats: int
    retries: int = 0
    took_s: float
