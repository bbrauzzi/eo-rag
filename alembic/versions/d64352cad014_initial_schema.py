"""initial schema

Revision ID: d64352cad014
Revises:
Create Date: 2026-08-17 13:27:51.797345

Raw, idempotent SQL throughout - CREATE ... IF NOT EXISTS, mirroring the old
scripts/init_db.sql exactly - rather than op.create_table, so this one revision is
safe to run both against a brand new database *and* against one that mechanism already
initialized (every volume created before migrations existed). Either way the only new
thing this adds is the alembic_version table Alembic itself tracks.

The embedding dimension is read from settings rather than hardcoded, so it no longer
needs to be kept in step by hand with `settings.embedding_dim` and `Vector(...)` in
app/db/models.py - changing the model still means truncating and re-ingesting (vectors
from a different model aren't comparable), but the column width now follows from one
place instead of three.
"""

from collections.abc import Sequence

from alembic import op
from app.config import settings

# revision identifiers, used by Alembic.
revision: str = "d64352cad014"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS doc_chunks (
            id BIGSERIAL PRIMARY KEY,
            content TEXT NOT NULL,
            embedding vector({settings.embedding_dim}),
            source TEXT NOT NULL,
            section TEXT,
            url TEXT,
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )

    op.execute(
        """
        CREATE INDEX IF NOT EXISTS doc_chunks_embedding_idx
            ON doc_chunks USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS eval_cases (
            id BIGSERIAL PRIMARY KEY,
            question TEXT NOT NULL,
            expected_answer TEXT,
            expected_tool_calls TEXT[],
            created_at TIMESTAMPTZ DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS eval_cases")
    op.execute("DROP INDEX IF EXISTS doc_chunks_embedding_idx")
    op.execute("DROP TABLE IF EXISTS doc_chunks")
