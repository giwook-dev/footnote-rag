"""Password hashing (argon2) and JWT access tokens."""

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
