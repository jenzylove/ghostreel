"""Backblaze B2 storage — sink + backend, mirroring the working sample and proven spike."""
from __future__ import annotations

from functools import lru_cache

from genblaze_core import KeyStrategy, ObjectStorageSink
from genblaze_s3 import S3StorageBackend

from app.config import settings


@lru_cache(maxsize=1)
def backend() -> S3StorageBackend:
    # Proven-spike path: credentials (B2_KEY_ID / B2_APP_KEY) are read from the environment
    # by genblaze-s3; we pass only the bucket. Region is auto-detected (the SDK self-corrects
    # on a 301 to the right B2 region). Singleton so we reuse one client across requests.
    return S3StorageBackend.for_backblaze(settings.b2_bucket_name)


@lru_cache(maxsize=8)
def sink(prefix: str | None = None) -> ObjectStorageSink:
    # Cached/reused across requests. The spike built ONE sink and reused it for all 3 runs;
    # rebuilding it per request re-pays any first-use B2 cost (manifest/index setup), which
    # grows with bucket contents - the prime suspect for the per-request slowdown. HIERARCHICAL
    # keys give runs/{date}/{run_id}/assets/{asset_id}.ext, the layout the spike confirmed.
    return ObjectStorageSink(
        backend(),
        prefix=prefix or settings.asset_prefix,
        key_strategy=KeyStrategy.HIERARCHICAL,
    )
