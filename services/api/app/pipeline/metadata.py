"""Phase 5: YouTube upload package — title, description, tags, and thumbnail.

Generated right after the script so the data is available during the review gate.
Thumbnail is a separate Imagen call (adds to Genblaze usage) done before assembly.
"""
from __future__ import annotations

import json

from genblaze_google import chat

from app.config import settings
from app.models import Script, StylePreset
from app.pipeline.visuals import generate_image


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return json.loads(t.strip())


def generate_yt_metadata(script: Script) -> dict:
    """One LLM call → upload-ready YouTube title, description, and tags."""
    narrations = "\n".join(f"{i + 1}. {s.narration}" for i, s in enumerate(script.segments))
    prompt = (
        f"You are a YouTube SEO specialist. Create upload-ready metadata for a short faceless "
        f"video about '{script.topic}'.\n\nScript:\n{narrations}\n\n"
        "Return ONLY valid JSON (no markdown fences):\n"
        '{"title": "under 60 chars, compelling and accurate — no ALL CAPS or excessive punctuation", '
        '"description": "3-4 short engaging paragraphs about the video content; end with a call to action to like and subscribe", '
        '"tags": ["15 relevant tags mixing broad and specific — no # symbol"]}'
    )
    resp = chat(settings.chat_model, prompt=prompt, api_key=settings.gemini_api_key)
    try:
        data = _extract_json(resp.text)
        return {
            "title": str(data.get("title", script.topic))[:100],
            "description": str(data.get("description", "")),
            "tags": [str(t) for t in (data.get("tags") or [])[:20]],
        }
    except Exception:
        return {"title": script.topic, "description": "", "tags": []}


def generate_thumbnail(topic: str, style: StylePreset) -> str | None:
    """Extra Imagen call: a striking hero image optimised for a YouTube grid thumbnail."""
    subject = (
        f"YouTube thumbnail hero image for a video about '{topic}': "
        "dramatic composition, bold contrast, cinematic lighting, visually striking, "
        "no text overlays, designed to catch the eye in a YouTube search grid"
    )
    result = generate_image(style.apply(subject))
    return result.get("asset_url")
