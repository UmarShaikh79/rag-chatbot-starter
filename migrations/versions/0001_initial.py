"""Initial schema: indexes, documents, chunks, jobs, conversations, turns.

Revision ID: 0001
Revises:
Create Date: 2026-11-22
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        -- A physical index: one embedding model and dimension (ADR 005).
        CREATE TABLE vector_indexes (
            id               serial PRIMARY KEY,
            name             text NOT NULL UNIQUE,
            embedding_model  text NOT NULL,
            dims             integer NOT NULL CHECK (dims > 0 AND dims <= 16000),
            chunker_version  text NOT NULL,
            min_score        double precision,     -- calibrated threshold (ADR 011), NULL = uncalibrated
            min_hits         integer,
            calibrated_at    timestamptz,
            created_at       timestamptz NOT NULL DEFAULT now()
        );

        -- The name used in config (vector_store.index) points at one physical index.
        CREATE TABLE index_aliases (
            alias       text PRIMARY KEY,
            index_id    integer NOT NULL REFERENCES vector_indexes(id),
            updated_at  timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE documents (
            index_id         integer NOT NULL REFERENCES vector_indexes(id) ON DELETE CASCADE,
            id               text NOT NULL,
            source           text NOT NULL,
            uri              text NOT NULL,
            title            text NOT NULL,
            content_hash     text NOT NULL,
            chunker_version  text NOT NULL,
            embedding_model  text NOT NULL,
            user_id          text,
            metadata         jsonb NOT NULL DEFAULT '{}',
            updated_at       timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (index_id, id)
        );
        CREATE INDEX documents_source_idx ON documents (index_id, source);
        CREATE INDEX documents_user_idx ON documents (user_id) WHERE user_id IS NOT NULL;

        -- Vectors are untyped here; each physical index gets a partial HNSW index on
        -- (embedding::vector(dims)) WHERE index_id = N, created by the pgvector store.
        CREATE TABLE chunks (
            index_id      integer NOT NULL,
            id            text NOT NULL,
            document_id   text NOT NULL,
            position      integer NOT NULL,
            text          text NOT NULL,
            metadata      jsonb NOT NULL DEFAULT '{}',
            content_hash  text NOT NULL,
            embedding     vector NOT NULL,
            PRIMARY KEY (index_id, id),
            FOREIGN KEY (index_id, document_id)
                REFERENCES documents (index_id, id) ON DELETE CASCADE
        );
        CREATE INDEX chunks_document_idx ON chunks (index_id, document_id);
        CREATE INDEX chunks_metadata_idx ON chunks USING gin (metadata jsonb_path_ops);

        -- Postgres job queue, claimed with FOR UPDATE SKIP LOCKED (ADR 004).
        CREATE TABLE jobs (
            id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind          text NOT NULL,
            payload       jsonb NOT NULL DEFAULT '{}',
            status        text NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
            attempts      integer NOT NULL DEFAULT 0,
            max_attempts  integer NOT NULL DEFAULT 3,
            dedup_key     text,
            user_id       text,
            last_error    text,
            result        jsonb,
            run_after     timestamptz NOT NULL DEFAULT now(),
            locked_at     timestamptz,
            created_at    timestamptz NOT NULL DEFAULT now(),
            updated_at    timestamptz NOT NULL DEFAULT now()
        );
        CREATE UNIQUE INDEX jobs_dedup_idx ON jobs (dedup_key)
            WHERE dedup_key IS NOT NULL AND status IN ('queued', 'running');
        CREATE INDEX jobs_claim_idx ON jobs (status, run_after);
        CREATE INDEX jobs_user_idx ON jobs (user_id) WHERE user_id IS NOT NULL;

        CREATE TABLE conversations (
            id          text PRIMARY KEY,
            user_id     text,
            created_at  timestamptz NOT NULL DEFAULT now(),
            updated_at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX conversations_user_idx ON conversations (user_id);

        -- Turns are stored after PII redaction (ADR 015).
        CREATE TABLE turns (
            id               bigserial PRIMARY KEY,
            conversation_id  text NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_id          text,
            role             text NOT NULL CHECK (role IN ('user', 'assistant')),
            content          text NOT NULL,
            metadata         jsonb NOT NULL DEFAULT '{}',
            created_at       timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX turns_conversation_idx ON turns (conversation_id, id);
        CREATE INDEX turns_user_idx ON turns (user_id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS turns;
        DROP TABLE IF EXISTS conversations;
        DROP TABLE IF EXISTS jobs;
        DROP TABLE IF EXISTS chunks;
        DROP TABLE IF EXISTS documents;
        DROP TABLE IF EXISTS index_aliases;
        DROP TABLE IF EXISTS vector_indexes;
        """
    )
