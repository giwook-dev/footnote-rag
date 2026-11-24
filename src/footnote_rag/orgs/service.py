"""조직과 사용자 관리 서비스.

역할: 이메일(소문자 비교)로 사용자를 찾고, 사용자를 만들며(감사 기록 포함), 첫 조직과 관리자를 시드한다.
주요 요소: ``get_user_by_email``, ``create_user``, ``seed_org``, ``EmailTakenError``.
호출 관계: ``auth.router``, ``cli.cmd_seed``, ``evals.run``, 테스트의 ``make_org``가 호출한다.
학습 문서: docs/study/03-auth-rbac-isolation.md

Organization and user management.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.audit.service import Actions, record
from footnote_rag.auth.security import hash_password
from footnote_rag.db.models import Organization, User


class EmailTakenError(Exception):
    pass


async def get_user_by_email(session: AsyncSession, email: str) -> User | None:
    return await session.scalar(select(User).where(func.lower(User.email) == email.lower()))


async def create_user(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    email: str,
    password: str,
    role: str,
    actor_user_id: uuid.UUID | None,
) -> User:
    if await get_user_by_email(session, email) is not None:
        raise EmailTakenError(email)
    user = User(
        id=uuid.uuid4(),
        org_id=org_id,
        email=email.lower(),
        password_hash=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    record(
        session,
        action=Actions.USER_CREATED,
        org_id=org_id,
        actor_user_id=actor_user_id,
        target_type="user",
        target_id=user.id,
        metadata={"email": user.email, "role": role},
    )
    return user


async def seed_org(
    session: AsyncSession, *, org_name: str, admin_email: str, admin_password: str
) -> tuple[Organization, User]:
    org = Organization(id=uuid.uuid4(), name=org_name)
    session.add(org)
    await session.flush()
    admin = await create_user(
        session,
        org_id=org.id,
        email=admin_email,
        password=admin_password,
        role="admin",
        actor_user_id=None,
    )
    await session.commit()
    return org, admin
