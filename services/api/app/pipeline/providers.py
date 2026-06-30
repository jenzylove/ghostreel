"""Provider construction. Kept in one place so Phase 2 can add fallback_models cleanly."""
from __future__ import annotations

from genblaze_google import ImagenProvider


def image_provider() -> ImagenProvider:
    # Bare construction mirrors the working backblaze-labs sample and our proven WSL spike:
    # no output_dir (staging defaults to /tmp, which the sink allowlist permits on Linux);
    # the API key is read from GEMINI_API_KEY in the environment.
    #
    # Phase 2 TODO: add provider fallback via fallback_models=[...] on the .step() call
    # (e.g. a GMI/Flux image model) for resilience + to showcase Genblaze provider-swap.
    return ImagenProvider()
