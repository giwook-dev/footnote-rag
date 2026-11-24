"""initial schema: orgs, users, documents, chunks (pgvector), jobs, Q&A, usage, audit

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

from footnote_rag.config import get_settings

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dim = int(get_settings().embedding_dim)

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("CREATE TYPE user_role AS ENUM ('admin', 'reviewer', 'member')")
    op.execute("CREATE TYPE document_source AS ENUM ('dart', 'upload')")
    op.execute("CREATE TYPE document_status AS ENUM ('queued', 'processing', 'ready', 'failed')")
    op.execute("CREATE TYPE job_status AS ENUM ('queued', 'running', 'succeeded', 'failed')")
    op.execute(
        "CREATE TYPE answer_status AS ENUM ('published', 'pending_review', 'approved', 'rejected')"
    )

    op.execute(
        """
        CREATE TABLE organizations (
            id uuid PRIMARY KEY,
            name varchar(200) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            email varchar(320) NOT NULL UNIQUE,
            password_hash varchar(255) NOT NULL,
            role user_role NOT NULL,
            is_active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_users_org_id ON users (org_id)")

    op.execute(
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            created_by uuid NOT NULL REFERENCES users(id),
            title varchar(500) NOT NULL,
            source document_source NOT NULL,
            source_ref varchar(200),
            meta jsonb NOT NULL DEFAULT '{}'::jsonb,
            status document_status NOT NULL DEFAULT 'queued',
            section_count integer NOT NULL DEFAULT 0,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT uq_documents_source UNIQUE (org_id, source, source_ref)
        )
        """
    )
    op.execute("CREATE INDEX ix_documents_org_id ON documents (org_id)")

    op.execute(
        f"""
        CREATE TABLE chunks (
            id uuid PRIMARY KEY,
            document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            org_id uuid NOT NULL,
            section_path text NOT NULL,
            chunk_index integer NOT NULL,
            text text NOT NULL,
            embedding vector({dim}) NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_chunks_org_id ON chunks (org_id)")
    op.execute("CREATE INDEX ix_chunks_document_id ON chunks (document_id)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks USING hnsw (embedding vector_cosine_ops)"
    )

    op.execute(
        """
        CREATE TABLE jobs (
            id uuid PRIMARY KEY,
            type varchar(50) NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            status job_status NOT NULL DEFAULT 'queued',
            attempts integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 5,
            run_after timestamptz NOT NULL DEFAULT now(),
            locked_at timestamptz,
            locked_by varchar(200),
            last_error text,
            result jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_jobs_claim ON jobs (status, run_after, created_at) WHERE status = 'queued'"
    )

    op.execute(
        """
        CREATE TABLE questions (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            asked_by uuid NOT NULL REFERENCES users(id),
            text text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_questions_org_id ON questions (org_id)")

    op.execute(
        """
        CREATE TABLE answers (
            id uuid PRIMARY KEY,
            question_id uuid NOT NULL REFERENCES questions(id),
            org_id uuid NOT NULL,
            text text NOT NULL,
            footnotes jsonb NOT NULL DEFAULT '[]'::jsonb,
            status answer_status NOT NULL,
            review_reason varchar(200),
            reviewed_by uuid REFERENCES users(id),
            reviewed_at timestamptz,
            review_comment text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_answers_org_status ON answers (org_id, status)")

    op.execute(
        """
        CREATE TABLE llm_calls (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL,
            answer_id uuid REFERENCES answers(id),
            model varchar(100) NOT NULL,
            input_tokens integer NOT NULL DEFAULT 0,
            output_tokens integer NOT NULL DEFAULT 0,
            cache_read_input_tokens integer NOT NULL DEFAULT 0,
            cache_creation_input_tokens integer NOT NULL DEFAULT 0,
            cost_usd numeric(12, 6),
            latency_ms integer NOT NULL,
            stop_reason varchar(50),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_llm_calls_org_created ON llm_calls (org_id, created_at)")

    op.execute(
        """
        CREATE TABLE audit_logs (
            id uuid PRIMARY KEY,
            org_id uuid,
            actor_user_id uuid,
            action varchar(100) NOT NULL,
            target_type varchar(50),
            target_id varchar(100),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            request_id varchar(100),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_audit_logs_org_created ON audit_logs (org_id, created_at)")
    op.execute("CREATE INDEX ix_audit_logs_org_action ON audit_logs (org_id, action)")

    # Append-only audit log: reject UPDATE / DELETE / TRUNCATE at the database level.
    op.execute(
        """
        CREATE FUNCTION audit_logs_append_only() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_logs is append-only (% rejected)', TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER audit_logs_no_update_delete BEFORE UPDATE OR DELETE ON audit_logs "
        "FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only()"
    )
    op.execute(
        "CREATE TRIGGER audit_logs_no_truncate BEFORE TRUNCATE ON audit_logs "
        "FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS audit_logs")
    op.execute("DROP FUNCTION IF EXISTS audit_logs_append_only()")
    op.execute("DROP TABLE IF EXISTS llm_calls")
    op.execute("DROP TABLE IF EXISTS answers")
    op.execute("DROP TABLE IF EXISTS questions")
    op.execute("DROP TABLE IF EXISTS jobs")
    op.execute("DROP TABLE IF EXISTS chunks")
    op.execute("DROP TABLE IF EXISTS documents")
    op.execute("DROP TABLE IF EXISTS users")
    op.execute("DROP TABLE IF EXISTS organizations")
    for t in ("answer_status", "job_status", "document_status", "document_source", "user_role"):
        op.execute(f"DROP TYPE IF EXISTS {t}")
