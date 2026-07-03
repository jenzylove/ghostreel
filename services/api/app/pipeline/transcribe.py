"""Hosted STT via genblaze-assemblyai — powers bring-your-own-voice + word-level captions.

Takes an audio URL (AssemblyAI fetches it) and returns the transcript text + word-level
timings. Needs ASSEMBLYAI_API_KEY.
"""
from __future__ import annotations

from app.config import settings


def transcribe_url(audio_url: str, model: str | None = None) -> dict:
    """Transcribe an audio URL -> {"text": str, "words": [{word, start, end}, ...]}."""
    from genblaze_assemblyai import AssemblyAIProvider
    from genblaze_core import Modality, Pipeline

    out = (
        Pipeline("transcribe")
        .step(
            AssemblyAIProvider(),
            model=model or settings.stt_model,
            prompt=audio_url,
            modality=Modality.TEXT,
        )
        .run()
    )
    run_obj = out[0] if isinstance(out, tuple) else getattr(out, "run", out)
    asset = run_obj.steps[0].assets[0]

    words = []
    audio_meta = getattr(asset, "audio", None)
    if audio_meta and getattr(audio_meta, "word_timings", None):
        words = [
            {"word": w.word, "start": float(w.start), "end": float(w.end)}
            for w in audio_meta.word_timings
        ]

    text = ""
    meta = getattr(asset, "metadata", None)
    if isinstance(meta, dict):
        text = meta.get("text", "")

    return {"text": text, "words": words}


def segment_words(words: list[dict], n: int) -> list[dict]:
    """Group word timings into n time-even beats: [{text, start, end}, ...].

    Beats are illustration windows for image generation; captions use the raw word timings
    separately, so they stay word-accurate regardless of this grouping.
    """
    if not words:
        return []
    span = words[-1]["end"] or 1.0
    buckets: list[list[str]] = [[] for _ in range(n)]
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        idx = min(n - 1, max(0, int(mid / span * n)))
        buckets[idx].append(w["word"])
    return [
        {"text": " ".join(buckets[i]).strip() or "(pause)", "start": i * span / n, "end": (i + 1) * span / n}
        for i in range(n)
    ]
