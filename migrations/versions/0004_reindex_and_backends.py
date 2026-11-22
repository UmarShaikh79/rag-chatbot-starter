"""Safe re-indexing and a second vector store (ADR 023, ADR 024).

* ``vector_indexes.backend``: which store holds the vectors (pgvector or qdrant). Postgres
  stays the catalog for both: index metadata, aliases, documents and calibrations.
* ``vector_indexes.status``: ``building`` while ``chatbot reindex`` fills a new index,
  ``ready`` when it may take traffic. ``build_report`` keeps what the build saw.
* ``index_aliases.previous_index_id``: where the alias pointed before the last switch, so
  ``chatbot index rollback`` can flip it back.
* ``documents.sections``: the parsed text of each document, so a re-index can rebuild
  documents that no configured source can re-read (pushed through the API, for example).
  Rows written before this migration have NULL here.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-22
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE vector_indexes
            ADD COLUMN backend text NOT NULL DEFAULT 'pgvector',
            ADD COLUMN status text NOT NULL DEFAULT 'ready'
                CHECK (status IN ('building', 'ready')),
            ADD COLUMN ready_at timestamptz,
            ADD COLUMN build_report jsonb NOT NULL DEFAULT '{}';
        UPDATE vector_indexes SET ready_at = created_at;

        ALTER TABLE index_aliases
            ADD COLUMN previous_index_id integer
                REFERENCES vector_indexes(id) ON DELETE SET NULL;

        ALTER TABLE documents ADD COLUMN sections jsonb;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE documents DROP COLUMN IF EXISTS sections;
        ALTER TABLE index_aliases DROP COLUMN IF EXISTS previous_index_id;
        ALTER TABLE vector_indexes
            DROP COLUMN IF EXISTS build_report,
            DROP COLUMN IF EXISTS ready_at,
            DROP COLUMN IF EXISTS status,
            DROP COLUMN IF EXISTS backend;
        """
    )
