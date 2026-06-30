"""Request/response schemas. Phase 0 covers the single-image seam only."""
from __future__ import annotations

from pydantic import BaseModel


class GenerateRequest(BaseModel):
    prompt: str


class GenerateResponse(BaseModel):
    asset_url: str | None
    took_s: float
    # Populated only when the asset URL couldn't be extracted, so we can see the raw shape.
    raw: str | None = None
