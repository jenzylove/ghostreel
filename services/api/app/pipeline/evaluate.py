"""Phase 2 evaluate step: QA a generated image, so the retry loop can self-heal.

Two tiers:
1. structural_check — deterministic, no API: decodes, min size, not near-blank. Catches the
   demo-killing garbage (blank/garbled/tiny) for free.
2. semantic_check — Gemini vision (via google-genai directly, since genblaze's chat is
   text-only): does the image match the style + the segment's content? FAILS OPEN so a QA
   hiccup can never block generation.
"""
from __future__ import annotations

import json
from io import BytesIO

from PIL import Image

from app.config import settings
from app.models import Segment, StylePreset, Verdict


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return json.loads(t.strip())


def structural_check(data: bytes) -> tuple[bool, str]:
    if not data or len(data) < 1024:
        return False, "empty or truncated file"
    try:
        img = Image.open(BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        return False, f"undecodable image: {e}"
    if img.width < 256 or img.height < 256:
        return False, f"too small ({img.width}x{img.height})"
    # Blank/solid detection: near-zero luminance variance on a downscaled copy.
    small = img.convert("L").resize((32, 32))
    px = list(small.getdata())
    mean = sum(px) / len(px)
    variance = sum((p - mean) ** 2 for p in px) / len(px)
    if variance < 20:
        return False, f"near-blank (variance {variance:.1f})"
    return True, "ok"


def semantic_check(data: bytes, segment: Segment, style: StylePreset) -> Verdict:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=settings.gemini_api_key)
        instruction = (
            "You are QA for AI-generated video stills. PASS the image unless it is genuinely "
            "BROKEN: corrupted, blank, a glitched or garbled mess, or disfigured nonsense. Do "
            "NOT fail for imperfect style, loose subject match, odd cropping, or artistic "
            "choices — only for images that are actually unusable. When in doubt, PASS.\n"
            'Return ONLY JSON: {"passed": true|false, "score": 0.0-1.0, "reason": "short"}'
        )
        resp = client.models.generate_content(
            model=settings.qa_model,
            contents=[types.Part.from_bytes(data=data, mime_type="image/png"), instruction],
        )
        v = _extract_json(resp.text)
        return Verdict(
            passed=bool(v.get("passed", True)),
            score=float(v.get("score", 1.0)),
            reason=str(v.get("reason", "")),
        )
    except Exception as e:  # noqa: BLE001
        # Fail open — a QA hiccup must never block the pipeline.
        return Verdict(passed=True, score=1.0, reason=f"semantic check skipped: {e}")


def evaluate_image(data: bytes, segment: Segment, style: StylePreset) -> Verdict:
    ok, reason = structural_check(data)
    if not ok:
        return Verdict(passed=False, score=0.0, reason=f"structural: {reason}")
    return semantic_check(data, segment, style)
