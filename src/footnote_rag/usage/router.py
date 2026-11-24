"""GET /usage (admin): LLM call counts, tokens, cost and latency percentiles per organization."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.auth.deps import ADMINS, require_role
from footnote_rag.db.models import LlmCall, User
from footnote_rag.db.session import get_session

router = APIRouter(tags=["usage"])


class ModelUsage(BaseModel):
    model: str
    calls: int
    cost_usd: Decimal | None


class UsageOut(BaseModel):
    calls: int
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    cost_usd: Decimal
    unpriced_calls: int
    latency_ms_p50: float | None
    latency_ms_p95: float | None
    by_model: list[ModelUsage]


@router.get("/usage", response_model=UsageOut)
async def get_usage(
    date_from: datetime | None = Query(default=None, alias="from"),
    date_to: datetime | None = Query(default=None, alias="to"),
    user: User = Depends(require_role(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> UsageOut:
    conditions = [LlmCall.org_id == user.org_id]
    if date_from:
        conditions.append(LlmCall.created_at >= date_from)
    if date_to:
        conditions.append(LlmCall.created_at < date_to)
    row = (
        await session.execute(
            select(
                func.count(LlmCall.id),
                func.coalesce(func.sum(LlmCall.input_tokens), 0),
                func.coalesce(func.sum(LlmCall.output_tokens), 0),
                func.coalesce(func.sum(LlmCall.cache_read_input_tokens), 0),
                func.coalesce(func.sum(LlmCall.cache_creation_input_tokens), 0),
                func.coalesce(func.sum(LlmCall.cost_usd), 0),
                func.count(LlmCall.id).filter(LlmCall.cost_usd.is_(None)),
                func.percentile_cont(0.5).within_group(LlmCall.latency_ms),
                func.percentile_cont(0.95).within_group(LlmCall.latency_ms),
            ).where(*conditions)
        )
    ).one()
    by_model = await session.execute(
        select(LlmCall.model, func.count(LlmCall.id), func.sum(LlmCall.cost_usd))
        .where(*conditions)
        .group_by(LlmCall.model)
        .order_by(LlmCall.model)
    )
    return UsageOut(
        calls=int(row[0]),
        input_tokens=int(row[1]),
        output_tokens=int(row[2]),
        cache_read_input_tokens=int(row[3]),
        cache_creation_input_tokens=int(row[4]),
        cost_usd=Decimal(row[5]),
        unpriced_calls=int(row[6]),
        latency_ms_p50=float(row[7]) if row[7] is not None else None,
        latency_ms_p95=float(row[8]) if row[8] is not None else None,
        by_model=[
            ModelUsage(model=m, calls=int(c), cost_usd=cost) for m, c, cost in by_model.all()
        ],
    )
