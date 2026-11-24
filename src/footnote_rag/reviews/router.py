"""사람 검토(HITL) 대기열 엔드포인트(reviewer / admin).

역할: 상태별 답변 목록을 보여 주고, ``pending_review`` 답변을 승인·거절한다.
주요 요소: ``list_reviews``(``GET /reviews``), ``decide``(``POST /reviews/{answer_id}``, 조건부 UPDATE).
호출 관계: ``auth.deps.require_role(REVIEWERS)``로 보호되고 ``audit.service.record``를 같은 트랜잭션에서 부른다.
학습 문서: docs/study/08-review-audit-usage.md

Human review queue (reviewer / admin).

A decision is a single conditional UPDATE (``... WHERE status = 'pending_review'``), so
when two reviewers decide at the same time exactly one row update wins; the loser sees
zero affected rows and gets 409. The audit entry is written in the same transaction.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.audit.service import Actions, record
from footnote_rag.auth.deps import REVIEWERS, require_role
from footnote_rag.db.models import ANSWER_STATUSES, Answer, Question, User
from footnote_rag.db.session import get_session

router = APIRouter(tags=["reviews"])


class ReviewItem(BaseModel):
    answer_id: uuid.UUID
    question_id: uuid.UUID
    question: str
    status: str
    review_reason: str | None
    text: str
    footnotes: list[dict[str, Any]]
    reviewed_by: uuid.UUID | None
    reviewed_at: datetime | None
    review_comment: str | None
    created_at: datetime


class DecisionIn(BaseModel):
    decision: Literal["approve", "reject"]
    comment: str | None = Field(default=None, max_length=2000)


class DecisionOut(BaseModel):
    answer_id: uuid.UUID
    status: str
    reviewed_by: uuid.UUID
    reviewed_at: datetime


@router.get("/reviews", response_model=list[ReviewItem])
async def list_reviews(
    status: str = Query(default="pending_review"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(require_role(*REVIEWERS)),
    session: AsyncSession = Depends(get_session),
) -> list[ReviewItem]:
    if status not in ANSWER_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {ANSWER_STATUSES}")
    rows = await session.execute(
        select(Answer, Question.text)
        .join(Question, Question.id == Answer.question_id)
        .where(Answer.org_id == user.org_id, Answer.status == status)
        .order_by(Answer.created_at)
        .limit(limit)
        .offset(offset)
    )
    return [
        ReviewItem(
            answer_id=a.id,
            question_id=a.question_id,
            question=qtext,
            status=a.status,
            review_reason=a.review_reason,
            text=a.text,
            footnotes=a.footnotes,
            reviewed_by=a.reviewed_by,
            reviewed_at=a.reviewed_at,
            review_comment=a.review_comment,
            created_at=a.created_at,
        )
        for a, qtext in rows.all()
    ]


@router.post("/reviews/{answer_id}", response_model=DecisionOut)
async def decide(
    answer_id: uuid.UUID,
    body: DecisionIn,
    user: User = Depends(require_role(*REVIEWERS)),
    session: AsyncSession = Depends(get_session),
) -> DecisionOut:
    new_status = "approved" if body.decision == "approve" else "rejected"
    org_id, user_id = user.org_id, user.id  # stable even if the session rolls back
    # 조건부 UPDATE 한 번: WHERE에 status='pending_review'를 넣어 "읽고 나서 쓰기" 경쟁을 없앤다.
    # 동시에 결정하면 먼저 행을 바꾼 요청만 1행을 받고, 나머지는 0행 → 아래에서 409(또는 404).
    result = await session.execute(
        update(Answer)
        .where(
            Answer.id == answer_id,
            Answer.org_id == org_id,
            Answer.status == "pending_review",
        )
        .values(
            status=new_status,
            reviewed_by=user_id,
            reviewed_at=func.now(),
            review_comment=body.comment,
        )
        .returning(Answer.id, Answer.reviewed_at, Answer.review_reason)
    )
    row = result.first()
    if row is None:
        await session.rollback()
        exists = await session.scalar(
            select(Answer.status).where(Answer.id == answer_id, Answer.org_id == org_id)
        )
        if exists is None:
            raise HTTPException(status_code=404, detail="Answer not found")
        raise HTTPException(status_code=409, detail=f"Answer is already {exists}")
    record(
        session,
        action=Actions.REVIEW_DECIDED,
        org_id=org_id,
        actor_user_id=user_id,
        target_type="answer",
        target_id=answer_id,
        metadata={
            "decision": body.decision,
            "new_status": new_status,
            "review_reason": row.review_reason,
            "has_comment": bool(body.comment),
        },
    )
    await session.commit()
    return DecisionOut(
        answer_id=answer_id, status=new_status, reviewed_by=user_id, reviewed_at=row.reviewed_at
    )
