"""문서·작업 엔드포인트(``POST /documents/dart``, ``POST /documents/upload``, ``GET /documents``,
``POST /documents/{id}/reprocess``, ``GET /jobs/{id}``).

역할: 요청을 검증(``rcept_no`` 14자리)하고 서비스에 넘기며, 처음 요청은 202, 중복 요청은 200을 돌려준다.
재처리는 admin만(403), 같은 조직 문서만(404), queued/processing이 아닐 때만(409) 202로 받는다.
주요 요소: ``import_dart``, ``upload_text``, ``list_documents``, ``get_document``, ``reprocess_document``, ``get_job``.
호출 관계: ``documents.service``의 ``request_dart_import``·``create_text_upload``·``request_reprocess``를 호출한다.
학습 문서: docs/study/02-request-walkthrough.md

/documents and /jobs endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.auth.deps import ADMINS, ANY_MEMBER, require_role
from footnote_rag.config import get_settings
from footnote_rag.db.models import Document, Job, User
from footnote_rag.db.session import get_session
from footnote_rag.documents.service import (
    DocumentBusyError,
    DocumentNotFoundError,
    InvalidRceptNoError,
    UploadRejectedError,
    create_text_upload,
    request_dart_import,
    request_reprocess,
)

router = APIRouter(tags=["documents"])


class DartImportIn(BaseModel):
    rcept_no: str = Field(pattern=r"^\d{14}$", description="14-digit OpenDART receipt number")


class ImportOut(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    status: str
    duplicate: bool = False


class ReprocessOut(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    source: str
    source_ref: str | None
    meta: dict[str, Any]
    status: str
    section_count: int
    error: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: uuid.UUID
    type: str
    status: str
    attempts: int
    max_attempts: int
    run_after: datetime
    last_error: str | None
    result: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.post(
    "/documents/dart",
    response_model=ImportOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "Already imported: the existing document is returned"}},
)
async def import_dart(
    body: DartImportIn,
    response: Response,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> ImportOut:
    try:
        result = await request_dart_import(session, user=user, rcept_no=body.rcept_no)
    except InvalidRceptNoError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if result.existing:
        response.status_code = status.HTTP_200_OK
    return ImportOut(
        document_id=result.document.id,
        job_id=result.job_id,
        status=result.document.status,
        duplicate=result.existing,
    )


@router.post("/documents/upload", response_model=ImportOut, status_code=status.HTTP_202_ACCEPTED)
async def upload_text(
    file: UploadFile = File(...),
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> ImportOut:
    limit = get_settings().upload_max_bytes
    data = await file.read(limit + 1)
    try:
        result = await create_text_upload(
            session, user=user, filename=file.filename or "upload.txt", data=data
        )
    except UploadRejectedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return ImportOut(document_id=result.document.id, job_id=result.job_id, status="queued")


@router.get("/documents", response_model=list[DocumentOut])
async def list_documents(
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> list[Document]:
    rows = await session.scalars(
        select(Document).where(Document.org_id == user.org_id).order_by(Document.created_at.desc())
    )
    return list(rows)


@router.get("/documents/{document_id}", response_model=DocumentOut)
async def get_document(
    document_id: uuid.UUID,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> Document:
    doc = await session.scalar(
        select(Document).where(Document.id == document_id, Document.org_id == user.org_id)
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.post(
    "/documents/{document_id}/reprocess",
    response_model=ReprocessOut,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        404: {"description": "No such document in your organization"},
        409: {"description": "The document is queued or processing"},
    },
)
async def reprocess_document(
    document_id: uuid.UUID,
    user: User = Depends(require_role(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> ReprocessOut:
    """Re-run ingestion (parse, chunk, embed) for a ready/failed document. Admin only."""
    try:
        result = await request_reprocess(session, user=user, document_id=document_id)
    except DocumentNotFoundError:
        raise HTTPException(status_code=404, detail="Document not found") from None
    except DocumentBusyError as exc:
        raise HTTPException(
            status_code=409, detail=f"Document is {exc} and cannot be reprocessed now"
        ) from None
    return ReprocessOut(document_id=result.document.id, job_id=result.job_id)


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(
    job_id: uuid.UUID,
    user: User = Depends(require_role(*ANY_MEMBER)),
    session: AsyncSession = Depends(get_session),
) -> Job:
    job = await session.scalar(
        select(Job).where(Job.id == job_id, Job.payload["org_id"].astext == str(user.org_id))
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job
