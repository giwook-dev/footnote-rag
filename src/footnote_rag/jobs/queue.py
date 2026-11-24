"""PostgreSQL job queue (``FOR UPDATE SKIP LOCKED``).

State machine::

    queued --claim--> running --ok--> succeeded
       ^                 |
       |   retryable &   |  permanent error, or retryable with attempts exhausted
       +-- attempts left +-------------------------------------------> failed
       ^
       +-- running with locked_at older than JOB_LOCK_TIMEOUT (stale recovery)

``attempts`` counts started executions (incremented on claim), so a worker crash also
consumes an attempt. Backoff after the n-th failed attempt: ``2**n * BASE_BACKOFF_SECONDS``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.db.models import Job


class JobError(Exception):
    retryable: bool = False


class RetryableJobError(JobError):
    retryable = True


class PermanentJobError(JobError):
    retryable = False


def enqueue(session: AsyncSession, *, type: str, payload: dict[str, Any], max_attempts: int) -> Job:
    """Add a job to the caller's transaction (no commit)."""
    job = Job(
        id=uuid.uuid4(),
        type=type,
        payload=payload,
        status="queued",
        attempts=0,
        max_attempts=max_attempts,
    )
    session.add(job)
    return job


async def claim_job(session: AsyncSession, worker_id: str) -> Job | None:
    """Atomically take the oldest runnable job. Commits the claim."""
    stmt = (
        select(Job)
        .where(Job.status == "queued", Job.run_after <= func.now())
        .order_by(Job.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = await session.scalar(stmt)
    if job is None:
        await session.rollback()
        return None
    job.status = "running"
    job.attempts = job.attempts + 1
    job.locked_at = func.now()
    job.locked_by = worker_id
    await session.commit()
    await session.refresh(job)
    return job


def backoff_seconds(attempts: int, base_seconds: float) -> float:
    return (2**attempts) * base_seconds


@dataclass
class FailureOutcome:
    status: str  # "queued" (retry scheduled) or "failed"
    attempts: int
    retry_in_seconds: float | None


async def mark_succeeded(session: AsyncSession, job_id: uuid.UUID, result: dict[str, Any]) -> None:
    """Mark success inside the caller's transaction (no commit)."""
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="succeeded",
            result=result,
            last_error=None,
            locked_at=None,
            locked_by=None,
            updated_at=func.now(),
        )
    )


async def mark_failed(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    error: str,
    retryable: bool,
    base_backoff_seconds: float,
) -> FailureOutcome:
    """Record a failure inside the caller's transaction (no commit)."""
    job = await session.get(Job, job_id, with_for_update=True)
    assert job is not None
    error = error[:4000]
    if retryable and job.attempts < job.max_attempts:
        delay = backoff_seconds(job.attempts, base_backoff_seconds)
        job.status = "queued"
        job.run_after = func.now() + timedelta(seconds=delay)
        job.last_error = error
        job.locked_at = None
        job.locked_by = None
        job.updated_at = func.now()
        return FailureOutcome("queued", job.attempts, delay)
    job.status = "failed"
    job.last_error = error
    job.locked_at = None
    job.locked_by = None
    job.updated_at = func.now()
    return FailureOutcome("failed", job.attempts, None)


async def recover_stale_jobs(session: AsyncSession, lock_timeout_seconds: int) -> int:
    """Return ``running`` jobs whose lock expired to ``queued``. Commits."""
    cutoff = func.now() - timedelta(seconds=lock_timeout_seconds)
    result = await session.execute(
        update(Job)
        .where(Job.status == "running", Job.locked_at < cutoff)
        .values(
            status="queued",
            locked_at=None,
            locked_by=None,
            run_after=func.now(),
            last_error="lock timeout: worker presumed dead",
            updated_at=func.now(),
        )
        .returning(Job.id)
    )
    recovered = len(result.all())
    await session.commit()
    return recovered
