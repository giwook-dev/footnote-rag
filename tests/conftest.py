"""Test configuration.

Fake mode is forced BEFORE any application module is imported:
* ``.env`` is ignored (FOOTNOTE_DISABLE_DOTENV=1) so real keys are never read,
* provider keys are removed from the process environment,
* every provider is pinned to ``fake``.
Tests therefore never reach Claude, Voyage or OpenDART over the network.
"""

from __future__ import annotations

import os
import tempfile

_TEST_DATA_DIR = tempfile.mkdtemp(prefix="footnote-rag-test-")
for _key in ("ANTHROPIC_API_KEY", "VOYAGE_API_KEY", "OPENDART_API_KEY"):
    os.environ.pop(_key, None)
os.environ.update(
    {
        "FOOTNOTE_DISABLE_DOTENV": "1",
        "LLM_PROVIDER": "fake",
        "EMBEDDER": "fake",
        "DART_PROVIDER": "fake",
        "DATA_DIR": _TEST_DATA_DIR,
        "LOG_JSON": "true",
        "BASE_BACKOFF_SECONDS": "5",
        "JOB_MAX_ATTEMPTS": "3",
        "MIN_RETRIEVAL_SCORE": "0.05",
    }
)
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://footnote:footnote@127.0.0.1:55439/footnote_test"
)

import asyncio  # noqa: E402
import uuid  # noqa: E402
from collections.abc import AsyncIterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.ext.asyncio import create_async_engine  # noqa: E402

from footnote_rag.config import get_settings  # noqa: E402


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def _database() -> None:
    """Fresh schema per test session, built by the real Alembic migrations."""
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    assert settings.llm_mode == "fake" and settings.embedder_mode == "fake"
    assert settings.dart_mode == "fake"
    asyncio.run(_reset_schema(settings.database_url))
    cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(os.path.dirname(__file__), "..", "alembic"))
    command.upgrade(cfg, "head")


@pytest.fixture(autouse=True)
async def _clean_jobs() -> AsyncIterator[None]:
    """Jobs and the embedding cache are global (not org-scoped); keep tests independent."""
    from footnote_rag.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        await session.execute(text("DELETE FROM jobs"))
        await session.execute(text("DELETE FROM embedding_cache"))
        await session.commit()
    yield


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def sessionmaker():
    from footnote_rag.db.session import get_sessionmaker

    return get_sessionmaker()


@pytest.fixture
def fake_llm():
    from footnote_rag.answering.llm import FakeLLM

    return FakeLLM()


@pytest.fixture
def fake_embedder(settings):
    from footnote_rag.retrieval.embedder import FakeEmbedder

    return FakeEmbedder(dim=settings.embedding_dim)


@pytest.fixture
def fake_dart():
    from footnote_rag.dart.client import FakeDartClient

    return FakeDartClient()


@pytest.fixture
def app(fake_llm, fake_embedder):
    from footnote_rag.main import create_app

    application = create_app()
    application.state.embedder = fake_embedder
    application.state.llm = fake_llm
    return application


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def worker(sessionmaker, settings, fake_embedder, fake_dart):
    from footnote_rag.jobs.handlers import JobContext
    from footnote_rag.jobs.worker import Worker

    return Worker(
        sessionmaker=sessionmaker,
        context=JobContext(sessionmaker, settings, fake_embedder, fake_dart),
        worker_id="test-worker",
    )


@dataclass
class OrgUsers:
    org_id: uuid.UUID
    admin_id: uuid.UUID
    tokens: dict[str, str]
    emails: dict[str, str]

    def headers(self, role: str = "admin") -> dict[str, str]:
        return {"Authorization": f"Bearer {self.tokens[role]}"}


PASSWORD = "correct-horse-battery"


@pytest.fixture
def make_org(sessionmaker, client):
    async def _make(name: str | None = None) -> OrgUsers:
        from footnote_rag.orgs.service import create_user, seed_org

        suffix = uuid.uuid4().hex[:8]
        emails = {role: f"{role}-{suffix}@example.com" for role in ("admin", "reviewer", "member")}
        async with sessionmaker() as session:
            org, admin = await seed_org(
                session,
                org_name=name or f"org-{suffix}",
                admin_email=emails["admin"],
                admin_password=PASSWORD,
            )
            for role in ("reviewer", "member"):
                await create_user(
                    session,
                    org_id=org.id,
                    email=emails[role],
                    password=PASSWORD,
                    role=role,
                    actor_user_id=admin.id,
                )
            await session.commit()
        tokens = {}
        for role, email in emails.items():
            resp = await client.post("/auth/token", data={"username": email, "password": PASSWORD})
            assert resp.status_code == 200, resp.text
            tokens[role] = resp.json()["access_token"]
        return OrgUsers(org.id, admin.id, tokens, emails)

    return _make


@pytest.fixture
def ingest(client, worker):
    """Import a (synthetic) DART filing via the API and run the worker until idle."""

    async def _ingest(org: OrgUsers, rcept_no: str = "20260310900001") -> str:
        resp = await client.post(
            "/documents/dart", json={"rcept_no": rcept_no}, headers=org.headers("member")
        )
        assert resp.status_code in (200, 202), resp.text
        while await worker.run_once():
            pass
        return resp.json()["document_id"]

    return _ingest
