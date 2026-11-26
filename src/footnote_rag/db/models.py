"""SQLAlchemy ORM 모델(테이블 매핑). 스키마 생성은 Alembic 마이그레이션이 맡는다.

역할: organizations, users, documents, chunks(pgvector), embedding_cache(pgvector), jobs, questions,
answers, llm_calls, audit_logs, note_comparisons, note_comparison_summaries, agent_runs, agent_steps 테이블과
상태 enum 값을 정의한다.
주요 요소: ``Document``, ``Chunk``, ``EmbeddingCache``, ``Job``, ``Answer``, ``LlmCall``, ``AuditLog``,
``NoteComparison``, ``NoteComparisonSummary``, ``AgentRun``, ``AgentStep``, ``ANSWER_STATUSES`` 등.
호출 관계: 서비스·라우터·워커가 import해 쿼리한다. 실제 DDL은 alembic/versions/0001_initial.py,
0002_embedding_cache.py, 0003_llm_calls_dropped_refs.py, 0004_note_comparisons.py, 0005_agent_runs.py.
학습 문서: docs/study/01-big-picture.md, ERD는 docs/architecture.md

ORM models. The schema itself is created by Alembic (see alembic/versions).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from footnote_rag.config import get_settings

USER_ROLES = ("admin", "reviewer", "member")
DOCUMENT_SOURCES = ("dart", "upload")
DOCUMENT_STATUSES = ("queued", "processing", "ready", "failed")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
ANSWER_STATUSES = ("published", "pending_review", "approved", "rejected")


def _enum(*values: str, name: str) -> ENUM:
    # Types are created by the migration; never let SQLAlchemy emit CREATE TYPE.
    return ENUM(*values, name=name, create_type=False)


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Organization(Base):
    __tablename__ = "organizations"
    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = _created_at()


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(_enum(*USER_ROLES, name="user_role"), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = _created_at()


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("org_id", "source", "source_ref", name="uq_documents_source"),
    )
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source: Mapped[str] = mapped_column(_enum(*DOCUMENT_SOURCES, name="document_source"))
    source_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        _enum(*DOCUMENT_STATUSES, name="document_status"), nullable=False, default="queued"
    )
    section_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    section_path: Mapped[str] = mapped_column(Text, nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=False
    )


class EmbeddingCache(Base):
    """Document embeddings keyed by ``sha256(model|dim|input_type|text)``.

    Shared across organizations on purpose: it stores no text and no org id, and the same
    public filing text yields the same vector for everyone (docs/adr/0007).
    """

    __tablename__ = "embedding_cache"
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(
        Vector(get_settings().embedding_dim), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    type: Mapped[str] = mapped_column(String(50), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(
        _enum(*JOB_STATUSES, name="job_status"), nullable=False, default="queued"
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Question(Base):
    __tablename__ = "questions"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    asked_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Answer(Base):
    __tablename__ = "answers"
    id: Mapped[uuid.UUID] = _uuid_pk()
    question_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("questions.id"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    footnotes: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(_enum(*ANSWER_STATUSES, name="answer_status"))
    review_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()


class LlmCall(Base):
    __tablename__ = "llm_calls"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    answer_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("answers.id"), nullable=True
    )
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_creation_input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Local models only: source numbers outside 1..k that were dropped (0 for Claude).
    dropped_source_refs: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # Note change summaries (comparison.service.summarize_note): set instead of ``answer_id``.
    note_comparison_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("note_comparisons.id"), nullable=True
    )
    # Agent model turns (agent.runner): set instead of ``answer_id``.
    agent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id"), nullable=True
    )
    created_at: Mapped[datetime] = _created_at()


AGENT_RUN_STATUSES = ("running", "completed", "needs_review", "failed")
AGENT_STEP_KINDS = ("model", "tool")


class AgentRun(Base):
    """One tool-using agent run (docs/adr/0013-tool-using-agent.md)."""

    __tablename__ = "agent_runs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Sources the final answer cites (full source objects, see agent.tools.Source).
    citations: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    review_reasons: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentStep(Base):
    """One model turn or tool call of an agent run, stored as it happens."""

    __tablename__ = "agent_steps"
    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agent_runs.id", ondelete="CASCADE"), nullable=False
    )
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    tool_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Exactly what the model was given (tool) or what it said (model).
    result_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sources: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created_at()


class NoteComparison(Base):
    """Prior vs current annual-report note comparison (``comparison.notes`` result as JSON)."""

    __tablename__ = "note_comparisons"
    id: Mapped[uuid.UUID] = _uuid_pk()
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    prior_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    current_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


SUMMARY_STATUSES = ("ok", "needs_review")


class NoteComparisonSummary(Base):
    """Local-model summary of one note's changes; regenerating adds a row (latest wins)."""

    __tablename__ = "note_comparison_summaries"
    id: Mapped[uuid.UUID] = _uuid_pk()
    comparison_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("note_comparisons.id", ondelete="CASCADE"), nullable=False
    )
    org_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    note_index: Mapped[int] = mapped_column(Integer, nullable=False)
    note_key: Mapped[str] = mapped_column(Text, nullable=False)
    bullets: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    ungrounded_numbers: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[uuid.UUID] = _uuid_pk()
    # Nullable: a failed login for an unknown email has no organization.
    org_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict
    )
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = _created_at()
