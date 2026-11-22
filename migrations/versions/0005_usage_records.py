"""Token and cost accounting (ADR 028).

One row per operation: a chat turn, a background job, a CLI ingest. ``calls`` keeps the
per-call breakdown (model, role, purpose, tokens, cost) so reports can group by model.
Rows hold no message text. ``forget_user`` clears ``user_id`` and ``conversation_id`` on a
user's rows, keeping the totals (ADR 015).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-22
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE usage_records (
            id bigserial PRIMARY KEY,
            created_at timestamptz NOT NULL DEFAULT now(),
            operation text NOT NULL,
            trace_id text NOT NULL,
            user_id text,
            conversation_id text,
            prompt_tokens integer NOT NULL DEFAULT 0,
            completion_tokens integer NOT NULL DEFAULT 0,
            embedding_tokens integer NOT NULL DEFAULT 0,
            cost_usd numeric(18, 9),
            unpriced_calls integer NOT NULL DEFAULT 0,
            calls jsonb NOT NULL DEFAULT '[]'
        );
        CREATE INDEX usage_records_created_at ON usage_records (created_at);
        CREATE INDEX usage_records_user ON usage_records (user_id, created_at)
            WHERE user_id IS NOT NULL;
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS usage_records")
