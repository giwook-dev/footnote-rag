"""Job queue: SKIP LOCKED claims, backoff, permanent failures, stale recovery, idempotent ingest."""

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import func, select, update

from footnote_rag.dart.client import FakeDartClient, classify_status
from footnote_rag.db.models import Chunk, Document, Job
from footnote_rag.jobs.handlers import JobContext
from footnote_rag.jobs.queue import (
    RetryableJobError,
    backoff_seconds,
    claim_job,
    enqueue,
    recover_stale_jobs,
)
from footnote_rag.jobs.worker import Worker


async def _enqueue(sessionmaker, n=1, type_="noop", max_attempts=3) -> list[uuid.UUID]:
    ids = []
    async with sessionmaker() as s:
        for _ in range(n):
            ids.append(enqueue(s, type=type_, payload={}, max_attempts=max_attempts).id)
        await s.commit()
    return ids


async def test_concurrent_claims_get_one_job_once(sessionmaker):
    [job_id] = await _enqueue(sessionmaker)

    async def claim(worker_id):
        async with sessionmaker() as s:
            return await claim_job(s, worker_id)

    results = await asyncio.gather(*(claim(f"w{i}") for i in range(5)))
    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1
    assert claimed[0].id == job_id and claimed[0].status == "running"
    assert claimed[0].attempts == 1


async def test_two_workers_process_each_job_exactly_once(sessionmaker, settings, fake_embedder):
    job_ids = await _enqueue(sessionmaker, n=12, type_="count")
    seen: list[tuple[str, uuid.UUID]] = []

    async def handler(ctx, job):
        await asyncio.sleep(0.01)
        seen.append((job.locked_by, job.id))
        async with ctx.sessionmaker() as s:
            await s.execute(update(Job).where(Job.id == job.id).values(status="succeeded"))
            await s.commit()
        return {}

    ctx = JobContext(sessionmaker, settings, fake_embedder, FakeDartClient())
    workers = [
        Worker(
            sessionmaker=sessionmaker, context=ctx, handlers={"count": handler}, worker_id=f"w{i}"
        )
        for i in range(2)
    ]

    async def drain(w):
        while await w.run_once():
            pass

    await asyncio.gather(*(drain(w) for w in workers))
    processed = [jid for _, jid in seen]
    assert sorted(processed) == sorted(job_ids)
    assert len(processed) == len(set(processed))


async def test_retryable_failure_backs_off_then_fails_after_max_attempts(
    sessionmaker, settings, fake_embedder
):
    [job_id] = await _enqueue(sessionmaker, type_="flaky", max_attempts=3)

    async def flaky(ctx, job):
        raise RetryableJobError("temporary upstream failure")

    w = Worker(
        sessionmaker=sessionmaker,
        context=JobContext(sessionmaker, settings, fake_embedder, FakeDartClient()),
        handlers={"flaky": flaky},
    )
    for attempt in (1, 2):
        assert await w.run_once()
        async with sessionmaker() as s:
            job = await s.get(Job, job_id)
            delay = (job.run_after - await s.scalar(select(func.now()))).total_seconds()
        assert job.status == "queued" and job.attempts == attempt
        assert job.last_error == "RetryableJobError: temporary upstream failure"
        expected = backoff_seconds(attempt, settings.base_backoff_seconds)  # 2**n * base
        assert expected - 2 < delay <= expected
        assert not await w.run_once(), "job must not run before run_after"
        async with sessionmaker() as s:  # fast-forward time
            await s.execute(update(Job).where(Job.id == job_id).values(run_after=func.now()))
            await s.commit()

    assert await w.run_once()  # third and last attempt
    async with sessionmaker() as s:
        job = await s.get(Job, job_id)
    assert job.status == "failed" and job.attempts == 3
    assert "temporary upstream failure" in job.last_error


async def test_backoff_is_exponential():
    assert [backoff_seconds(n, 5) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]


async def test_non_retryable_dart_error_fails_job_and_document_immediately(
    client, make_org, sessionmaker, settings, fake_embedder
):
    org = await make_org()
    rcept_no = "20260310123456"
    dart = FakeDartClient(documents={})  # every download -> status 014 (file missing)
    resp = await client.post("/documents/dart", json={"rcept_no": rcept_no}, headers=org.headers())
    assert resp.status_code == 202
    w = Worker(
        sessionmaker=sessionmaker, context=JobContext(sessionmaker, settings, fake_embedder, dart)
    )
    assert await w.run_once()
    async with sessionmaker() as s:
        job = await s.get(Job, uuid.UUID(resp.json()["job_id"]))
        doc = await s.get(Document, uuid.UUID(resp.json()["document_id"]))
    assert job.status == "failed" and job.attempts == 1
    assert "014" in job.last_error
    assert doc.status == "failed" and "014" in doc.error


async def test_retryable_dart_error_requeues_document(
    client, make_org, sessionmaker, settings, fake_embedder
):
    org = await make_org()

    class Limited(FakeDartClient):
        async def download_document(self, rcept_no):
            raise classify_status("020", "request limit exceeded")

    resp = await client.post(
        "/documents/dart", json={"rcept_no": "20260310654321"}, headers=org.headers()
    )
    w = Worker(
        sessionmaker=sessionmaker,
        context=JobContext(sessionmaker, settings, fake_embedder, Limited()),
    )
    assert await w.run_once()
    async with sessionmaker() as s:
        job = await s.get(Job, uuid.UUID(resp.json()["job_id"]))
        doc = await s.get(Document, uuid.UUID(resp.json()["document_id"]))
    assert job.status == "queued" and job.attempts == 1 and "020" in job.last_error
    assert doc.status == "queued"


async def test_parse_failure_is_permanent(client, make_org, sessionmaker, settings, fake_embedder):
    import io
    import zipfile

    org = await make_org()
    rcept_no = "20260310000777"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{rcept_no}.xml", "<DOCUMENT><BODY><P> </P></BODY></DOCUMENT>")
    dart = FakeDartClient(documents={rcept_no: buf.getvalue()})
    resp = await client.post("/documents/dart", json={"rcept_no": rcept_no}, headers=org.headers())
    w = Worker(
        sessionmaker=sessionmaker, context=JobContext(sessionmaker, settings, fake_embedder, dart)
    )
    await w.run_once()
    async with sessionmaker() as s:
        job = await s.get(Job, uuid.UUID(resp.json()["job_id"]))
    assert job.status == "failed" and job.attempts == 1
    assert "parse failed" in job.last_error


async def test_stale_running_job_is_recovered(sessionmaker, settings):
    [stale_id, fresh_id] = await _enqueue(sessionmaker, n=2)
    async with sessionmaker() as s:
        await s.execute(
            update(Job)
            .where(Job.id == stale_id)
            .values(
                status="running",
                attempts=1,
                locked_by="dead-worker",
                locked_at=func.now() - timedelta(seconds=settings.job_lock_timeout_seconds + 30),
            )
        )
        await s.execute(
            update(Job)
            .where(Job.id == fresh_id)
            .values(status="running", attempts=1, locked_by="alive", locked_at=func.now())
        )
        await s.commit()
    async with sessionmaker() as s:
        recovered = await recover_stale_jobs(s, settings.job_lock_timeout_seconds)
    assert recovered == 1
    async with sessionmaker() as s:
        stale = await s.get(Job, stale_id)
        fresh = await s.get(Job, fresh_id)
    assert stale.status == "queued" and stale.locked_by is None
    assert fresh.status == "running"


async def test_reingest_does_not_duplicate_chunks(client, make_org, sessionmaker, worker, ingest):
    org = await make_org()
    doc_id = await ingest(org, "20260310900003")

    async def chunk_count():
        async with sessionmaker() as s:
            return await s.scalar(
                select(func.count())
                .select_from(Chunk)
                .where(Chunk.document_id == uuid.UUID(doc_id))
            )

    first = await chunk_count()
    assert first > 0
    # Re-run the same import job (as a retry would) twice.
    async with sessionmaker() as s:
        for _ in range(2):
            enqueue(
                s,
                type="import_dart",
                payload={
                    "document_id": doc_id,
                    "org_id": str(org.org_id),
                    "rcept_no": "20260310900003",
                },
                max_attempts=3,
            )
        await s.commit()
    while await worker.run_once():
        pass
    assert await chunk_count() == first
    async with sessionmaker() as s:
        indexes = list(
            await s.scalars(
                select(Chunk.chunk_index)
                .where(Chunk.document_id == uuid.UUID(doc_id))
                .order_by(Chunk.chunk_index)
            )
        )
    assert indexes == list(range(first))
