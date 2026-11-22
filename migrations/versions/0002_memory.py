"""Rolling conversation summaries and long-term user facts (ADR 016).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-22
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- One row per conversation: the rolling summary and how far each memory job got.
        -- Text is redacted before it is written (ADR 015).
        CREATE TABLE conversation_summaries (
            conversation_id   text PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
            user_id           text,
            summary           text NOT NULL DEFAULT '',
            summarized_until  bigint NOT NULL DEFAULT 0,   -- last turn id folded into summary
            facts_until       bigint NOT NULL DEFAULT 0,   -- last turn id read for facts
            updated_at        timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX conversation_summaries_user_idx ON conversation_summaries (user_id);

        -- Long-term memory: facts about a user, kept across conversations.
        CREATE TABLE user_facts (
            id                      bigserial PRIMARY KEY,
            user_id                 text NOT NULL,
            fact                    text NOT NULL,
            fact_key                text NOT NULL,         -- normalised text, for dedup
            source_conversation_id  text,
            created_at              timestamptz NOT NULL DEFAULT now(),
            updated_at              timestamptz NOT NULL DEFAULT now(),
            UNIQUE (user_id, fact_key)
        );
        CREATE INDEX user_facts_recent_idx ON user_facts (user_id, updated_at DESC);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE IF EXISTS user_facts;
        DROP TABLE IF EXISTS conversation_summaries;
        """
    )
