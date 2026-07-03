"""Hosted STT via genblaze-assemblyai — word-level timings for beat grouping and captions.

transcribe_url   — takes an audio URL (AssemblyAI fetches it), returns text + word timings.
group_into_beats — groups words into visual beat windows of ~beat_duration_s seconds,
                   preferring natural pauses over mid-phrase cuts.
"""
from __future__ import annotations

from app.config import settings
from app.models import Beat


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
    assets = run_obj.steps[0].assets
    if not assets:
        raise RuntimeError("AssemblyAI returned no assets — pipeline failed (check audio URL and API key)")
    asset = assets[0]

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


def group_into_beats(
    word_timings: list[dict], beat_duration_s: float | None = None
) -> list[Beat]:
    """Group word-level timings into visual beat windows.

    Closes a beat after ~beat_duration_s of audio. Prefers a natural pause (next word
    starts ≥300ms later) rather than cutting mid-phrase, up to 1.5× the target duration.
    """
    duration = beat_duration_s or settings.beat_duration_s
    if not word_timings:
        return []

    beats: list[Beat] = []
    beat_start = word_timings[0]["start"]
    current: list[dict] = []

    for i, w in enumerate(word_timings):
        current.append(w)
        elapsed = w["end"] - beat_start

        if elapsed >= duration:
            next_gap = (
                word_timings[i + 1]["start"] - w["end"]
                if i + 1 < len(word_timings) else 1.0
            )
            natural_pause = next_gap >= 0.3
            max_stretch = elapsed >= duration * 1.5

            if natural_pause or max_stretch:
                beats.append(Beat(
                    index=len(beats),
                    start_s=beat_start,
                    end_s=w["end"],
                    text=" ".join(ww["word"] for ww in current),
                ))
                beat_start = word_timings[i + 1]["start"] if i + 1 < len(word_timings) else w["end"]
                current = []

    if current:
        beats.append(Beat(
            index=len(beats),
            start_s=beat_start,
            end_s=current[-1]["end"],
            text=" ".join(w["word"] for w in current),
        ))

    return beats
