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


# --- Phase 1 vertical slice ---
class StylePreset(BaseModel):
    """A reusable style-lock string appended to every image prompt (the proven technique)."""

    positive: str
    negatives: str = ""

    def apply(self, subject: str) -> str:
        prompt = ", ".join(p for p in (subject.strip(), self.positive.strip()) if p)
        if self.negatives.strip():
            prompt += f". Avoid: {self.negatives.strip()}"
        return prompt


# --- Phase 2 evaluate-retry ---
class Verdict(BaseModel):
    passed: bool
    score: float
    reason: str


class QaAttempt(BaseModel):
    """One generate+evaluate cycle for a segment image (the self-healing audit trail)."""

    attempt: int
    url: str | None
    passed: bool
    score: float
    reason: str


class Segment(BaseModel):
    index: int
    narration: str                    # spoken text — drives the voiceover AND the timing
    visual: str                       # what the still image should show
    image_prompt: str | None = None   # the styled prompt actually sent (provenance)
    image_url: str | None = None
    audio_url: str | None = None
    duration_s: float | None = None   # measured from the generated audio (audio drives timing)
    attempts: list[QaAttempt] = []    # every QA attempt for this segment's image


class Script(BaseModel):
    topic: str
    segments: list[Segment]


# --- Phase 3/4 jobs ---
class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING_REVIEW = "awaiting_review"   # script generated, paused for human approval/edit
    DONE = "done"
    FAILED = "failed"


class Job(BaseModel):
    """A video job. Persisted to B2 as JSON — the record doubles as the provenance manifest."""

    job_id: str
    topic: str
    status: JobStatus = JobStatus.PENDING
    step: str = "queued"
    script: Script | None = None
    video_url: str | None = None
    retries: int = 0
    error: str | None = None
    models: dict[str, str] = {}
    # Phase 4 controls:
    style_id: str = "doodle"
    style: StylePreset | None = None      # resolved preset (falls back to default if None)
    segment_count: int = 6
    voice_id: str = ""
    review: bool = False                  # pause after script for human approval/edit
    approved: bool = False
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)


# --- API bodies ---
class VideoRequest(BaseModel):
    topic: str
    style_id: str | None = None
    style_positive: str | None = None      # custom style (from a reference image) — overrides style_id
    style_negatives: str | None = None
    segment_count: int | None = None
    voice_id: str | None = None
    review: bool = False


class SegmentEdit(BaseModel):
    index: int
    narration: str
    visual: str


class ApproveRequest(BaseModel):
    segments: list[SegmentEdit] | None = None   # optional edited script; omit to approve as-is


class VideoResponse(BaseModel):
    video_url: str | None
    segments: int
    retries: int = 0
    took_s: float
