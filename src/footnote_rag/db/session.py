"""비동기 엔진(asyncpg)과 세션 팩토리.

역할: 프로세스당 엔진 하나를 지연 생성하고, 요청마다 세션 하나를 주는 FastAPI 의존성을 제공한다.
주요 요소: ``get_engine``, ``get_sessionmaker``, ``get_session``, ``dispose_engine``.
호출 관계: 라우터가 ``Depends(get_session)``으로, 워커·CLI·Eval이 ``get_sessionmaker()``로 쓴다.
커밋은 서비스 함수가 명시적으로 한다.
학습 문서: docs/study/04-job-queue.md (트랜잭션 경계)

Async engine and session factory.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from footnote_rag.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request. Services commit explicitly."""
    async with get_sessionmaker()() as session:
        yield session
