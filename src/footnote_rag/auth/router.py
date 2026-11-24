"""로그인(``POST /auth/token``)과 사용자 엔드포인트(``/users/me``, ``POST /users``).

역할: OAuth2 password flow로 이메일·비밀번호를 검증해 JWT를 발급하고, 성공·실패를 감사 로그에 남긴다.
주요 요소: ``login``, ``me``, ``create_user_endpoint``.
호출 관계: ``orgs.service``(사용자 조회·생성), ``auth.security``(해시 검증·토큰 생성), ``audit.service.record``.
학습 문서: docs/study/03-auth-rbac-isolation.md

/auth/token and /users endpoints.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.audit.service import Actions, record
from footnote_rag.auth.deps import ADMINS, ANY_MEMBER, require_role
from footnote_rag.auth.security import create_access_token, verify_password
from footnote_rag.db.models import User
from footnote_rag.db.session import get_session
from footnote_rag.orgs.service import EmailTakenError, create_user, get_user_by_email

router = APIRouter(tags=["auth"])


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class UserOut(BaseModel):
    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=200)
    role: Literal["admin", "reviewer", "member"] = "member"


@router.post("/auth/token", response_model=TokenOut)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: AsyncSession = Depends(get_session),
) -> TokenOut:
    user = await get_user_by_email(session, form.username)
    ok = verify_password(form.password, user.password_hash if user else None)
    if not ok or user is None or not user.is_active:
        record(
            session,
            action=Actions.LOGIN_FAILED,
            org_id=user.org_id if user else None,
            actor_user_id=user.id if user else None,
            target_type="user",
            target_id=user.id if user else None,
            metadata={"email": form.username.lower()[:320]},
        )
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    record(
        session,
        action=Actions.LOGIN_SUCCEEDED,
        org_id=user.org_id,
        actor_user_id=user.id,
        target_type="user",
        target_id=user.id,
    )
    await session.commit()
    return TokenOut(
        access_token=create_access_token(user_id=user.id, org_id=user.org_id, role=user.role)
    )


@router.get("/users/me", response_model=UserOut)
async def me(user: User = Depends(require_role(*ANY_MEMBER))) -> User:
    return user


@router.post("/users", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def create_user_endpoint(
    body: UserCreate,
    admin: User = Depends(require_role(*ADMINS)),
    session: AsyncSession = Depends(get_session),
) -> User:
    try:
        user = await create_user(
            session,
            org_id=admin.org_id,
            email=body.email,
            password=body.password,
            role=body.role,
            actor_user_id=admin.id,
        )
    except EmailTakenError:
        raise HTTPException(status_code=409, detail="Email already registered") from None
    await session.commit()
    return user
