"""인증·인가 FastAPI 의존성.

역할: Bearer 토큰을 검증하고 DB에서 사용자를 다시 읽어(토큰의 org_id와 대조) 현재 사용자를 만든다.
주요 요소: ``get_current_user``(401), ``require_role``(403), 역할 묶음 ``ANY_MEMBER``·``REVIEWERS``·``ADMINS``.
호출 관계: 모든 보호된 라우터가 ``Depends(require_role(...))``로 쓰고, 이 모듈은 ``auth.security``를 부른다.
학습 문서: docs/study/03-auth-rbac-isolation.md

Authentication / authorization dependencies.

401 = missing or invalid credentials, 403 = authenticated but role not allowed.
The current user's ``org_id`` scopes every query; cross-org lookups return 404.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from footnote_rag.auth.security import decode_access_token
from footnote_rag.db.models import User
from footnote_rag.db.session import get_session

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token", auto_error=False)

_UNAUTHORIZED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Not authenticated",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_session),
) -> User:
    if not token:
        raise _UNAUTHORIZED
    try:
        claims = decode_access_token(token)
    except (jwt.PyJWTError, ValueError, KeyError):
        raise _UNAUTHORIZED from None
    user = await session.get(User, claims.user_id)
    if user is None or not user.is_active or user.org_id != claims.org_id:
        raise _UNAUTHORIZED
    return user


def require_role(*roles: str) -> Callable[..., Awaitable[User]]:
    allowed = frozenset(roles)

    async def _checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
        return user

    return _checker


ANY_MEMBER = ("admin", "reviewer", "member")
REVIEWERS = ("admin", "reviewer")
ADMINS = ("admin",)
