"""BM25 columns on chunks and per-scorer calibrations (ADR 020, ADR 021).

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-22
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- The text search config an index was built with (like its embedding model).
        ALTER TABLE vector_indexes ADD COLUMN text_config text NOT NULL DEFAULT 'english';

        -- Lexical search: the chunk's lexemes and its length in tokens, written by the store.
        ALTER TABLE chunks ADD COLUMN tsv tsvector NOT NULL DEFAULT ''::tsvector;
        ALTER TABLE chunks ADD COLUMN doc_len integer NOT NULL DEFAULT 0;
        UPDATE chunks SET tsv = to_tsvector('english', text);
        UPDATE chunks SET doc_len = (
            SELECT coalesce(sum(coalesce(array_length(u.positions, 1), 1)), 0)
            FROM unnest(chunks.tsv) u
        );
        CREATE INDEX chunks_tsv_idx ON chunks USING gin (tsv);

        -- Calibrated thresholds per index AND per scorer: the score they apply to and every
        -- retrieval step that shapes it. Enabling rerank or HyDE needs a new row (ADR 011).
        CREATE TABLE retrieval_calibrations (
            index_id       integer NOT NULL REFERENCES vector_indexes(id) ON DELETE CASCADE,
            scorer         text NOT NULL,
            min_score      double precision NOT NULL,
            min_hits       integer NOT NULL CHECK (min_hits >= 1),
            metrics        jsonb NOT NULL DEFAULT '{}',
            dataset        text NOT NULL DEFAULT '',
            calibrated_at  timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (index_id, scorer)
        );

        -- Milestone 1 stored one pair per index; it could only have come from plain dense.
        INSERT INTO retrieval_calibrations (index_id, scorer, min_score, min_hits, calibrated_at)
        SELECT id, 'dense|dense', min_score, min_hits, coalesce(calibrated_at, now())
        FROM vector_indexes WHERE min_score IS NOT NULL AND min_hits IS NOT NULL;

        ALTER TABLE vector_indexes
            DROP COLUMN min_score, DROP COLUMN min_hits, DROP COLUMN calibrated_at;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE vector_indexes
            ADD COLUMN min_score double precision,
            ADD COLUMN min_hits integer,
            ADD COLUMN calibrated_at timestamptz;
        UPDATE vector_indexes v SET min_score = c.min_score, min_hits = c.min_hits,
                                    calibrated_at = c.calibrated_at
        FROM retrieval_calibrations c WHERE c.index_id = v.id AND c.scorer = 'dense|dense';
        DROP TABLE IF EXISTS retrieval_calibrations;
        DROP INDEX IF EXISTS chunks_tsv_idx;
        ALTER TABLE chunks DROP COLUMN IF EXISTS tsv, DROP COLUMN IF EXISTS doc_len;
        ALTER TABLE vector_indexes DROP COLUMN IF EXISTS text_config;
        """
    )
