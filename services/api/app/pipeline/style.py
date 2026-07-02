"""Style presets — the style-lock mechanism proven in the spike.

A single reusable style string is appended to every segment's image prompt so all
generations share one look. Users pick a preset (Phase 4 control). Phase 4-rich extension:
extract_style_preset(reference_images) -> StylePreset via a vision LLM + custom presets in B2.
"""
from __future__ import annotations

from app.models import StylePreset

PRESETS: dict[str, StylePreset] = {
    "doodle": StylePreset(
        positive=(
            "hand-drawn marker doodle illustration, thick uneven black felt-tip outlines, "
            "flat muted colors, simple shapes"
        ),
        negatives="photorealism, 3d render, photograph, glossy, hyperdetailed, text, watermark",
    ),
    "line": StylePreset(
        positive=(
            "minimalist single-line ink drawing, clean thin black outlines, lots of white "
            "space, no shading"
        ),
        negatives="photorealism, color fills, 3d render, photograph, clutter",
    ),
    "flat-vector": StylePreset(
        positive="flat vector illustration, bold simple shapes, limited pastel palette, soft rounded forms",
        negatives="photorealism, 3d render, gradients, photograph, texture, outlines",
    ),
    "chalkboard": StylePreset(
        positive="white chalk drawing on a dark chalkboard, sketchy hand-drawn lines, educational diagram feel",
        negatives="photorealism, color photo, 3d render, glossy",
    ),
    "watercolor": StylePreset(
        positive="soft watercolor illustration, gentle washes, muted earthy tones, visible paper texture",
        negatives="photorealism, 3d render, hard black outlines, neon, photograph",
    ),
}

DEFAULT_STYLE = PRESETS["doodle"]


def get_preset(style_id: str | None) -> StylePreset:
    return PRESETS.get(style_id or "doodle", DEFAULT_STYLE)


def list_presets() -> list[dict]:
    return [{"id": k, "description": v.positive} for k, v in PRESETS.items()]
