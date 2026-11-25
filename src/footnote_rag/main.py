"""FastAPI 앱 진입점.

역할: 라우터를 묶고, 요청마다 ``X-Request-ID``를 붙이는 미들웨어와 ``/healthz``·``/readyz``를 둔다.
빌드 없는 웹 화면(``web/static``)을 ``/ui``에 마운트하고 ``/ui`` 응답에만 CSP 등 보안 헤더를 붙인다(``/``는 ``/ui/``로 리다이렉트).
주요 요소: ``create_app``(앱 조립), ``lifespan``(로깅 설정, 운영 JWT 검사, 임베더·LLM·상향용 fallback LLM 생성).
호출 관계: uvicorn이 ``app``을 띄우고, 앱은 각 ``*/router.py``(``comparison.router``의 전기 대비 주석 변동 분석 포함)와
``providers``를 호출한다.
학습 문서: docs/study/01-big-picture.md, docs/study/09-testing-and-ops.md

FastAPI application.
"""

from __future__ import annotations

import mimetypes
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from footnote_rag import __version__
from footnote_rag.answering.router import router as answering_router
from footnote_rag.audit.router import router as audit_router
from footnote_rag.auth.router import router as auth_router
from footnote_rag.comparison.router import router as comparison_router
from footnote_rag.config import DEV_JWT_SECRET, get_settings
from footnote_rag.dart.router import router as dart_router
from footnote_rag.db.session import dispose_engine, get_sessionmaker
from footnote_rag.documents.router import router as documents_router
from footnote_rag.observability import configure_logging, get_logger, request_id_var
from footnote_rag.providers import make_embedder, make_fallback_llm, make_llm
from footnote_rag.reviews.router import router as reviews_router
from footnote_rag.usage.router import router as usage_router

log = get_logger("footnote_rag.http")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# Build-free web UI (vanilla HTML/CSS/JS) served at /ui.
STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"
UI_PREFIX = "/ui"
# Applied only to /ui: Swagger (/docs) loads its assets from a CDN and must keep working.
UI_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
# Some Windows registries map .js to text/plain, which browsers refuse to run under nosniff.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def _is_ui_path(path: str) -> bool:
    return path == UI_PREFIX or path.startswith(UI_PREFIX + "/")


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
    if not hasattr(app.state, "fallback_llm"):
        # Local cascade target; None unless LLM_PROVIDER=local and LOCAL_LLM_FALLBACK_MODEL is set.
        app.state.fallback_llm = make_fallback_llm(settings)
    log.info(
        "app.started",
        llm=settings.llm_mode,
        embedder=settings.embedder_mode,
        dart=settings.dart_mode,
        model=getattr(app.state.llm, "model", None),
        fallback_model=getattr(app.state.fallback_llm, "model", None),
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
        if _is_ui_path(request.url.path):
            response.headers.update(UI_SECURITY_HEADERS)
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

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse(url=f"{UI_PREFIX}/", status_code=307)

    app.include_router(auth_router)
    app.include_router(documents_router)
    app.include_router(dart_router)
    app.include_router(answering_router)
    app.include_router(reviews_router)
    app.include_router(audit_router)
    app.include_router(usage_router)
    app.include_router(comparison_router)
    app.mount(UI_PREFIX, StaticFiles(directory=STATIC_DIR, html=True), name="ui")
    return app


app = create_app()
