"""문서 가져오기 요청 서비스(HTTP API와 CLI가 같이 쓴다).

역할: 문서 행(queued)·작업 행·감사 로그를 한 트랜잭션으로 만든다. 같은 조직의 같은 ``rcept_no``는 기존 문서를 돌려준다.
주요 요소: ``request_dart_import``, ``create_text_upload``, ``ImportResult``, ``InvalidRceptNoError``.
호출 관계: ``documents.router``, ``cli.cmd_dart_import``, ``evals.run``이 호출하고 ``jobs.queue.enqueue``를 부른다.
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
