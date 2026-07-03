"""Script generation: topic -> narration segments.

Uses genblaze's standalone `chat()` (the sample confirms chat is a function, not a pipeline
step). Google's chat reuses the already-working GEMINI_API_KEY. We instruct JSON and parse
defensively rather than relying on a structured-output param, so this stays portable.

FIRST-RUN CHECK: confirm `from genblaze_google import chat` takes (model, prompt=,
api_key=) and returns an object with `.text`. If not, it's a one-line fix here.
"""
from __future__ import annotations

import json

from genblaze_google import chat

from app.config import settings
from app.models import Script, Segment

_INSTRUCTION = """You are writing a short faceless YouTube video script about: {topic}

Break it into {n} sequential segments. Each segment must have:
- "narration": 1-2 spoken sentences (concise, engaging, no stage directions or labels)
- "visual": a short description of ONE still image that illustrates that narration

Return ONLY valid JSON in exactly this shape, no markdown, no commentary:
{{"segments": [{{"narration": "...", "visual": "..."}}]}}"""


def _extract_json(text: str) -> dict:
    t = text.strip()
    # Strip ```json ... ``` fences some models add despite instructions.
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return json.loads(t.strip())


def visuals_for_narration(narrations: list[str], topic: str = "") -> list[str]:
    """Given a video's narration beats (e.g. from a transcript), derive one still-image idea
    per beat in order. Used by the bring-your-own-voice flow. One LLM call."""
    about = f" about {topic}" if topic else ""
    listed = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(narrations))
    instruction = (
        f"Here are the narration beats of a short video{about}, in order. For EACH beat, give "
        "a concise description of ONE still image that illustrates it. Return ONLY JSON: "
        '{"visuals": ["...", "..."]} with exactly one entry per beat, same order.\n\n' + listed
    )
    resp = chat(settings.chat_model, prompt=instruction, api_key=settings.gemini_api_key)
    vis = _extract_json(resp.text).get("visuals", [])
    # Pad/truncate to match; fall back to the narration text itself if the LLM under-returns.
    while len(vis) < len(narrations):
        vis.append(narrations[len(vis)])
    return [str(v) for v in vis[: len(narrations)]]


def generate_script(topic: str, n: int = 6) -> Script:
    resp = chat(
        settings.chat_model,
        prompt=_INSTRUCTION.format(topic=topic, n=n),
        api_key=settings.gemini_api_key,
    )
    data = _extract_json(resp.text)
    segments = [
        Segment(index=i, narration=s["narration"], visual=s["visual"])
        for i, s in enumerate(data["segments"])
    ]
    if not segments:
        raise ValueError("script generation returned no segments")
    return Script(topic=topic, segments=segments)
