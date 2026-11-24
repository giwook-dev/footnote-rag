"""FastAPI application."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from footnote_rag import __version__
from footnote_rag.answering.router import router as answering_router
from footnote_rag.audit.router import router as audit_router
from footnote_rag.auth.router import router as auth_router
from footnote_rag.config import DEV_JWT_SECRET, get_settings
from footnote_rag.db.session import dispose_engine, get_sessionmaker
from footnote_rag.documents.router import router as documents_router
from footnote_rag.observability import configure_logging, get_logger, request_id_var
from footnote_rag.providers import make_embedder, make_llm
from footnote_rag.reviews.router import router as reviews_router
from footnote_rag.usage.router import router as usage_router

log = get_logger("footnote_rag.http")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.app_env == "production" and settings.jwt_secret.get_secret_value() == (
        DEV_JWT_SECRET
    ):
        raise RuntimeError("JWT_SECRET must be set in production")
    if not hasattr(app.state, "embedder"):
        app.state.embedder = make_embedder(settings)
    if not hasattr(app.state, "llm"):
        app.state.llm = make_llm(settings)
    log.info(
        "app.started",
        llm=settings.llm_mode,
        embedder=settings.embedder_mode,
        dart=settings.dart_mode,
        model=settings.claude_model,
    )
    yield
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(
        title="footnote-rag",
        version=__version__,
        description="DART 감사보고서 기반 각주형 RAG 답변 API",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("X-Request-ID", "")
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(rid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("http.unhandled_error", method=request.method, path=request.url.path)
            response = JSONResponse({"detail": "Internal Server Error"}, status_code=500)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        response.headers["X-Request-ID"] = rid
        log.info(
            "http.request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=duration_ms,
        )
        request_id_var.reset(token)
        return response

    @app.get("/healthz", tags=["ops"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["ops"])
    async def readyz() -> JSONResponse:
        try:
            async with get_sessionmaker()() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001
            log.warning("readyz.db_unavailable", error=type(exc).__name__)
            return JSONResponse({"status": "unavailable", "database": "down"}, status_code=503)
        return JSONResponse({"status": "ready", "database": "ok"})

    app.include_router(auth_router)
    app.include_router(documents_router)
    app.include_router(answering_router)
    app.include_router(reviews_router)
    app.include_router(audit_router)
    app.include_router(usage_router)
    return app


app = create_app()
