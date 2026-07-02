"""Request/response + domain schemas."""
from __future__ import annotations

from pydantic import BaseModel


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
    image_url: str | None = None
    audio_url: str | None = None
    duration_s: float | None = None   # measured from the generated audio (audio drives timing)
    attempts: list[QaAttempt] = []    # every QA attempt for this segment's image


class Script(BaseModel):
    topic: str
    segments: list[Segment]


class VideoRequest(BaseModel):
    topic: str


class VideoResponse(BaseModel):
    video_url: str | None
    segments: int
    retries: int = 0                  # total auto-regenerations across all segments (self-healing count)
    took_s: float
