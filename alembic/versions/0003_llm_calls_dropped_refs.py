"""llm_calls.dropped_source_refs: out-of-range source numbers dropped from local-model answers

Revision ID: 0003_llm_calls_dropped_refs
Revises: 0002_embedding_cache
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003_llm_calls_dropped_refs"
down_revision: str | None = "0002_embedding_cache"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Local (Ollama) models return source numbers in JSON; numbers outside 1..k are dropped
    # and counted here (docs/adr/0008-local-llm-ollama.md). Claude rows stay 0.
    op.execute(
        "ALTER TABLE llm_calls ADD COLUMN IF NOT EXISTS dropped_source_refs integer NOT NULL DEFAULT 0"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN IF EXISTS dropped_source_refs")
