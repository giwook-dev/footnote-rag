"""GET /audit-logs (admin only, own organization, paginated)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.auth.deps import ADMINS, require_role
from footnote_rag.db.models import AuditLog, User
from footnote_rag.db.session import get_session

router = APIRouter(tags=["audit"])


class AuditLogOut(BaseModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str | None
    target_id: str | None
    metadata: dict[str, Any]
    request_id: str | None
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditLogOut]
    total: int
    limit: int
    offset: int


@router.get("/audit-logs", response_model=AuditPage)
async def list_audit_logs(
    action: str | None = None,
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_role(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> AuditPage:
    conditions = [AuditLog.org_id == user.org_id]
    if action:
        conditions.append(AuditLog.action == action)
    if date_from:
        conditions.append(AuditLog.created_at >= date_from)
    if date_to:
        conditions.append(AuditLog.created_at < date_to)
    total = await session.scalar(select(func.count()).select_from(AuditLog).where(*conditions))
    rows = await session.scalars(
        select(AuditLog)
        .where(*conditions)
        .order_by(AuditLog.created_at.desc(), AuditLog.id)
        .limit(limit)
        .offset(offset)
    )
    return AuditPage(
        items=[
            AuditLogOut(
                id=r.id,
                actor_user_id=r.actor_user_id,
                action=r.action,
                target_type=r.target_type,
                target_id=r.target_id,
                metadata=r.metadata_,
                request_id=r.request_id,
                created_at=r.created_at,
            )
            for r in rows
        ],
        total=int(total or 0),
        limit=limit,
        offset=offset,
    )
