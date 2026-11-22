"""Conversation memory in Postgres. Text arrives here already redacted (ADR 015).

Rolling summaries and long-term user facts arrive in milestone 2; their tables will be
added to ``forget_user`` when they land.
"""

from __future__ import annotations

import json
import uuid

from psycopg_pool import AsyncConnectionPool

from chatbot.core.errors import ForbiddenError
from chatbot.core.types import ConversationContext, DeletionReport, Turn


class PostgresMemoryStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def load(
        self, user_id: str | None, conversation_id: str | None, history_turns: int
    ) -> ConversationContext:
        if conversation_id is None:
            return ConversationContext(
                conversation_id=uuid.uuid4().hex, user_id=user_id, is_new=True
            )
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT user_id FROM conversations WHERE id = %s", (conversation_id,)
            )
            conv = await cur.fetchone()
            if conv is None:
                return ConversationContext(
                    conversation_id=conversation_id, user_id=user_id, is_new=True
                )
            if conv["user_id"] is not None and conv["user_id"] != user_id:
                raise ForbiddenError("This conversation belongs to a different user")
            cur = await conn.execute(
                """SELECT role, content, created_at, metadata FROM (
                       SELECT * FROM turns WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
                   ) t ORDER BY id""",
                (conversation_id, history_turns),
            )
            rows = await cur.fetchall()
        return ConversationContext(
            conversation_id=conversation_id,
            user_id=conv["user_id"],
            history=[Turn(**r) for r in rows],
        )

    async def save_turns(self, ctx: ConversationContext, turns: list[Turn]) -> None:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                """INSERT INTO conversations (id, user_id) VALUES (%s, %s)
                   ON CONFLICT (id) DO UPDATE SET updated_at = now()""",
                (ctx.conversation_id, ctx.user_id),
            )
            async with conn.cursor() as cur:
                await cur.executemany(
                    """INSERT INTO turns (conversation_id, user_id, role, content, metadata)
                       VALUES (%s, %s, %s, %s, %s)""",
                    [
                        (
                            ctx.conversation_id,
                            ctx.user_id,
                            t.role,
                            t.content,
                            json.dumps(t.metadata),
                        )
                        for t in turns
                    ],
                )

    async def forget_user(self, user_id: str) -> DeletionReport:
        """Delete everything stored for a user and report counts per table."""
        deleted: dict[str, int] = {}
        async with self.pool.connection() as conn, conn.transaction():
            for table, sql in [
                ("turns", "DELETE FROM turns WHERE user_id = %s"),
                ("conversations", "DELETE FROM conversations WHERE user_id = %s"),
                ("jobs", "DELETE FROM jobs WHERE user_id = %s"),
                ("documents", "DELETE FROM documents WHERE user_id = %s"),
            ]:
                cur = await conn.execute(sql, (user_id,))
                deleted[table] = cur.rowcount
        return DeletionReport(user_id=user_id, deleted=deleted)
