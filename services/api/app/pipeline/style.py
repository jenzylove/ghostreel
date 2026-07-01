"""Style presets — the style-lock mechanism proven in the spike.

A single reusable style string is appended to every segment's image prompt so all
generations share one look. Phase 1 ships the proven default; Phase 4 adds
extract_style_preset(reference_images) -> StylePreset (vision LLM) + named presets in B2.
"""
from __future__ import annotations

from app.models import StylePreset

# The exact style that held consistent across every generation in the WSL spike.
DEFAULT_STYLE = StylePreset(
    positive=(
        "hand-drawn marker doodle illustration, thick uneven black felt-tip outlines, "
        "flat muted colors, simple shapes"
    ),
    negatives="photorealism, 3d render, photograph, glossy, hyperdetailed, text, watermark",
)
