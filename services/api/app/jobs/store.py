"""B2-backed job store: persist / load / enumerate jobs.

The stored JSON at ghostreel/jobs/{job_id}.json IS the provenance manifest — topic, the full
script with per-segment prompts + QA verdicts, models used, and timestamps. Persisting after
every step is what makes jobs resumable across a crash/restart.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.models import Job, JobStatus
from app.storage.b2 import backend

_PREFIX = f"{settings.asset_prefix}/jobs"


def _key(job_id: str) -> str:
    return f"{_PREFIX}/{job_id}.json"


def save(job: Job) -> None:
    job.updated_at = datetime.now(timezone.utc).isoformat()
    backend().put(_key(job.job_id), job.model_dump_json().encode(), content_type="application/json")


def load(job_id: str) -> Job:
    return Job.model_validate_json(backend().get(_key(job_id)))


def list_incomplete() -> list[Job]:
    """Every job still PENDING/RUNNING — i.e. interrupted mid-flight and safe to resume."""
    jobs: list[Job] = []
    token: str | None = None
    while True:
        page = backend().list(prefix=f"{_PREFIX}/", continuation_token=token)
        for entry in page.entries:
            if not entry.key.endswith(".json"):
                continue
            try:
                job = Job.model_validate_json(backend().get(entry.key))
            except Exception:
                continue
            if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
                jobs.append(job)
        token = page.next_token
        if token is None:
            break
    return jobs
