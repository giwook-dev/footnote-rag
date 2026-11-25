"""note_comparisons, note_comparison_summaries, llm_calls.note_comparison_id

Revision ID: 0004_note_comparisons
Revises: 0003_llm_calls_dropped_refs
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004_note_comparisons"
down_revision: str | None = "0003_llm_calls_dropped_refs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Prior vs current annual-report note comparison (docs/adr/0012-note-change-analysis.md).
    op.execute(
        """
        CREATE TABLE note_comparisons (
            id uuid PRIMARY KEY,
            org_id uuid NOT NULL REFERENCES organizations(id),
            created_by uuid NOT NULL REFERENCES users(id),
            prior_document_id uuid NOT NULL REFERENCES documents(id),
            current_document_id uuid NOT NULL REFERENCES documents(id),
            scope varchar(20) NOT NULL,
            result jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_note_comparisons_org_id ON note_comparisons (org_id)")
    op.execute(
        """
        CREATE TABLE note_comparison_summaries (
            id uuid PRIMARY KEY,
            comparison_id uuid NOT NULL REFERENCES note_comparisons(id) ON DELETE CASCADE,
            org_id uuid NOT NULL,
            note_index integer NOT NULL,
            note_key text NOT NULL,
            bullets jsonb NOT NULL DEFAULT '[]'::jsonb,
            status varchar(20) NOT NULL CHECK (status IN ('ok', 'needs_review')),
            ungrounded_numbers jsonb NOT NULL DEFAULT '[]'::jsonb,
            model varchar(100) NOT NULL,
            created_by uuid NOT NULL REFERENCES users(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_note_comparison_summaries_note "
        "ON note_comparison_summaries (comparison_id, note_index, created_at)"
    )
    # Summary calls are LLM usage too (answer_id stays NULL for them).
    op.execute(
        "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS note_comparison_id uuid "
        "REFERENCES note_comparisons(id)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS note_comparison_id")
    op.execute("DROP TABLE IF EXISTS note_comparison_summaries")
    op.execute("DROP TABLE IF EXISTS note_comparisons")
