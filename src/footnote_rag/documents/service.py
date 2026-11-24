"""문서 가져오기 요청 서비스(HTTP API와 CLI가 같이 쓴다).

역할: 문서 행(queued)·작업 행·감사 로그를 한 트랜잭션으로 만든다. 같은 조직의 같은 ``rcept_no``는 기존 문서를 돌려준다.
이미 처리된(ready/failed) 문서는 같은 방식으로 다시 처리 작업을 등록할 수 있다(``request_reprocess``).
주요 요소: ``request_dart_import``, ``create_text_upload``, ``request_reprocess``, ``ImportResult``,
``ReprocessResult``, ``InvalidRceptNoError``, ``DocumentNotFoundError``, ``DocumentBusyError``.
호출 관계: ``documents.router``, ``cli.cmd_dart_import``·``cli.cmd_dart_reprocess``, ``evals.run``이 호출하고
``jobs.queue.enqueue``를 부른다.
학습 문서: docs/study/02-request-walkthrough.md, docs/study/04-job-queue.md

Document import requests. Used by both the HTTP API and the CLI.

Duplicate DART imports: if the organization already has a document for the same
``rcept_no`` the existing document is returned (HTTP 200, ``duplicate: true``) and no new
document or job is created. See README "중복 가져오기".
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.audit.service import Actions, record
from footnote_rag.config import get_settings
from footnote_rag.db.models import Document, Job, User
from footnote_rag.documents.text_upload import ALLOWED_EXTENSIONS
from footnote_rag.jobs.handlers import dart_url
from footnote_rag.jobs.queue import enqueue

RCEPT_NO_RE = re.compile(r"^\d{14}$")


class InvalidRceptNoError(ValueError):
    pass


class UploadRejectedError(ValueError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class DocumentNotFoundError(LookupError):
    """No such document in the caller's organization (HTTP 404)."""


class DocumentBusyError(RuntimeError):
    """The document is queued or processing; a reprocess would race the running job (HTTP 409)."""


BUSY_DOCUMENT_STATUSES = frozenset({"queued", "processing"})


@dataclass
class ImportResult:
    document: Document
    job_id: uuid.UUID | None
    existing: bool


async def _latest_job_id(session: AsyncSession, document_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(Job.id)
        .where(Job.payload["document_id"].astext == str(document_id))
        .order_by(Job.created_at.desc())
        .limit(1)
    )


async def _find_dart_document(
    session: AsyncSession, org_id: uuid.UUID, rcept_no: str
) -> Document | None:
    return await session.scalar(
        select(Document).where(
            Document.org_id == org_id,
            Document.source == "dart",
            Document.source_ref == rcept_no,
        )
    )


async def request_dart_import(session: AsyncSession, *, user: User, rcept_no: str) -> ImportResult:
    rcept_no = rcept_no.strip()
    if not RCEPT_NO_RE.match(rcept_no):
        raise InvalidRceptNoError("rcept_no must be exactly 14 digits")
    org_id = user.org_id  # captured: a rollback below would expire `user`

    existing = await _find_dart_document(session, org_id, rcept_no)
    if existing is not None:
        return ImportResult(existing, await _latest_job_id(session, existing.id), existing=True)

    settings = get_settings()
    doc = Document(
        id=uuid.uuid4(),
        org_id=org_id,
        created_by=user.id,
        title=f"DART {rcept_no}",
        source="dart",
        source_ref=rcept_no,
        meta={"rcept_no": rcept_no, "rcept_dt": rcept_no[:8], "dart_url": dart_url(rcept_no)},
        status="queued",
        section_count=0,
    )
    session.add(doc)  # INSERT happens at commit; ids are generated client-side
    job = enqueue(
        session,
        type="import_dart",
        payload={"document_id": str(doc.id), "org_id": str(org_id), "rcept_no": rcept_no},
        max_attempts=settings.job_max_attempts,
    )
    record(
        session,
        action=Actions.DART_IMPORT_REQUESTED,
        org_id=org_id,
        actor_user_id=user.id,
        target_type="document",
        target_id=doc.id,
        metadata={"rcept_no": rcept_no, "job_id": str(job.id)},
    )
    try:
        await session.commit()  # document + job + audit in one transaction
    except IntegrityError:
        # A concurrent request created the same (org, dart, rcept_no) first.
        await session.rollback()
        existing = await _find_dart_document(session, org_id, rcept_no)
        if existing is None:
            raise
        return ImportResult(existing, await _latest_job_id(session, existing.id), existing=True)
    return ImportResult(doc, job.id, existing=False)


async def create_text_upload(
    session: AsyncSession, *, user: User, filename: str, data: bytes
) -> ImportResult:
    settings = get_settings()
    safe_name = (filename or "upload.txt").replace("\\", "/").rsplit("/", 1)[-1][:200]
    ext = "." + safe_name.rsplit(".", 1)[-1].lower() if "." in safe_name else ""
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadRejectedError("only .txt and .md files are accepted", 415)
    if len(data) > settings.upload_max_bytes:
        raise UploadRejectedError("file exceeds the 5MB limit", 413)
    if not data.strip():
        raise UploadRejectedError("file is empty", 400)

    doc_id = uuid.uuid4()
    stored_name = f"{doc_id}{ext}"
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    (settings.upload_dir / stored_name).write_bytes(data)

    title = safe_name.rsplit(".", 1)[0] or safe_name
    doc = Document(
        id=doc_id,
        org_id=user.org_id,
        created_by=user.id,
        title=title,
        source="upload",
        source_ref=None,
        meta={"filename": safe_name, "size_bytes": len(data)},
        status="queued",
        section_count=0,
    )
    session.add(doc)
    await session.flush()
    job = enqueue(
        session,
        type="ingest_text",
        payload={
            "document_id": str(doc.id),
            "org_id": str(user.org_id),
            "stored_name": stored_name,
            "filename": safe_name,
        },
        max_attempts=settings.job_max_attempts,
    )
    record(
        session,
        action=Actions.TEXT_UPLOADED,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="document",
        target_id=doc.id,
        metadata={"filename": safe_name, "size_bytes": len(data), "job_id": str(job.id)},
    )
    await session.commit()
    return ImportResult(doc, job.id, existing=False)


@dataclass
class ReprocessResult:
    document: Document
    job_id: uuid.UUID


async def _reprocess_payload(session: AsyncSession, doc: Document) -> tuple[str, dict[str, str]]:
    """Job type and payload that rebuild ``doc`` from its source (DART ZIP cache or upload file)."""
    base = {"document_id": str(doc.id), "org_id": str(doc.org_id)}
    if doc.source == "dart":
        rcept_no = doc.source_ref or str((doc.meta or {}).get("rcept_no", ""))
        return "import_dart", {**base, "rcept_no": rcept_no}
    previous = await session.scalar(
        select(Job.payload)
        .where(Job.type == "ingest_text", Job.payload["document_id"].astext == str(doc.id))
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    filename = str((doc.meta or {}).get("filename") or "upload.txt")
    if previous and previous.get("stored_name"):
        return "ingest_text", {
            **base,
            "stored_name": str(previous["stored_name"]),
            "filename": filename,
        }
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return "ingest_text", {**base, "stored_name": f"{doc.id}{ext}", "filename": filename}


async def request_reprocess(
    session: AsyncSession, *, user: User, document_id: uuid.UUID
) -> ReprocessResult:
    """Queue a fresh ingest job for an existing document (e.g. after a parser change).

    Document -> ``queued`` (error cleared), new job and ``document.reprocess_requested`` audit
    entry commit together. The handler replaces the chunks, and a DART filing is re-parsed from
    the cached ZIP (``data/dart/{rcept_no}.zip``) without downloading it again.
    """
    org_id, actor_id = user.org_id, user.id
    doc = await session.scalar(
        select(Document)
        .where(Document.id == document_id, Document.org_id == org_id)
        .with_for_update()
        .execution_options(populate_existing=True)  # read the locked row, not a cached object
    )
    # Nothing changed on these paths: commit (not rollback) just releases the row lock without
    # expiring loaded objects such as ``user``, so callers can keep using them (CLI loop).
    if doc is None:
        await session.commit()
        raise DocumentNotFoundError(str(document_id))
    if doc.status in BUSY_DOCUMENT_STATUSES:
        status = doc.status
        await session.commit()
        raise DocumentBusyError(status)

    job_type, payload = await _reprocess_payload(session, doc)
    previous_status = doc.status
    doc.status = "queued"
    doc.error = None
    job = enqueue(
        session,
        type=job_type,
        payload=payload,
        max_attempts=get_settings().job_max_attempts,
    )
    record(
        session,
        action=Actions.REPROCESS_REQUESTED,
        org_id=org_id,
        actor_user_id=actor_id,
        target_type="document",
        target_id=doc.id,
        metadata={
            "job_id": str(job.id),
            "job_type": job_type,
            "source": doc.source,
            "source_ref": doc.source_ref,
            "previous_status": previous_status,
        },
    )
    await session.commit()  # document status + job + audit in one transaction
    return ReprocessResult(doc, job.id)
