"""B2-backed job store: persist / load / enumerate jobs.

The stored JSON at ghostreel/jobs/{job_id}.json IS the provenance manifest — topic, the full
script with per-segment prompts + QA verdicts, models used, and timestamps. Persisting after
every step is what makes jobs resumable across a crash/restart.

Session index: ghostreel/sessions/{user_id}.json holds a lightweight list of job summaries
for one anonymous browser session. One B2 read returns a user's full history — no bucket scan.

Pending index: ghostreel/pending.json tracks the IDs of PENDING/RUNNING jobs. On startup,
resume_pending reads only this index + those specific job JSONs instead of scanning the whole
bucket. Bounded to O(active jobs) instead of O(all jobs ever).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config import settings
from app.models import Job, JobStatus
from app.storage.b2 import backend

_PREFIX = f"{settings.asset_prefix}/jobs"
_SESSION_PREFIX = f"{settings.asset_prefix}/sessions"
_PENDING_KEY = f"{settings.asset_prefix}/pending.json"


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


def _read_pending_ids() -> list[str]:
    try:
        return json.loads(backend().get(_PENDING_KEY))
    except Exception:
        return []


def _write_pending_ids(ids: list[str]) -> None:
    backend().put(_PENDING_KEY, json.dumps(ids).encode(), content_type="application/json")


def add_pending(job_id: str) -> None:
    """Mark a job as needing resumption on the next restart."""
    ids = _read_pending_ids()
    if job_id not in ids:
        ids.append(job_id)
    _write_pending_ids(ids)


def remove_pending(job_id: str) -> None:
    """Remove a job from the pending index when it reaches a terminal or paused state."""
    _write_pending_ids([i for i in _read_pending_ids() if i != job_id])


def list_incomplete() -> list[Job]:
    """Resume scan: read the pending index then fetch only those job JSONs.

    O(pending jobs) instead of O(all jobs ever) — the original full bucket scan
    became prohibitive as the total job count grew.
    """
    jobs: list[Job] = []
    for job_id in _read_pending_ids():
        try:
            job = load(job_id)
        except Exception:
            continue
        if job.status in (JobStatus.PENDING, JobStatus.RUNNING):
            jobs.append(job)
    return jobs
