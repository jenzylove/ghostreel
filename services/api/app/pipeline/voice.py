"""Voiceover generation: narration text -> audio asset in B2 (ElevenLabs via Genblaze)."""
from __future__ import annotations

from genblaze_core import Modality, Pipeline

from app.config import settings
from app.pipeline.providers import voice_provider
from app.storage.b2 import sink


def generate_voice(narration: str, voice_id: str | None = None) -> str | None:
    out = (
        Pipeline("ghostreel-voice")
        .step(
            voice_provider(),
            model=settings.tts_model,
            modality=Modality.AUDIO,
            prompt=narration,
            voice_id=voice_id or settings.voice_id,
        )
        # timeout bounds a hung TTS call (we saw a 2m13s connection timeout); max_retries
        # lets Genblaze self-heal transient failures — voice's equivalent of the image QA loop.
        .run(sink=sink(), timeout=90, max_retries=2)
    )
    try:
        run_obj = out[0] if isinstance(out, tuple) else getattr(out, "run", out)
        return run_obj.steps[0].assets[0].url
    except Exception:
        return None
