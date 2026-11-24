"""Job handlers: ``import_dart`` and ``ingest_text``.

Each handler downloads/parses/chunks/embeds outside any transaction, then writes the
result in ONE transaction: delete old chunks -> insert new chunks -> document ready ->
job succeeded -> audit entry. Re-running a job therefore never duplicates chunks.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import delete
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
from footnote_rag.retrieval.embedder import Embedder, EmbeddingError

log = get_logger(__name__)

DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


@dataclass
class JobContext:
    sessionmaker: async_sessionmaker[AsyncSession]
    settings: Settings
    embedder: Embedder
    dart_client: DartClient


Handler = Callable[[JobContext, Job], Awaitable[dict[str, Any]]]


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
) -> dict[str, Any]:
    s = ctx.settings
    drafts = chunk_sections(
        [(sec.path, sec.text) for sec in sections], s.chunk_size, s.chunk_overlap
    )
    if not drafts:
        raise PermanentJobError("no chunks produced")
    try:
        embedded = await ctx.embedder.embed_documents([d.text for d in drafts])
    except EmbeddingError as exc:
        if exc.retryable:
            raise RetryableJobError(str(exc)) from None
        raise PermanentJobError(str(exc)) from None
    if len(embedded.vectors) != len(drafts):
        raise RetryableJobError("embedding count mismatch")

    result = {
        "section_count": len(sections),
        "chunk_count": len(drafts),
        "embedding_model": ctx.embedder.model,
        "embedding_tokens": embedded.total_tokens,
    }
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
            metadata={"job_id": str(job.id), "job_type": job.type, **result, **audit_extra},
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

    try:
        content = await load_dart_zip(
            ctx.dart_client, dart_cache_dir(ctx.settings.dart_dir, ctx.dart_client), rcept_no
        )
    except DartError as exc:
        if exc.retryable:
            raise RetryableJobError(str(exc)) from None
        raise PermanentJobError(str(exc)) from None
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
    )


async def handle_ingest_text(ctx: JobContext, job: Job) -> dict[str, Any]:
    document_id = uuid.UUID(job.payload["document_id"])
    await _mark_processing(ctx, document_id)
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
    )


HANDLERS: dict[str, Handler] = {
    "import_dart": handle_import_dart,
    "ingest_text": handle_ingest_text,
}
