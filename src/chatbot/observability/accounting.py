"""Where usage records go, and the reports over them (ADR 028).

``PostgresUsageStore`` writes one row per operation into ``usage_records`` and sums them by
day, user, model or operation for ``GET /v1/usage`` and ``chatbot usage``. Writing never
fails the operation it accounts for: an error is logged and the row is lost.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from chatbot.observability.usage import UsageRecord

log = logging.getLogger(__name__)

GroupBy = Literal["day", "user", "model", "operation"]


class UsageRow(BaseModel):
    key: str | None  # the day (YYYY-MM-DD), user_id, model or operation; None = no user
    operations: int  # rows (turns, jobs) for day/user/operation; calls for model
    prompt_tokens: int = 0
    completion_tokens: int = 0
    embedding_tokens: int = 0
    cost_usd: float | None = None  # sum of priced calls; None when nothing was priced
    unpriced_calls: int = 0  # calls with no price in models.yaml (cost is a lower bound)


class UsageReport(BaseModel):
    group_by: GroupBy
    since: datetime | None = None
    until: datetime | None = None
    rows: list[UsageRow]
    total: UsageRow


def _total(rows: list[UsageRow]) -> UsageRow:
    costs = [r.cost_usd for r in rows if r.cost_usd is not None]
    return UsageRow(
        key=None,
        operations=sum(r.operations for r in rows),
        prompt_tokens=sum(r.prompt_tokens for r in rows),
        completion_tokens=sum(r.completion_tokens for r in rows),
        embedding_tokens=sum(r.embedding_tokens for r in rows),
        cost_usd=sum(costs) if costs else None,
        unpriced_calls=sum(r.unpriced_calls for r in rows),
    )


def _float(value: Any) -> float | None:
    return float(value) if isinstance(value, Decimal | int | float) else None


class PostgresUsageStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def record(self, rec: UsageRecord) -> None:
        if not rec.calls:
            return
        try:
            async with self.pool.connection() as conn:
                await conn.execute(
                    """INSERT INTO usage_records (operation, trace_id, user_id, conversation_id,
                           prompt_tokens, completion_tokens, embedding_tokens, cost_usd,
                           unpriced_calls, calls)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        rec.operation,
                        rec.trace_id,
                        rec.user_id,
                        rec.conversation_id,
                        rec.usage.prompt_tokens,
                        rec.usage.completion_tokens,
                        rec.usage.embedding_tokens,
                        rec.usage.cost_usd,
                        rec.unpriced_calls,
                        Jsonb([c.model_dump(mode="json") for c in rec.calls]),
                    ),
                )
        except Exception:
            log.exception("could not store usage record", extra={"operation": rec.operation})

    async def report(
        self,
        group_by: GroupBy = "day",
        since: datetime | None = None,
        until: datetime | None = None,
        user_id: str | None = None,
    ) -> UsageReport:
        where, params = ["TRUE"], []
        if since is not None:
            where.append("created_at >= %s")
            params.append(since)
        if until is not None:
            where.append("created_at < %s")
            params.append(until)
        if user_id is not None:
            where.append("user_id = %s")
            params.append(user_id)
        cond = " AND ".join(where)
        if group_by == "model":
            sql = f"""
                SELECT c->>'model' AS key, count(*) AS operations,
                       sum((c->>'prompt_tokens')::int) AS prompt_tokens,
                       sum((c->>'completion_tokens')::int) AS completion_tokens,
                       sum((c->>'embedding_tokens')::int) AS embedding_tokens,
                       sum((c->>'cost_usd')::numeric) AS cost_usd,
                       count(*) FILTER (WHERE c->>'cost_usd' IS NULL) AS unpriced_calls
                FROM usage_records, jsonb_array_elements(calls) AS c
                WHERE {cond} GROUP BY 1 ORDER BY 1"""
        else:
            key = {
                "day": "to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD')",
                "user": "user_id",
                "operation": "operation",
            }[group_by]
            sql = f"""
                SELECT {key} AS key, count(*) AS operations,
                       sum(prompt_tokens) AS prompt_tokens,
                       sum(completion_tokens) AS completion_tokens,
                       sum(embedding_tokens) AS embedding_tokens,
                       sum(cost_usd) AS cost_usd, sum(unpriced_calls) AS unpriced_calls
                FROM usage_records WHERE {cond} GROUP BY 1 ORDER BY 1 NULLS LAST"""
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            rows = [
                UsageRow(
                    key=r["key"],
                    operations=r["operations"],
                    prompt_tokens=r["prompt_tokens"] or 0,
                    completion_tokens=r["completion_tokens"] or 0,
                    embedding_tokens=r["embedding_tokens"] or 0,
                    cost_usd=_float(r["cost_usd"]),
                    unpriced_calls=r["unpriced_calls"] or 0,
                )
                for r in await cur.fetchall()
            ]
        return UsageReport(
            group_by=group_by, since=since, until=until, rows=rows, total=_total(rows)
        )


class InMemoryUsageStore:
    """Same contract, in memory: tests and evals."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, rec: UsageRecord) -> None:
        if rec.calls:
            self.records.append(rec.model_copy(update={"created_at": datetime.now(UTC)}))

    async def report(
        self,
        group_by: GroupBy = "day",
        since: datetime | None = None,
        until: datetime | None = None,
        user_id: str | None = None,
    ) -> UsageReport:
        groups: dict[str | None, list[tuple[int, Any]]] = defaultdict(list)
        for rec in self.records:
            at = rec.created_at or datetime.now(UTC)
            before_window = since is not None and at < since
            after_window = until is not None and at >= until
            if before_window or after_window:
                continue
            if user_id is not None and rec.user_id != user_id:
                continue
            if group_by == "model":
                for c in rec.calls:
                    groups[c.model].append((1, c))
                continue
            key = {
                "day": at.strftime("%Y-%m-%d"),
                "user": rec.user_id,
                "operation": rec.operation,
            }[group_by]
            groups[key].append((1, rec))
        rows = []
        for key, items in sorted(groups.items(), key=lambda kv: (kv[0] is None, kv[0] or "")):
            usages = [i.usage if isinstance(i, UsageRecord) else i for _, i in items]
            costs = [u.cost_usd for u in usages if u.cost_usd is not None]
            unpriced = sum(
                i.unpriced_calls if isinstance(i, UsageRecord) else int(i.cost_usd is None)
                for _, i in items
            )
            rows.append(
                UsageRow(
                    key=key,
                    operations=len(items),
                    prompt_tokens=sum(u.prompt_tokens for u in usages),
                    completion_tokens=sum(u.completion_tokens for u in usages),
                    embedding_tokens=sum(u.embedding_tokens for u in usages),
                    cost_usd=sum(costs) if costs else None,
                    unpriced_calls=unpriced,
                )
            )
        return UsageReport(
            group_by=group_by, since=since, until=until, rows=rows, total=_total(rows)
        )

    async def forget_user(self, user_id: str) -> int:
        n = 0
        for i, rec in enumerate(self.records):
            if rec.user_id == user_id:
                self.records[i] = rec.model_copy(update={"user_id": None, "conversation_id": None})
                n += 1
        return n
