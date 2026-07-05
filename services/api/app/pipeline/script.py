"""Script generation: topic + target length -> one continuous narration text.

One LLM call produces a single spoken-word piece (600-900 words for a 5-8 min video).
Visual descriptions per beat are derived separately AFTER word timings are known,
so they map to actual on-screen moments rather than arbitrary equal-length chunks.
"""
from __future__ import annotations

import json

from genblaze_google import chat

from app.config import settings
from app.models import Beat, Script


def _extract_json(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):
        t = t[3:]
        if t[:4].lower() == "json":
            t = t[4:]
        t = t.rsplit("```", 1)[0]
    return json.loads(t.strip())


def generate_script(topic: str, target_minutes: float = 4.0) -> Script:
    """Generate a single continuous narration (~130 wpm pace) for a faceless video."""
    target_words = int(target_minutes * 130)
    prompt = (
        f"Write a single continuous spoken narration for a faceless YouTube video about: {topic}\n\n"
        f"Target: approximately {target_words} words ({target_minutes:.0f} min at natural speaking pace).\n\n"
        "Style rules — follow every one of these:\n"
        "- Open with the most surprising or counterintuitive fact about the topic. No warm-up.\n"
        "- Short sentences. 20 words maximum. Vary sentence length for rhythm.\n"
        "- Write how a smart person talks, not how they write an essay. Conversational and direct.\n"
        "- Active voice only. No passive constructions.\n"
        "- Zero filler phrases — cut any of these on sight: 'it's important to note', "
        "'throughout history', 'in conclusion', 'one might argue', 'it is worth mentioning', "
        "'as we can see', 'in today's world', 'needless to say', 'of course', 'simply put', "
        "'at the end of the day', 'it goes without saying'.\n"
        "- No section headers, bullet points, labels, or speaker cues.\n"
        "- No opening hook like 'welcome' or 'today we're talking about'.\n"
        "- No sign-off, outro, or call-to-action at the end.\n"
        "- Return ONLY the narration text, nothing else."
    )
    resp = chat(settings.chat_model, prompt=prompt, api_key=settings.gemini_api_key)
    narration = resp.text.strip()
    if not narration:
        raise ValueError("script generation returned empty narration")
    return Script(topic=topic, narration=narration)


def derive_beat_visuals(beats: list[Beat], topic: str = "") -> list[str]:
    """One LLM call: given each beat's spoken text, return one visual description per beat.

    Derives what the camera/image should show during each window — purely visual,
    no text references, optimised for still-image generation.
    """
    about = f" about {topic}" if topic else ""
    listed = "\n".join(f"{i + 1}. {b.text}" for i, b in enumerate(beats))
    instruction = (
        f"For a faceless YouTube video{about}, here are the narration segments in order. "
        "For EACH segment, write a concise description of ONE still image that visually "
        "illustrates what is being said — no text in the image, purely visual. "
        'Return ONLY JSON: {"visuals": ["...", "..."]} same count and order as the input.\n\n'
        + listed
    )
    resp = chat(settings.chat_model, prompt=instruction, api_key=settings.gemini_api_key)
    try:
        vis = _extract_json(resp.text).get("visuals", [])
    except Exception:
        vis = []
    while len(vis) < len(beats):
        vis.append(beats[len(vis)].text)
    return [str(v) for v in vis[: len(beats)]]
