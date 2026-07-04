"""B2-backed job store: persist / load / enumerate jobs.

The stored JSON at ghostreel/jobs/{job_id}.json IS the provenance manifest — topic, the full
script with per-segment prompts + QA verdicts, models used, and timestamps. Persisting after
every step is what makes jobs resumable across a crash/restart.

Session index: ghostreel/sessions/{user_id}.json holds a lightweight list of job summaries
for one anonymous browser session. One B2 read returns a user's full history — no bucket scan.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config import settings
from app.models import Job, JobStatus
from app.storage.b2 import backend

_PREFIX = f"{settings.asset_prefix}/jobs"
_SESSION_PREFIX = f"{settings.asset_prefix}/sessions"


def _key(job_id: str) -> str:
    return f"{_PREFIX}/{job_id}.json"


def save(job: Job) -> None:
    job.updated_at = datetime.now(timezone.utc).isoformat()
    backend().put(_key(job.job_id), job.model_dump_json().encode(), content_type="application/json")


def load(job_id: str) -> Job:
    return Job.model_validate_json(backend().get(_key(job_id)))


def _session_key(user_id: str) -> str:
    return f"{_SESSION_PREFIX}/{user_id}.json"


def _job_summary(job: Job) -> dict:
    return {
        "job_id": job.job_id,
        "topic": job.topic,
        "status": job.status.value,
        "step": job.step,
        "created_at": job.created_at,
        "has_video": bool(job.video_url),
        "error": job.error,
    }


def update_session_index(job: Job) -> None:
    """Upsert this job's summary into the session's index file.

    Called at job creation and at key status transitions (awaiting_review, done, failed)
    so the history list stays accurate without a full bucket scan.
    """
    if not job.user_id or job.user_id == "anon":
        return
    key = _session_key(job.user_id)
    try:
        index: list[dict] = json.loads(backend().get(key))
    except Exception:
        index = []
    summary = _job_summary(job)
    for i, entry in enumerate(index):
        if entry.get("job_id") == job.job_id:
            index[i] = summary
            break
    else:
        index.append(summary)
    index.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    backend().put(_session_key(job.user_id), json.dumps(index).encode(), content_type="application/json")


def list_by_session(user_id: str, limit: int = 20) -> list[dict]:
    """Return the session's job summary list — one B2 read, no bucket scan."""
    if not user_id or user_id == "anon":
        return []
    try:
        return json.loads(backend().get(_session_key(user_id)))[:limit]
    except Exception:
        return []


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
