"""Backblaze B2 storage — sink, backend, and authenticated fetch-by-URL."""
from __future__ import annotations

from functools import lru_cache
from urllib.parse import unquote, urlparse

from genblaze_core import KeyStrategy, ObjectStorageSink
from genblaze_s3 import S3StorageBackend

from app.config import settings


@lru_cache(maxsize=1)
def backend() -> S3StorageBackend:
    # Proven-spike path: credentials (B2_KEY_ID / B2_APP_KEY) read from env; pass only the
    # bucket. Region auto-detected. Singleton so we reuse one client across requests
    # (rebuilding it per request re-paid a growing manifest-index cost — the Phase 0 slowdown).
    return S3StorageBackend.for_backblaze(settings.b2_bucket_name)


@lru_cache(maxsize=8)
def sink(prefix: str | None = None) -> ObjectStorageSink:
    # Cached/reused across requests. HIERARCHICAL keys give
    # runs/{date}/{run_id}/assets/{asset_id}.ext (confirmed in the bucket).
    return ObjectStorageSink(
        backend(),
        prefix=prefix or settings.asset_prefix,
        key_strategy=KeyStrategy.HIERARCHICAL,
    )


def key_from_url(url: str) -> str:
    """Recover the B2 object key from a durable Asset URL across the common URL shapes.

    Asset.url is durable + credential-free (there's no separate key field — the model's own
    docstring says to parse the key from the URL when the sink backend is known).
    """
    path = unquote(urlparse(url).path).lstrip("/")
    bucket = settings.b2_bucket_name
    if path.startswith(f"file/{bucket}/"):     # B2 "friendly" URL: /file/{bucket}/{key}
        return path[len(f"file/{bucket}/"):]
    if path.startswith(f"{bucket}/"):          # path-style: /{bucket}/{key}
        return path[len(bucket) + 1:]
    return path                                # virtual-hosted: /{key}


def get_by_url(url: str) -> bytes:
    """Fetch a private-bucket asset's bytes through the authenticated backend (no anon GET)."""
    return backend().get(key_from_url(url))
