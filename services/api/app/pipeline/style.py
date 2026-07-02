"""Style presets + custom style extraction — the style-lock mechanism proven in the spike.

A single reusable style string is appended to every segment's image prompt so all generations
share one look. Users pick a preset OR upload reference image(s) and we extract a style string
from them via a vision LLM (the technique from the reference tutorial).
"""
from __future__ import annotations

import json

from app.config import settings
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


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return json.loads(t.strip())


def extract_style_preset(images: list[bytes]) -> StylePreset:
    """Derive a reusable StylePreset from reference image(s) via Gemini vision.

    Focuses on style characteristics only (not subject), so the result is reusable for any
    prompt — exactly the technique from the reference tutorial.
    """
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.gemini_api_key)
    instruction = (
        "Analyze the VISUAL STYLE of the reference image(s) for use in an AI image generator. "
        "Focus on style characteristics ONLY, not subject matter. Return ONLY JSON: "
        '{"positive": "short comma-separated style description to paste as a prompt", '
        '"negatives": "comma-separated things to avoid so the generator does not drift toward '
        'polished or photorealistic output"}'
    )
    parts = [types.Part.from_bytes(data=b, mime_type="image/png") for b in images]
    parts.append(instruction)
    resp = client.models.generate_content(model=settings.qa_model, contents=parts)
    v = _extract_json(resp.text)
    return StylePreset(positive=str(v.get("positive", "")).strip(), negatives=str(v.get("negatives", "")).strip())
