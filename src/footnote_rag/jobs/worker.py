"""Polling worker: ``python -m footnote_rag.jobs.worker``.

Several workers can run at once; ``FOR UPDATE SKIP LOCKED`` guarantees one claimer per job.
On SIGTERM/SIGINT the worker finishes the job in hand and exits.
"""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import uuid
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from footnote_rag.audit.service import Actions, record
from footnote_rag.config import Settings, get_settings
from footnote_rag.db.models import Document, Job
from footnote_rag.jobs.handlers import HANDLERS, Handler, JobContext
from footnote_rag.jobs.queue import (
    JobError,
    claim_job,
    mark_failed,
    recover_stale_jobs,
)
from footnote_rag.observability import configure_logging, get_logger

log = get_logger(__name__)


class Worker:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        context: JobContext,
        handlers: Mapping[str, Handler] = HANDLERS,
        worker_id: str | None = None,
    ) -> None:
        self.sessionmaker = sessionmaker
        self.context = context
        self.handlers = handlers
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.settings: Settings = context.settings
        self._stop = asyncio.Event()

    def request_stop(self) -> None:
        self._stop.set()

    async def recover(self) -> int:
        async with self.sessionmaker() as session:
            n = await recover_stale_jobs(session, self.settings.job_lock_timeout_seconds)
        if n:
            log.warning("jobs.recovered_stale", count=n)
        return n

    async def run_once(self) -> bool:
        """Claim and execute at most one job. Returns True if a job was processed."""
        async with self.sessionmaker() as session:
            job = await claim_job(session, self.worker_id)
        if job is None:
            return False
        log.info("job.claimed", job_id=str(job.id), type=job.type, attempt=job.attempts)
        await self._execute(job)
        return True

    async def _execute(self, job: Job) -> None:
        handler = self.handlers.get(job.type)
        try:
            if handler is None:
                raise _Permanent(f"unknown job type {job.type!r}")
            if job.attempts > job.max_attempts:
                raise _Permanent("max attempts exceeded")
            result = await handler(self.context, job)
            log.info("job.succeeded", job_id=str(job.id), type=job.type, **_loggable(result))
        except Exception as exc:  # noqa: BLE001 - every failure must be recorded
            retryable = exc.retryable if isinstance(exc, JobError | _Permanent) else False
            if not isinstance(exc, JobError | _Permanent):
                log.exception("job.unexpected_error", job_id=str(job.id))
            await self._record_failure(job, f"{type(exc).__name__}: {exc}", retryable)

    async def _record_failure(self, job: Job, error: str, retryable: bool) -> None:
        async with self.sessionmaker() as session:
            outcome = await mark_failed(
                session,
                job.id,
                error=error,
                retryable=retryable,
                base_backoff_seconds=self.settings.base_backoff_seconds,
            )
            doc_id = job.payload.get("document_id")
            doc = await session.get(Document, uuid.UUID(doc_id)) if doc_id else None
            if doc is not None:
                if outcome.status == "failed":
                    doc.status = "failed"
                    doc.error = error[:2000]
                else:
                    doc.status = "queued"
                    doc.error = f"retry scheduled: {error[:1900]}"
                record(
                    session,
                    action=Actions.INGEST_FAILED
                    if outcome.status == "failed"
                    else Actions.INGEST_RETRY_SCHEDULED,
                    org_id=doc.org_id,
                    actor_user_id=doc.created_by,
                    target_type="document",
                    target_id=doc.id,
                    metadata={
                        "job_id": str(job.id),
                        "job_type": job.type,
                        "attempts": outcome.attempts,
                        "retryable": retryable,
                        "error": error[:500],
                        "retry_in_seconds": outcome.retry_in_seconds,
                    },
                )
            await session.commit()
        log.warning(
            "job.failed",
            job_id=str(job.id),
            type=job.type,
            outcome=outcome.status,
            attempts=outcome.attempts,
            retry_in_seconds=outcome.retry_in_seconds,
            error=error[:500],
        )

    async def run_forever(self) -> None:
        log.info("worker.started", worker_id=self.worker_id)
        await self.recover()
        loops = 0
        while not self._stop.is_set():
            processed = await self.run_once()
            loops += 1
            if loops % 60 == 0:
                await self.recover()
            if not processed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.settings.worker_poll_interval_seconds
                    )
                except TimeoutError:
                    pass
        log.info("worker.stopped", worker_id=self.worker_id)


class _Permanent(Exception):
    retryable = False


def _loggable(result: dict) -> dict:
    return {k: v for k, v in result.items() if isinstance(v, int | float | str)}


async def _main() -> None:
    from footnote_rag.db.session import dispose_engine, get_sessionmaker
    from footnote_rag.providers import make_dart_client, make_embedder

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    sm = get_sessionmaker()
    ctx = JobContext(
        sessionmaker=sm,
        settings=settings,
        embedder=make_embedder(settings),
        dart_client=make_dart_client(settings),
    )
    worker = Worker(sessionmaker=sm, context=ctx)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except NotImplementedError:  # Windows: fall back to signal.signal
            signal.signal(sig, lambda *_: worker.request_stop())
    try:
        await worker.run_forever()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    asyncio.run(_main())
