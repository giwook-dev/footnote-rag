"""Embedding rate limits, per-batch retries, the shared embedding cache and ingest progress.

No network: the Voyage client is a fake object, sleeps and clocks are fake, and the cache is
the real ``embedding_cache`` table in the test Postgres.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import voyageai.error as verr
from sqlalchemy import func, select, update
from structlog.testing import capture_logs

from footnote_rag.dart.client import FakeDartClient
from footnote_rag.db.models import Chunk, Document, EmbeddingCache, Job
from footnote_rag.jobs.handlers import JobContext, JobProgress
from footnote_rag.jobs.queue import claim_job, enqueue
from footnote_rag.jobs.worker import Worker
from footnote_rag.retrieval import embedder as emb
from footnote_rag.retrieval.embedder import (
    EmbeddingError,
    FakeEmbedder,
    RateLimiter,
    VoyageEmbedder,
    retry_delay,
)
from footnote_rag.retrieval.embedding_cache import CachedEmbedder, PgEmbeddingCache, cache_key

SECRET_KEY = "pa-test-secret-key-should-never-be-logged"
PAYMENT_MSG = (
    "You have not yet added your payment method in the billing page and will have reduced "
    "rate limits of 3 RPM and 10K TPM."
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ---------------------------------------------------------------- RateLimiter


async def test_rate_limiter_waits_for_rpm_window():
    clock = FakeClock()
    limiter = RateLimiter(rpm=3, clock=clock, sleep=clock.sleep)
    for _ in range(3):
        assert await limiter.acquire(10) == 0
    assert limiter.delay_for(10) == pytest.approx(60.0)
    assert await limiter.acquire(10) == pytest.approx(60.0)
    assert clock.sleeps == [pytest.approx(60.0)]


async def test_rate_limiter_waits_for_tpm_budget():
    clock = FakeClock()
    limiter = RateLimiter(tpm=9000, clock=clock, sleep=clock.sleep)
    await limiter.acquire(5000)
    clock.now += 10
    await limiter.acquire(3000)  # 8000 in window: fits
    assert clock.sleeps == []
    # 8000 + 2000 > 9000: the first request (5000) must leave the window -> wait 50 s.
    assert limiter.delay_for(2000) == pytest.approx(50.0)
    await limiter.acquire(2000)
    assert clock.sleeps == [pytest.approx(50.0)]
    # A request larger than the whole budget goes alone, once the window is empty.
    clock.now += 120
    assert limiter.delay_for(20_000) == 0
    await limiter.acquire(20_000)
    assert limiter.delay_for(1) == pytest.approx(60.0)


async def test_rate_limiter_never_exceeds_limits_over_a_long_run():
    clock = FakeClock()
    rpm, tpm = 3, 9000
    limiter = RateLimiter(rpm=rpm, tpm=tpm, clock=clock, sleep=clock.sleep)
    rng = random.Random(7)
    sent: list[tuple[float, int]] = []
    for _ in range(60):
        tokens = rng.randint(500, 6000)
        await limiter.acquire(tokens)
        sent.append((clock.now, tokens))
        clock.now += rng.choice([0, 0, 1, 5])
    for start, _ in sent:
        window = [t for at, t in sent if start <= at < start + 60]
        assert len(window) <= rpm
        assert sum(window) <= tpm
    assert clock.sleeps, "the limiter must have waited at least once"


async def test_rate_limiter_disabled_by_default():
    clock = FakeClock()
    limiter = RateLimiter(clock=clock, sleep=clock.sleep)
    for _ in range(100):
        await limiter.acquire(100_000)
    assert clock.sleeps == []


def test_retry_delay_is_exponential_capped_with_jitter():
    assert [retry_delay(n, 20, 120, 0.0) for n in range(5)] == [20, 40, 80, 120, 120]
    assert retry_delay(0, 20, 120, 0.999) == pytest.approx(20 * 1.0999)


def test_tpm_limit_caps_the_batch_token_budget(settings):
    s = settings.model_copy(update={"embed_tpm_limit": 9000})
    assert s.embed_effective_batch_max_tokens == 9000
    s = settings.model_copy(update={"embed_tpm_limit": None})
    assert s.embed_effective_batch_max_tokens == settings.embed_batch_max_tokens


# ---------------------------------------------------------------- VoyageEmbedder


class FakeVoyageClient:
    """Records every request; ``failures[i]`` lists exceptions to raise for the i-th batch."""

    def __init__(self, failures: dict[str, list[Exception]] | None = None) -> None:
        self.failures = failures or {}
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts, model=None, input_type=None, output_dimension=None):
        self.calls.append(tuple(texts))
        pending = self.failures.get(texts[0])
        if pending:
            raise pending.pop(0)
        return SimpleNamespace(
            embeddings=[[float(len(t)), 0.0, 0.0, 1.0] for t in texts], total_tokens=len(texts)
        )


def _voyage(client, clock, **kw) -> VoyageEmbedder:
    return VoyageEmbedder(
        api_key=SECRET_KEY,
        dim=4,
        max_texts=2,
        client=client,
        sleep=clock.sleep,
        rand=lambda: 0.0,
        **kw,
    )


TEXTS = ["a1", "a2", "b1", "b2", "c1"]  # max_texts=2 -> batches a*, b*, c*


async def test_rate_limited_batch_is_retried_alone_and_the_job_does_not_fail():
    clock = FakeClock()
    client = FakeVoyageClient({"b1": [verr.RateLimitError("slow down"), verr.TryAgain("busy")]})
    e = _voyage(client, clock, retry_base_seconds=20, retry_max_seconds=120)
    r = await e.embed_documents(TEXTS)
    assert len(r.vectors) == 5
    assert client.calls == [("a1", "a2"), ("b1", "b2"), ("b1", "b2"), ("b1", "b2"), ("c1",)]
    assert clock.sleeps == [20, 40]  # min(base * 2^n, max), jitter fixed at 0


async def test_exhausted_batch_retries_become_a_retryable_job_error():
    clock = FakeClock()
    client = FakeVoyageClient({"b1": [verr.RateLimitError("slow down") for _ in range(10)]})
    e = _voyage(client, clock, max_retries=3)
    stored: list[list[str]] = []

    async def on_batch(batch, result):
        stored.append(batch)

    with pytest.raises(EmbeddingError) as info:
        await e.embed_documents(TEXTS, on_batch=on_batch)
    assert info.value.retryable is True and info.value.kind == "rate_limited"
    assert "한도 초과" in str(info.value) and "재시도 3회 소진" in str(info.value)
    assert client.calls.count(("b1", "b2")) == 4  # first try + 3 retries
    assert ("c1",) not in client.calls
    assert stored == [["a1", "a2"]]  # the batch before the failure was handed over
    assert clock.sleeps == [20, 40, 80]


async def test_non_retryable_error_fails_immediately():
    clock = FakeClock()
    client = FakeVoyageClient({"a1": [verr.AuthenticationError("bad key")]})
    e = _voyage(client, clock)
    with pytest.raises(EmbeddingError) as info:
        await e.embed_documents(TEXTS)
    assert info.value.retryable is False
    assert client.calls == [("a1", "a2")] and clock.sleeps == []


async def test_batches_wait_on_the_rate_limiter():
    clock = FakeClock()
    limiter = RateLimiter(rpm=1, clock=clock, sleep=clock.sleep)
    e = _voyage(FakeVoyageClient(), clock, rate_limiter=limiter)
    await e.embed_documents(TEXTS)
    assert clock.sleeps == [pytest.approx(60.0), pytest.approx(60.0)]


async def test_payment_method_429_warns_once_without_the_key(monkeypatch):
    emb.reset_reduced_limits_warning()
    clock = FakeClock()
    client = FakeVoyageClient(
        {
            "a1": [verr.RateLimitError(PAYMENT_MSG), verr.RateLimitError(PAYMENT_MSG)],
            "b1": [verr.RateLimitError(PAYMENT_MSG)],
        }
    )
    e = _voyage(client, clock)
    with capture_logs() as logs:
        await e.embed_documents(TEXTS)
        await e.embed_documents(TEXTS)  # same process: no second warning
    warnings = [x for x in logs if x["event"] == "embedding.provider_reduced_limits"]
    assert len(warnings) == 1
    assert "EMBED_RPM_LIMIT" in warnings[0]["hint"] and "결제 수단" in warnings[0]["hint"]
    assert SECRET_KEY not in repr(logs)
    assert PAYMENT_MSG not in repr(logs)  # the raw provider message is not logged either
    emb.reset_reduced_limits_warning()


async def test_payment_method_error_is_flagged_for_the_document_error():
    emb.reset_reduced_limits_warning()
    clock = FakeClock()
    client = FakeVoyageClient({"a1": [verr.RateLimitError(PAYMENT_MSG)]})
    e = _voyage(client, clock, max_retries=0)
    with capture_logs():
        with pytest.raises(EmbeddingError) as info:
            await e.embed_documents(TEXTS)
    assert info.value.payment_method_missing
    assert "임베딩 제공자 요청 한도 초과(결제 수단 미등록 가능성)" in str(info.value)
    emb.reset_reduced_limits_warning()


# ---------------------------------------------------------------- migration


def _alembic(*args: str) -> str:
    import os
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    proc = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout + proc.stderr


async def test_upgrade_from_0001_adds_embedding_cache_to_an_existing_db(
    client, make_org, ingest, sessionmaker
):
    import asyncio

    org = await make_org()
    await ingest(org, "20260310900001")  # an existing DB with documents, chunks and jobs
    await asyncio.to_thread(_alembic, "downgrade", "0001_initial")
    async with sessionmaker() as s:
        exists = await s.scalar(select(func.to_regclass("embedding_cache")))
    assert exists is None
    await asyncio.to_thread(_alembic, "upgrade", "head")
    assert "0002_embedding_cache" in await asyncio.to_thread(_alembic, "current")
    async with sessionmaker() as s:
        assert await s.scalar(select(func.count()).select_from(EmbeddingCache)) == 0
        assert await s.scalar(select(func.count()).select_from(Chunk)) > 0


# ---------------------------------------------------------------- cache


async def _cache_rows(sessionmaker) -> int:
    async with sessionmaker() as s:
        return await s.scalar(select(func.count()).select_from(EmbeddingCache))


def _document_texts_sent(fake: FakeEmbedder) -> int:
    return sum(n for kind, n in fake.calls if kind == "document")


async def _job_for(sessionmaker, job_id: str) -> Job:
    async with sessionmaker() as s:
        return await s.get(Job, uuid.UUID(job_id))


async def test_second_ingest_uses_the_cache_only(client, make_org, ingest, worker, sessionmaker):
    fake = worker.context.embedder
    org = await make_org()
    doc_id = await ingest(org, "20260310900001")
    async with sessionmaker() as s:
        first = await s.scalar(select(Job).where(Job.payload["document_id"].astext == doc_id))
    chunks = first.result["chunk_count"]
    assert first.result["embedding_cache_hits"] == 0
    assert first.result["embedding_cache_misses"] == chunks
    assert first.result["embedding_tokens"] > 0
    sent_before = _document_texts_sent(fake)

    resp = await client.post(f"/documents/{doc_id}/reprocess", headers=org.headers("admin"))
    assert resp.status_code == 202
    while await worker.run_once():
        pass
    second = await _job_for(sessionmaker, resp.json()["job_id"])
    assert second.status == "succeeded"
    assert _document_texts_sent(fake) == sent_before  # nothing sent to the embedder
    assert second.result["embedding_cache_hits"] == chunks
    assert second.result["embedding_cache_misses"] == 0
    assert second.result["embedding_tokens"] == 0

    # Another organization importing the same filing is served from the same cache.
    other = await make_org()
    await ingest(other, "20260310900001")
    assert _document_texts_sent(fake) == sent_before


async def test_cached_vectors_match_fresh_ones(sessionmaker, settings):
    fake = FakeEmbedder(dim=settings.embedding_dim)
    cached = CachedEmbedder(fake, PgEmbeddingCache(sessionmaker))
    texts = ["리스부채 측정", "우발부채", "리스부채 측정"]  # duplicate text: one provider call
    first = await cached.embed_documents(texts)
    assert fake.calls == [("document", 2)]
    assert (first.cache_hits, first.cache_misses) == (0, 3)
    again = await cached.embed_documents(texts)
    assert (again.cache_hits, again.cache_misses) == (3, 0) and again.total_tokens == 0
    for a, b in zip(first.vectors, again.vectors, strict=True):
        assert a == pytest.approx(b, abs=1e-6)


class FailingAfter(FakeEmbedder):
    """Batches of ``batch_size``; the ``fail_at``-th batch (0-based) raises ``error``."""

    def __init__(self, dim, *, fail_at: int, error: EmbeddingError, batch_size: int = 2):
        super().__init__(dim=dim, batch_size=batch_size)
        self.fail_at = fail_at
        self.error = error

    async def embed_documents(self, texts, on_batch=None):
        batches = 0

        async def counting(batch, result):
            nonlocal batches
            await on_batch(batch, result)
            batches += 1

        async def guarded(batch, result):
            if batches == self.fail_at:
                raise self.error
            await counting(batch, result)

        return await super().embed_documents(texts, on_batch=guarded)


async def test_batches_before_a_failure_stay_in_the_cache(sessionmaker, settings):
    error = EmbeddingError(
        "임베딩 요청 거부(InvalidRequestError)", retryable=False, kind="rejected"
    )
    failing = FailingAfter(settings.embedding_dim, fail_at=1, error=error)
    cache = PgEmbeddingCache(sessionmaker)
    texts = [f"청크 {i}" for i in range(6)]
    with pytest.raises(EmbeddingError):
        await CachedEmbedder(failing, cache).embed_documents(texts)
    keys = [cache_key(failing.model, failing.dim, "document", t) for t in texts]
    assert set(await cache.get_many(keys)) == set(keys[:2])  # committed by its own transaction

    retry = FakeEmbedder(dim=settings.embedding_dim)
    result = await CachedEmbedder(retry, cache).embed_documents(texts)
    assert retry.calls == [("document", 4)]  # only what was not paid for yet
    assert (result.cache_hits, result.cache_misses) == (2, 4)


async def test_query_embeddings_are_not_cached(sessionmaker, settings, client, make_org, ingest):
    fake = FakeEmbedder(dim=settings.embedding_dim)
    cached = CachedEmbedder(fake, PgEmbeddingCache(sessionmaker))
    await cached.embed_query("리스부채는 어떻게 측정하나요?")
    assert fake.calls == [("query", 1)]
    assert await _cache_rows(sessionmaker) == 0

    org = await make_org()
    await ingest(org, "20260310900001")
    rows = await _cache_rows(sessionmaker)
    resp = await client.post(
        "/questions", json={"question": "리스부채 측정 방법은?"}, headers=org.headers("member")
    )
    assert resp.status_code in (200, 201), resp.text
    assert await _cache_rows(sessionmaker) == rows


# ---------------------------------------------------------------- job integration


async def _post_dart(client, org, rcept_no="20260310900001") -> dict:
    resp = await client.post(
        "/documents/dart", json={"rcept_no": rcept_no}, headers=org.headers("member")
    )
    assert resp.status_code in (200, 202), resp.text
    return resp.json()


async def test_middle_batch_failure_keeps_earlier_batches_cached_through_the_worker(
    client, make_org, sessionmaker, settings
):
    org = await make_org()
    body = await _post_dart(client, org)
    error = EmbeddingError(
        "임베딩 요청 거부(InvalidRequestError)", retryable=False, kind="rejected"
    )
    failing = FailingAfter(settings.embedding_dim, fail_at=1, error=error)
    w = Worker(
        sessionmaker=sessionmaker,
        context=JobContext(sessionmaker, settings, failing, FakeDartClient()),
    )
    assert await w.run_once()
    async with sessionmaker() as s:
        doc = await s.get(Document, uuid.UUID(body["document_id"]))
        job = await s.get(Job, uuid.UUID(body["job_id"]))
    assert job.status == "failed" and job.attempts == 1  # non-retryable: no job retry
    assert doc.status == "failed" and "임베딩 요청 거부" in doc.error
    assert await _cache_rows(sessionmaker) == 2  # the first batch survived the failure


async def test_rate_limit_failure_schedules_retry_then_explains_in_korean(
    client, make_org, sessionmaker, settings
):
    error = EmbeddingError(
        "임베딩 제공자 요청 한도 초과(결제 수단 미등록 가능성): 배치 재시도 6회 소진",
        retryable=True,
        kind="rate_limited",
        payment_method_missing=True,
    )
    failing = FailingAfter(settings.embedding_dim, fail_at=1, error=error)
    org = await make_org()
    body = await _post_dart(client, org)
    w = Worker(
        sessionmaker=sessionmaker,
        context=JobContext(sessionmaker, settings, failing, FakeDartClient()),
    )
    assert await w.run_once()
    resp = await client.get(f"/documents/{body['document_id']}", headers=org.headers("member"))
    doc = resp.json()
    async with sessionmaker() as s:
        job = await s.get(Job, uuid.UUID(body["job_id"]))
    assert doc["status"] == "queued" and "한도 초과" in doc["error"]
    assert doc["next_retry_at"] is not None
    assert datetime.fromisoformat(doc["next_retry_at"].replace("Z", "+00:00")) == job.run_after
    assert doc["progress"]["stage"] == "embed"  # last progress of the failed attempt
    cached_after_first = await _cache_rows(sessionmaker)
    assert cached_after_first == 2

    for _ in range(settings.job_max_attempts - 1):
        async with sessionmaker() as s:  # fast-forward to run_after
            await s.execute(update(Job).where(Job.id == job.id).values(run_after=func.now()))
            await s.commit()
        assert await w.run_once()
    async with sessionmaker() as s:
        job = await s.get(Job, job.id)
        final = await s.get(Document, uuid.UUID(body["document_id"]))
    assert job.status == "failed"
    assert final.status == "failed"
    assert "임베딩 제공자 요청 한도 초과(결제 수단 미등록 가능성)" in final.error
    assert "EMBED_RPM_LIMIT" in final.error
    # Each retry re-sent only what was missing: batch 1 was cached on attempt 1, and every
    # later attempt failed at its second batch, adding one more batch to the cache.
    assert await _cache_rows(sessionmaker) == 2 * settings.job_max_attempts


# ---------------------------------------------------------------- progress


async def test_ingest_records_progress_and_exposes_it(client, make_org, ingest, sessionmaker):
    org = await make_org()
    doc_id = await ingest(org, "20260310900001")
    async with sessionmaker() as s:
        job = await s.scalar(select(Job).where(Job.payload["document_id"].astext == doc_id))
        n = await s.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.document_id == uuid.UUID(doc_id))
        )
    assert job.result["progress"] == {"stage": "store", "done": n, "total": n}
    assert job.result["chunk_count"] == n

    doc = (await client.get(f"/documents/{doc_id}", headers=org.headers("member"))).json()
    assert doc["progress"] == {"stage": "store", "done": n, "total": n}
    assert doc["next_retry_at"] is None
    # Existing fields are unchanged.
    for field in ("id", "title", "source", "source_ref", "meta", "status", "section_count"):
        assert field in doc
    listing = (await client.get("/documents", headers=org.headers("member"))).json()
    assert listing[0]["progress"] == doc["progress"] and "next_retry_at" in listing[0]
    job_out = (await client.get(f"/jobs/{job.id}", headers=org.headers("member"))).json()
    assert job_out["progress"] == doc["progress"]


async def test_progress_is_visible_while_embedding(client, make_org, sessionmaker, settings):
    seen: list[dict] = []

    class Peeking(FakeEmbedder):
        async def embed_documents(self, texts, on_batch=None):
            async with sessionmaker() as s:
                job = await s.scalar(select(Job).where(Job.status == "running"))
                seen.append(dict(job.result["progress"]))
            return await super().embed_documents(texts, on_batch=on_batch)

    org = await make_org()
    await _post_dart(client, org)
    w = Worker(
        sessionmaker=sessionmaker,
        context=JobContext(
            sessionmaker, settings, Peeking(dim=settings.embedding_dim), FakeDartClient()
        ),
    )
    assert await w.run_once()
    assert seen and seen[0]["stage"] == "embed" and seen[0]["done"] == 0
    assert seen[0]["total"] > 0


async def test_progress_writes_are_throttled_per_stage(sessionmaker):
    async with sessionmaker() as s:
        enqueue(s, type="noop", payload={}, max_attempts=1)
        await s.commit()
    async with sessionmaker() as s:
        job = await claim_job(s, "progress-test")
    assert job.result is None
    clock = FakeClock()
    progress = JobProgress(sessionmaker, job, clock=clock)

    async def stored() -> dict:
        async with sessionmaker() as s:
            return (await s.get(Job, job.id, populate_existing=True)).result["progress"]

    await progress.update("embed", 1, 10)
    clock.now += 1.0
    await progress.update("embed", 2, 10)  # < 2 s later: skipped
    assert await stored() == {"stage": "embed", "done": 1, "total": 10}
    clock.now += 1.5
    await progress.update("embed", 3, 10)
    assert await stored() == {"stage": "embed", "done": 3, "total": 10}
    await progress.update("store", 0, 10)  # stage change: written at once
    assert await stored() == {"stage": "store", "done": 0, "total": 10}


async def test_keepalive_refreshes_the_lock_during_long_embedding(sessionmaker):
    import asyncio

    async with sessionmaker() as s:
        enqueue(s, type="noop", payload={}, max_attempts=1)
        await s.commit()
    async with sessionmaker() as s:
        job = await claim_job(s, "heartbeat-test")
        await s.execute(
            update(Job).where(Job.id == job.id).values(locked_at=func.now() - timedelta(hours=1))
        )
        await s.commit()

    async def locked_at():
        async with sessionmaker() as s:
            return (await s.get(Job, job.id)).locked_at

    before = await locked_at()
    async with JobProgress(sessionmaker, job).keepalive(0.05):
        await asyncio.sleep(0.3)
    after = await locked_at()
    assert after - before > timedelta(minutes=59)


async def test_claim_resets_the_previous_attempt_progress(sessionmaker):
    async with sessionmaker() as s:
        job = enqueue(s, type="noop", payload={}, max_attempts=3)
        job.result = {"progress": {"stage": "embed", "done": 5, "total": 9}}
        await s.commit()
    async with sessionmaker() as s:
        claimed = await claim_job(s, "w")
    assert claimed.id == job.id and claimed.result is None
