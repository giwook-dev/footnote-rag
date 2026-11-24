"""작업 핸들러: ``import_dart``(OpenDART 원문)와 ``ingest_text``(업로드 파일).

역할: 다운로드·파싱·청킹·임베딩은 트랜잭션 밖에서 하고, 결과 저장(청크 삭제 후 재삽입, 문서 ready,
작업 succeeded, 감사)은 한 트랜잭션으로 한다. 외부 오류를 재시도 가능/불가 작업 오류로 바꾼다.
임베딩은 ``CachedEmbedder``를 거쳐 캐시에 없는 청크만 보내고, 성공한 배치는 바로 캐시에 커밋된다.
진행 중에는 ``JobProgress``가 ``jobs.result.progress``(단계, done/total)를 2초 이상 간격으로 기록한다.
주요 요소: ``handle_import_dart``, ``handle_ingest_text``, ``_embed_and_store``, ``JobProgress``,
``load_dart_zip``, ``HANDLERS``.
호출 관계: ``jobs.worker.Worker``가 작업 type으로 골라 호출하고, ``dart``·``documents.chunking``·
``retrieval.embedding_cache``·``retrieval.embedder``를 부른다.
학습 문서: docs/study/02-request-walkthrough.md, docs/study/04-job-queue.md

Job handlers: ``import_dart`` and ``ingest_text``.

Each handler downloads/parses/chunks/embeds outside any transaction, then writes the
result in ONE transaction: delete old chunks -> insert new chunks -> document ready ->
job succeeded -> audit entry. Re-running a job therefore never duplicates chunks.
Embeddings already paid for (earlier batches, earlier attempts, other documents with the
same text) come from ``embedding_cache`` instead of the provider.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from footnote_rag.audit.service import Actions, record
from footnote_rag.config import Settings
from footnote_rag.dart.client import DartClient, DartError
from footnote_rag.dart.corp_codes import CorpCodeCache
from footnote_rag.dart.parser import DartParseError, Section, parse_document_zip
from footnote_rag.db.models import Chunk, Document, Job
from footnote_rag.documents.chunking import chunk_sections
from footnote_rag.documents.text_upload import (
    UploadParseError,
    decode_upload,
    parse_text_document,
)
from footnote_rag.jobs.queue import PermanentJobError, RetryableJobError, mark_succeeded
from footnote_rag.observability import get_logger
from footnote_rag.retrieval.embedder import REDUCED_LIMITS_HINT, Embedder, EmbeddingError
from footnote_rag.retrieval.embedding_cache import (
    CachedEmbedder,
    EmbeddingCacheStore,
    PgEmbeddingCache,
)

log = get_logger(__name__)

DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
PROGRESS_MIN_INTERVAL_SECONDS = 2.0


@dataclass
class JobContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    embedder: Embedder
    dart_client: DartClient
    # Document-embedding cache; None = the ``embedding_cache`` table via ``sessionmaker``.
    embedding_cache: EmbeddingCacheStore | None = None

    def cached_embedder(self) -> CachedEmbedder:
        return CachedEmbedder(
            self.embedder, self.embedding_cache or PgEmbeddingCache(self.sessionmaker)
        )


Handler = Callable[[JobContext, Job], Awaitable[dict[str, Any]]]


class JobProgress:
    """Write ``jobs.result.progress = {stage, done, total}`` while the job runs.

    Each write is its own short transaction, at most once per ``min_interval`` seconds within a
    stage (a stage change is written at once). The write also refreshes ``locked_at``, and
    ``keepalive`` heartbeats during embedding, so a long, rate-limited run (retry sleeps included)
    is not mistaken for a dead worker by stale-job recovery.
    Progress is best effort: a failed write is logged, never fails the job.
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        job: Job,
        *,
        min_interval: float = PROGRESS_MIN_INTERVAL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._job_id = job.id
        self._locked_by = job.locked_by
        self._min_interval = min_interval
        self._clock = clock
        self._last_write: float | None = None
        self._last_stage: str | None = None
        self.current: dict[str, Any] | None = None

    async def update(self, stage: str, done: int, total: int) -> None:
        self.current = {"stage": stage, "done": done, "total": total}
        now = self._clock()
        if (
            stage == self._last_stage
            and self._last_write is not None
            and now - self._last_write < self._min_interval
        ):
            return
        self._last_write, self._last_stage = now, stage
        await self._write(result={"progress": self.current}, locked_at=func.now())

    async def heartbeat(self) -> None:
        """Refresh ``locked_at`` only (used while a batch sleeps between retries)."""
        await self._write(locked_at=func.now())

    @contextlib.asynccontextmanager
    async def keepalive(self, interval_seconds: float) -> AsyncIterator[None]:
        """Heartbeat every ``interval_seconds`` for the duration of the block."""

        async def beat() -> None:
            while True:
                await asyncio.sleep(interval_seconds)
                await self.heartbeat()

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _write(self, **values: Any) -> None:
        conditions = [Job.id == self._job_id, Job.status == "running"]
        if self._locked_by is not None:
            conditions.append(Job.locked_by == self._locked_by)
        try:
            async with self._sessionmaker() as session:
                await session.execute(update(Job).where(*conditions).values(**values))
                await session.commit()
        except SQLAlchemyError as exc:
            log.warning(
                "job.progress_write_failed", job_id=str(self._job_id), error=type(exc).__name__
            )


def dart_url(rcept_no: str) -> str:
    return DART_VIEWER_URL.format(rcept_no=rcept_no)


def dart_cache_dir(dart_dir: Path, client: DartClient) -> Path:
    """``data/dart`` for real downloads; ``data/dart/fake`` for synthetic data, so fake runs
    never leave files that a later real run would mistake for OpenDART downloads."""
    from footnote_rag.dart.client import FakeDartClient

    return dart_dir / "fake" if isinstance(client, FakeDartClient) else dart_dir


async def load_dart_zip(client: DartClient, dart_dir: Path, rcept_no: str) -> bytes:
    """Return the filing ZIP, reusing ``data/dart/{rcept_no}.zip`` when cached."""
    cache = dart_dir / f"{rcept_no}.zip"
    cached = await asyncio.to_thread(_read_if_present, cache)
    if cached is not None:
        return cached
    content = await client.download_document(rcept_no)
    await asyncio.to_thread(_atomic_write, cache, content)
    return content


def _read_if_present(path: Path) -> bytes | None:
    if path.exists() and path.stat().st_size > 0:
        return path.read_bytes()
    return None


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.part")
    tmp.write_bytes(content)
    os.replace(tmp, path)


async def _mark_processing(ctx: JobContext, document_id: uuid.UUID) -> Document:
    async with ctx.sessionmaker() as session:
        doc = await session.get(Document, document_id)
        if doc is None:
            raise PermanentJobError(f"document {document_id} not found")
        doc.status = "processing"
        doc.error = None
        await session.commit()
        return doc


async def _embed_and_store(
    ctx: JobContext,
    job: Job,
    *,
    document_id: uuid.UUID,
    sections: list[Section],
    title: str | None,
    meta_update: dict[str, Any],
    audit_extra: dict[str, Any],
    progress: JobProgress,
) -> dict[str, Any]:
    s = ctx.settings
    drafts = chunk_sections(
        [(sec.path, sec.text) for sec in sections], s.chunk_size, s.chunk_overlap
    )
    if not drafts:
        raise PermanentJobError("no chunks produced")

    async def on_progress(done: int, total: int) -> None:
        await progress.update("embed", done, total)

    # Rate-limit waits and batch retries can be long; keep the job's lock fresh meanwhile.
    heartbeat_seconds = max(1.0, s.job_lock_timeout_seconds / 3)
    try:
        async with progress.keepalive(heartbeat_seconds):
            embedded = await ctx.cached_embedder().embed_documents(
                [d.text for d in drafts], on_progress=on_progress
            )
    except EmbeddingError as exc:
        message = str(exc)
        if exc.payment_method_missing:
            message = f"{message} — {REDUCED_LIMITS_HINT}"
        if exc.retryable:
            raise RetryableJobError(message) from None
        raise PermanentJobError(message) from None
    except SQLAlchemyError as exc:
        # embedding_cache lookup/insert failed (DB hiccup): batches already cached are kept.
        raise RetryableJobError(f"임베딩 캐시 DB 오류({type(exc).__name__})") from None
    if len(embedded.vectors) != len(drafts):
        raise RetryableJobError("임베딩 개수 불일치(embedding count mismatch)")

    n = len(drafts)
    await progress.update("store", 0, n)
    result = {
        "section_count": len(sections),
        "chunk_count": n,
        "embedding_model": ctx.embedder.model,
        # Billable tokens only: chunks served from embedding_cache cost nothing.
        "embedding_tokens": embedded.total_tokens,
        "embedding_cache_hits": embedded.cache_hits,
        "embedding_cache_misses": embedded.cache_misses,
    }
    audit_metadata = {"job_id": str(job.id), "job_type": job.type, **result, **audit_extra}
    result["progress"] = {"stage": "store", "done": n, "total": n}
    async with ctx.sessionmaker() as session:
        doc = await session.get(Document, document_id, with_for_update=True)
        if doc is None:
            raise PermanentJobError(f"document {document_id} not found")
        await session.execute(delete(Chunk).where(Chunk.document_id == document_id))
        session.add_all(
            Chunk(
                id=uuid.uuid4(),
                document_id=document_id,
                org_id=doc.org_id,
                section_path=d.section_path,
                chunk_index=d.chunk_index,
                text=d.text,
                embedding=vec,
            )
            for d, vec in zip(drafts, embedded.vectors, strict=True)
        )
        if title:
            doc.title = title[:500]
        doc.meta = {**(doc.meta or {}), **meta_update}
        doc.status = "ready"
        doc.section_count = len(sections)
        doc.error = None
        await mark_succeeded(session, job.id, result)
        record(
            session,
            action=Actions.INGEST_SUCCEEDED,
            org_id=doc.org_id,
            actor_user_id=doc.created_by,
            target_type="document",
            target_id=doc.id,
            metadata=audit_metadata,
        )
        await session.commit()
    return result


async def _lookup_stock_code(ctx: JobContext, corp_code: str) -> str | None:
    if not corp_code:
        return None
    try:
        cache = CorpCodeCache(
            ctx.dart_client,
            dart_cache_dir(ctx.settings.dart_dir, ctx.dart_client) / "corp_codes.json",
            ctx.settings.dart_corp_code_ttl_days,
        )
        corps = await cache._load()  # noqa: SLF001 - reverse lookup on the same cache
        for info in corps.values():
            if info.corp_code == corp_code:
                return info.stock_code
    except Exception as exc:  # noqa: BLE001 - metadata enrichment is best effort
        log.warning("dart.corp_lookup_failed", error=type(exc).__name__)
    return None


async def handle_import_dart(ctx: JobContext, job: Job) -> dict[str, Any]:
    document_id = uuid.UUID(job.payload["document_id"])
    rcept_no = str(job.payload["rcept_no"])
    doc = await _mark_processing(ctx, document_id)
    progress = JobProgress(ctx.sessionmaker, job)

    await progress.update("download", 0, 1)
    try:
        content = await load_dart_zip(
            ctx.dart_client, dart_cache_dir(ctx.settings.dart_dir, ctx.dart_client), rcept_no
        )
    except DartError as exc:
        if exc.retryable:
            raise RetryableJobError(str(exc)) from None
        raise PermanentJobError(str(exc)) from None
    await progress.update("parse", 0, 1)
    try:
        parsed = parse_document_zip(content, rcept_no)
    except DartParseError as exc:
        raise PermanentJobError(f"parse failed: {exc}") from None

    stock_code = await _lookup_stock_code(ctx, parsed.corp_code)
    meta = {
        "rcept_no": rcept_no,
        "corp_name": parsed.company_name or doc.meta.get("corp_name"),
        "corp_code": parsed.corp_code or doc.meta.get("corp_code"),
        "stock_code": stock_code or doc.meta.get("stock_code"),
        "report_nm": parsed.document_name or doc.meta.get("report_nm"),
        "rcept_dt": rcept_no[:8],
        "dart_url": dart_url(rcept_no),
        "main_member": parsed.main_member,
    }
    return await _embed_and_store(
        ctx,
        job,
        document_id=document_id,
        sections=parsed.sections,
        title=parsed.title,
        meta_update=meta,
        audit_extra={"rcept_no": rcept_no},
        progress=progress,
    )


async def handle_ingest_text(ctx: JobContext, job: Job) -> dict[str, Any]:
    document_id = uuid.UUID(job.payload["document_id"])
    await _mark_processing(ctx, document_id)
    progress = JobProgress(ctx.sessionmaker, job)
    await progress.update("parse", 0, 1)
    path = ctx.settings.upload_dir / str(job.payload["stored_name"])
    if not path.exists():
        raise PermanentJobError("uploaded file is missing")
    try:
        sections = parse_text_document(decode_upload(path.read_bytes()), job.payload["filename"])
    except UploadParseError as exc:
        raise PermanentJobError(str(exc)) from None
    return await _embed_and_store(
        ctx,
        job,
        document_id=document_id,
        sections=sections,
        title=None,
        meta_update={},
        audit_extra={},
        progress=progress,
    )


HANDLERS: dict[str, Handler] = {
    "import_dart": handle_import_dart,
    "ingest_text": handle_ingest_text,
}
