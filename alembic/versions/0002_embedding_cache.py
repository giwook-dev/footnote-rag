"""embedding_cache: reuse document embeddings across jobs, retries and reprocessing

Revision ID: 0002_embedding_cache
Revises: 0001_initial
Create Date: 2026-11-24
"""

from collections.abc import Sequence

from alembic import op

from footnote_rag.config import get_settings

revision: str = "0002_embedding_cache"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    dim = int(get_settings().embedding_dim)
    # key = sha256(model | dim | input_type | text). No text and no org id are stored:
    # see docs/adr/0007-embedding-rate-limit-and-cache.md.
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS embedding_cache (
            key text PRIMARY KEY,
            model text NOT NULL,
            dim integer NOT NULL,
            embedding vector({dim}) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS embedding_cache")
