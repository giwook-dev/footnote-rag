"""agent_runs, agent_steps, llm_calls.agent_run_id

Revision ID: 0005_agent_runs
Revises: 0004_note_comparisons
Create Date: 2026-11-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_agent_runs"
down_revision: str | None = "0004_note_comparisons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Tool-using agent runs and their steps (docs/adr/0013-tool-using-agent.md).
    op.execute(
        """
        CREATE TABLE agent_runs (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            user_id uuid NOT NULL REFERENCES users(id),
            question text NOT NULL,
            status varchar(20) NOT NULL
                CHECK (status IN ('running', 'completed', 'needs_review', 'failed')),
            answer text,
            citations jsonb NOT NULL DEFAULT '[]'::jsonb,
            review_reasons jsonb NOT NULL DEFAULT '[]'::jsonb,
            model varchar(100) NOT NULL,
            step_count integer NOT NULL DEFAULT 0,
            tool_call_count integer NOT NULL DEFAULT 0,
            latency_ms integer,
            error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_runs_org_id ON agent_runs (org_id)")
    op.execute("CREATE INDEX ix_agent_runs_org_created ON agent_runs (org_id, created_at)")
    op.execute(
        """
        CREATE TABLE agent_steps (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
            idx integer NOT NULL,
            kind varchar(10) NOT NULL CHECK (kind IN ('model', 'tool')),
            tool_name varchar(50),
            args jsonb NOT NULL DEFAULT '{}'::jsonb,
            ok boolean NOT NULL DEFAULT true,
            result_text text NOT NULL DEFAULT '',
            sources jsonb NOT NULL DEFAULT '[]'::jsonb,
            latency_ms integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_agent_steps_run ON agent_steps (run_id, idx)")
    # Agent model turns are LLM usage too (answer_id stays NULL for them).
    op.execute(
        "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS agent_run_id uuid REFERENCES agent_runs(id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS agent_run_id")
    op.execute("DROP TABLE IF EXISTS agent_steps")
    op.execute("DROP TABLE IF EXISTS agent_runs")
