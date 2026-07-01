"""Provider construction. Kept in one place so Phase 2 can add fallback_models cleanly."""
from __future__ import annotations

from genblaze_elevenlabs import ElevenLabsTTSProvider
from genblaze_google import ImagenProvider

from app.config import settings


def image_provider() -> ImagenProvider:
    # Bare construction mirrors the working sample + our proven WSL spike: no output_dir
    # (staging defaults to /tmp on Linux); API key read from GEMINI_API_KEY in the env.
    # Phase 2 TODO: add fallback_models=[...] on the .step() call (e.g. a GMI/Flux model).
    return ImagenProvider()


def voice_provider() -> ElevenLabsTTSProvider:
    # api_key passed explicitly to avoid env-var-name guessing across adapters. No output_dir
    # -> stages to /tmp on Linux, matching the proven Imagen path.
    return ElevenLabsTTSProvider(api_key=settings.eleven_api_key)
