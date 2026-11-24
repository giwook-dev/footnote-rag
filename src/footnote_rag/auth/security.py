"""비밀번호 해시(argon2, pwdlib)와 JWT 발급·검증.

역할: 비밀번호를 해시·검증하고(없는 이메일도 더미 해시로 비슷한 시간 소비), HS256 JWT를 만들고 푼다.
주요 요소: ``hash_password``, ``verify_password``, ``create_access_token``, ``decode_access_token``,
``TokenClaims``(sub=user_id, org_id, role).
호출 관계: ``auth.router.login``과 ``auth.deps.get_current_user``가 호출한다.
학습 문서: docs/study/03-auth-rbac-isolation.md

Password hashing (argon2) and JWT access tokens.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash

from footnote_rag.config import get_settings

_hasher = PasswordHash.recommended()
# Used to keep timing similar when the email does not exist.
_DUMMY_HASH = _hasher.hash("timing-equalizer")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    if password_hash is None:
        _hasher.verify(password, _DUMMY_HASH)
        return False
    return _hasher.verify(password, password_hash)


@dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: str


def create_access_token(*, user_id: uuid.UUID, org_id: uuid.UUID, role: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=s.access_token_expire_minutes)).timestamp()),
    }
    return jwt.encode(payload, s.jwt_secret.get_secret_value(), algorithm=s.jwt_algorithm)


def decode_access_token(token: str) -> TokenClaims:
    """Raises ``jwt.PyJWTError`` (or ValueError/KeyError) for invalid tokens."""
    s = get_settings()
    payload = jwt.decode(
        token,
        s.jwt_secret.get_secret_value(),
        algorithms=[s.jwt_algorithm],
        options={"require": ["sub", "org_id", "role", "exp"]},
    )
    return TokenClaims(
        user_id=uuid.UUID(payload["sub"]),
        org_id=uuid.UUID(payload["org_id"]),
        role=str(payload["role"]),
    )
