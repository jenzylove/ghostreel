"""Image generation — the proven spike, ported into a callable service function."""
from __future__ import annotations

import time

from genblaze_core import Modality, Pipeline

from app.config import settings
from app.pipeline.providers import image_provider
from app.storage.b2 import sink


def generate_image(prompt: str) -> dict:
    """Generate one image from a prompt and upload it to B2. Returns asset url + timing.

    Phase 0 seam: synchronous, single image. Phase 1+ moves this behind the async job
    runner (jobs/runner.py) and fans it out per script segment, with the style string
    appended to every prompt and an evaluate-retry loop around each generation.
    """
    t0 = time.time()
    out = (
        Pipeline("ghostreel-seam")
        .step(
            image_provider(),
            model=settings.image_model,
            prompt=prompt,
            modality=Modality.IMAGE,
        )
        .run(sink=sink())
    )
    took = round(time.time() - t0, 1)

    # The SDK's .run() return shape differs across docs (object with .run vs a
    # (run, manifest) tuple); probe defensively, same as the spike, so a layout surprise
    # can't make a real success look like a failure.
    asset_url = None
    try:
        run_obj = out[0] if isinstance(out, tuple) else getattr(out, "run", out)
        asset_url = run_obj.steps[0].assets[0].url
    except Exception:
        asset_url = None

    return {
        "asset_url": asset_url,
        "took_s": took,
        "raw": None if asset_url else repr(out),
    }
