# syntax=docker/dockerfile:1

# ---- build stage: resolve dependencies with uv into /app/.venv ----
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
# src includes the build-free web UI (src/footnote_rag/web/static) served at /ui
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev

# ---- runtime stage: slim image, non-root user, no secrets baked in ----
FROM python:3.12-slim-bookworm AS runtime
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --no-create-home app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
RUN mkdir -p /app/data && chown app:app /app/data
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/app/data
USER app
EXPOSE 8000
CMD ["uvicorn", "footnote_rag.main:app", "--host", "0.0.0.0", "--port", "8000"]
