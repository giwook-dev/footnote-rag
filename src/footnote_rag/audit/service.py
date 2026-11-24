"""감사 로그 기록(append-only).

역할: 호출자 세션에 감사 행을 *추가만* 해서 업무 변경과 같은 트랜잭션으로 커밋(또는 롤백)되게 한다.
주요 요소: ``record``, action 이름 모음 ``Actions``, 금지 metadata 키 ``FORBIDDEN_METADATA_KEYS``.
호출 관계: auth·orgs·documents·jobs·answering·reviews가 호출한다. UPDATE/DELETE 거부는 DB 트리거 몫.
학습 문서: docs/study/08-review-audit-usage.md

Append-only audit trail.

``record`` only *adds* a row to the caller's session, so the audit entry commits (or rolls
back) together with the business change it describes. Never put passwords, tokens, API
keys or document text into ``metadata``.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.db.models import AuditLog
from footnote_rag.observability import request_id_var

FORBIDDEN_METADATA_KEYS = frozenset(
    {"password", "password_hash", "token", "access_token", "api_key", "text", "content"}
)


class Actions:
    LOGIN_SUCCEEDED = "auth.login_succeeded"
    LOGIN_FAILED = "auth.login_failed"
    USER_CREATED = "user.created"
    DART_SEARCH = "dart.search"
    DART_IMPORT_REQUESTED = "document.dart_import_requested"
    TEXT_UPLOADED = "document.text_uploaded"
    INGEST_SUCCEEDED = "document.ingest_succeeded"
    INGEST_FAILED = "document.ingest_failed"
    INGEST_RETRY_SCHEDULED = "document.ingest_retry_scheduled"
    QUESTION_ASKED = "question.asked"
    QUESTION_FAILED = "question.failed"
    ANSWER_CREATED = "answer.created"
    REVIEW_DECIDED = "review.decided"


def record(
    session: AsyncSession,
    *,
    action: str,
    org_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None = None,
    target_type: str | None = None,
    target_id: uuid.UUID | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AuditLog:
    meta = dict(metadata or {})
    leaked = FORBIDDEN_METADATA_KEYS.intersection(meta)
    if leaked:
        raise ValueError(f"audit metadata must not contain {sorted(leaked)}")
    entry = AuditLog(
        id=uuid.uuid4(),
        org_id=org_id,
        actor_user_id=actor_user_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id) if target_id is not None else None,
        metadata_=meta,
        request_id=request_id_var.get(),
    )
    session.add(entry)
    return entry
