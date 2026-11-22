"""Conversation memory in Postgres: turns, rolling summaries and long-term user facts.

Text arrives here already redacted (ADR 015). Summaries and facts are written by
``chatbot.memory.manager`` (ADR 016). ``forget_user`` deletes every row tied to a user
and unlinks the user from usage records.
"""

from __future__ import annotations

import json
import uuid

from psycopg_pool import AsyncConnectionPool

from chatbot.core.errors import ForbiddenError
from chatbot.core.types import (
    ConversationContext,
    DeletionReport,
    MemoryState,
    Turn,
    UserFact,
)
from chatbot.memory.facts import fact_key


class PostgresMemoryStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def load(
        self,
        user_id: str | None,
        conversation_id: str | None,
        history_turns: int,
        facts_limit: int = 0,
    ) -> ConversationContext:
        facts = await self.list_facts(user_id, facts_limit) if user_id and facts_limit else []
        if conversation_id is None:
            return ConversationContext(
                conversation_id=uuid.uuid4().hex, user_id=user_id, facts=facts, is_new=True
            )
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT user_id FROM conversations WHERE id = %s", (conversation_id,)
            )
            conv = await cur.fetchone()
            if conv is None:
                return ConversationContext(
                    conversation_id=conversation_id, user_id=user_id, facts=facts, is_new=True
                )
            if conv["user_id"] is not None and conv["user_id"] != user_id:
                raise ForbiddenError("This conversation belongs to a different user")
            cur = await conn.execute(
                """SELECT id, role, content, created_at, metadata FROM (
                       SELECT * FROM turns WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
                   ) t ORDER BY id""",
                (conversation_id, history_turns),
            )
            rows = await cur.fetchall()
            cur = await conn.execute(
                "SELECT summary FROM conversation_summaries WHERE conversation_id = %s",
                (conversation_id,),
            )
            summary = await cur.fetchone()
        return ConversationContext(
            conversation_id=conversation_id,
            user_id=conv["user_id"],
            history=[Turn(**r) for r in rows],
            summary=summary["summary"] if summary else "",
            facts=facts,
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

    # ------------------------------------------------------------------ summaries

    async def memory_state(self, conversation_id: str) -> MemoryState | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT c.id AS conversation_id, c.user_id,
                          coalesce(s.summary, '') AS summary,
                          coalesce(s.summarized_until, 0) AS summarized_until,
                          coalesce(s.facts_until, 0) AS facts_until
                   FROM conversations c
                   LEFT JOIN conversation_summaries s ON s.conversation_id = c.id
                   WHERE c.id = %s""",
                (conversation_id,),
            )
            row = await cur.fetchone()
        return MemoryState(**row) if row else None

    async def turns_after(self, conversation_id: str, after_id: int) -> list[Turn]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, role, content, created_at, metadata FROM turns
                   WHERE conversation_id = %s AND id > %s ORDER BY id""",
                (conversation_id, after_id),
            )
            rows = await cur.fetchall()
        return [Turn(**r) for r in rows]

    async def save_summary(self, state: MemoryState) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """INSERT INTO conversation_summaries
                       (conversation_id, user_id, summary, summarized_until, facts_until)
                   SELECT %s, %s, %s, %s, %s
                   WHERE EXISTS (SELECT 1 FROM conversations WHERE id = %s)
                   ON CONFLICT (conversation_id) DO UPDATE SET
                       summary = EXCLUDED.summary,
                       summarized_until = EXCLUDED.summarized_until,
                       facts_until = EXCLUDED.facts_until,
                       updated_at = now()""",
                (
                    state.conversation_id,
                    state.user_id,
                    state.summary,
                    state.summarized_until,
                    state.facts_until,
                    state.conversation_id,
                ),
            )

    # ------------------------------------------------------------------ long-term facts

    async def add_facts(
        self, user_id: str, facts: list[str], conversation_id: str | None, max_facts: int
    ) -> int:
        """Insert new facts (a repeated fact only refreshes updated_at), then keep the
        ``max_facts`` most recently seen. Returns how many facts were new."""
        if not facts:
            return 0
        added = 0
        async with self.pool.connection() as conn, conn.transaction():
            for fact in facts:
                cur = await conn.execute(
                    """INSERT INTO user_facts (user_id, fact, fact_key, source_conversation_id)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (user_id, fact_key) DO UPDATE SET updated_at = now()
                       RETURNING (xmax = 0) AS inserted""",
                    (user_id, fact, fact_key(fact), conversation_id),
                )
                row = await cur.fetchone()
                added += int(bool(row and row["inserted"]))
            await conn.execute(
                """DELETE FROM user_facts WHERE user_id = %s AND id NOT IN (
                       SELECT id FROM user_facts WHERE user_id = %s
                       ORDER BY updated_at DESC, id DESC LIMIT %s)""",
                (user_id, user_id, max_facts),
            )
        return added

    async def list_facts(self, user_id: str, limit: int = 100) -> list[UserFact]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """SELECT id, fact, source_conversation_id, created_at, updated_at
                   FROM user_facts WHERE user_id = %s
                   ORDER BY updated_at DESC, id DESC LIMIT %s""",
                (user_id, limit),
            )
            rows = await cur.fetchall()
        return [UserFact(**r) for r in rows]

    async def delete_fact(self, user_id: str, fact_id: int) -> bool:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM user_facts WHERE user_id = %s AND id = %s", (user_id, fact_id)
            )
        return cur.rowcount > 0

    # ------------------------------------------------------------------ deletion

    async def forget_user(self, user_id: str) -> DeletionReport:
        """Delete everything stored for a user and report counts per table."""
        deleted: dict[str, int] = {}
        async with self.pool.connection() as conn, conn.transaction():
            for table, sql in [
                ("user_facts", "DELETE FROM user_facts WHERE user_id = %s"),
                (
                    "conversation_summaries",
                    """DELETE FROM conversation_summaries WHERE user_id = %s
                       OR conversation_id IN (SELECT id FROM conversations WHERE user_id = %s)""",
                ),
                ("turns", "DELETE FROM turns WHERE user_id = %s"),
                ("conversations", "DELETE FROM conversations WHERE user_id = %s"),
                ("jobs", "DELETE FROM jobs WHERE user_id = %s"),
                ("documents", "DELETE FROM documents WHERE user_id = %s"),
                # Accounting keeps the totals but no longer knows whose they were (ADR 028).
                (
                    "usage_records_anonymised",
                    """UPDATE usage_records SET user_id = NULL, conversation_id = NULL
                       WHERE user_id = %s""",
                ),
            ]:
                params = (user_id,) * sql.count("%s")
                cur = await conn.execute(sql, params)
                deleted[table] = cur.rowcount
        return DeletionReport(user_id=user_id, deleted=deleted)
