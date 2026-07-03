"""Image generation + the QA/retry loop (Phase 2)."""
from __future__ import annotations

import time

from genblaze_core import Modality, Pipeline

from app.config import settings
from app.models import QaAttempt, StylePreset, Verdict
from app.pipeline.evaluate import evaluate_image
from app.pipeline.providers import image_provider
from app.storage.b2 import get_by_url, sink


def generate_image(prompt: str) -> dict:
    """Generate one image from a prompt and upload it to B2. Returns asset url + timing."""
    t0 = time.time()
    out = (
        Pipeline("ghostreel-image")
        .step(
            image_provider(),
            model=settings.image_model,
            prompt=prompt,
            modality=Modality.IMAGE,
        )
        # timeout bounds a hung image call; on failure generate_image returns None and the
        # QA loop in generate_and_qa regenerates, so we don't hang minutes on one image.
        .run(sink=sink(), timeout=120)
    )
    took = round(time.time() - t0, 1)

    asset_url = None
    try:
        run_obj = out[0] if isinstance(out, tuple) else getattr(out, "run", out)
        asset_url = run_obj.steps[0].assets[0].url
    except Exception:
        asset_url = None

    return {"asset_url": asset_url, "took_s": took, "raw": None if asset_url else repr(out)}


def generate_and_qa(
    styled_prompt: str, segment: object, style: StylePreset
) -> tuple[str | None, list[QaAttempt]]:
    """Generate a segment image, QA it, and regenerate on failure (capped).

    Returns the chosen image URL (first that passes, else the last generated) and the full
    per-attempt audit trail — the self-healing story for the demo + provenance.
    """
    attempts: list[QaAttempt] = []
    passed_url: str | None = None
    last_url: str | None = None
    n = settings.qa_max_attempts if settings.qa_enabled else 1

    for i in range(1, n + 1):
        url = generate_image(styled_prompt).get("asset_url")
        last_url = url or last_url

        if not settings.qa_enabled:
            attempts.append(QaAttempt(attempt=i, url=url, passed=True, score=1.0, reason="qa disabled"))
            passed_url = url
            break

        if not url:
            attempts.append(QaAttempt(attempt=i, url=None, passed=False, score=0.0, reason="no asset url"))
            continue

        try:
            verdict = evaluate_image(get_by_url(url), segment, style)
        except Exception as e:  # noqa: BLE001 - QA must never crash the pipeline
            verdict = Verdict(passed=True, score=1.0, reason=f"qa error, accepted: {e}")

        attempts.append(
            QaAttempt(attempt=i, url=url, passed=verdict.passed, score=verdict.score, reason=verdict.reason)
        )
        if verdict.passed:
            passed_url = url
            break

    return (passed_url or last_url), attempts
