"""Postgres job queue: FOR UPDATE SKIP LOCKED, retries with exponential backoff, dedup keys.

A job left 'running' longer than ``lock_timeout_s`` (worker crashed) is claimable again.
"""

from __future__ import annotations

import json
from typing import Any

from psycopg_pool import AsyncConnectionPool

from chatbot.core.types import Job


def _job(row: dict[str, Any]) -> Job:
    return Job(
        id=str(row["id"]),
        kind=row["kind"],
        payload=row["payload"],
        status=row["status"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        last_error=row["last_error"],
        result=row["result"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


class PostgresJobQueue:
    def __init__(
        self, pool: AsyncConnectionPool, lock_timeout_s: int = 600, backoff_base_s: float = 2.0
    ) -> None:
        self.pool = pool
        self.lock_timeout_s = lock_timeout_s
        self.backoff_base_s = backoff_base_s

    async def enqueue(
        self,
        kind: str,
        payload: dict[str, Any],
        dedup_key: str | None = None,
        max_attempts: int = 3,
    ) -> str:
        """Returns the job ID. With a dedup_key, an identical queued/running job is reused."""
        user_id = payload.get("user_id")
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """INSERT INTO jobs (kind, payload, dedup_key, max_attempts, user_id)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (dedup_key) WHERE dedup_key IS NOT NULL
                     AND status IN ('queued', 'running')
                   DO NOTHING RETURNING id""",
                (kind, json.dumps(payload), dedup_key, max_attempts, user_id),
            )
            row = await cur.fetchone()
            if row:
                return str(row["id"])
            cur = await conn.execute(
                """SELECT id FROM jobs WHERE dedup_key = %s AND status IN ('queued', 'running')""",
                (dedup_key,),
            )
            row = await cur.fetchone()
            if row is None:  # finished between the insert and the select; try once more
                return await self.enqueue(kind, payload, dedup_key, max_attempts)
            return str(row["id"])

    async def claim(self, kinds: list[str]) -> Job | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE jobs SET status = 'running', attempts = attempts + 1,
                          locked_at = now(), updated_at = now()
                   WHERE id = (
                       SELECT id FROM jobs
                       WHERE kind = ANY(%s)
                         AND ((status = 'queued' AND run_after <= now())
                              OR (status = 'running'
                                  AND locked_at < now() - make_interval(secs => %s)))
                       ORDER BY run_after, created_at
                       FOR UPDATE SKIP LOCKED
                       LIMIT 1)
                   RETURNING *""",
                (kinds, self.lock_timeout_s),
            )
            row = await cur.fetchone()
        return _job(row) if row else None

    async def complete(self, job_id: str, result: dict[str, Any]) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                """UPDATE jobs SET status = 'succeeded', result = %s, locked_at = NULL,
                          last_error = NULL, updated_at = now() WHERE id = %s""",
                (json.dumps(result), job_id),
            )

    async def fail(self, job_id: str, error: str, retry: bool = True) -> None:
        """Re-queue with backoff, or mark failed once max_attempts is reached (or at once,
        with ``retry=False``)."""
        async with self.pool.connection() as conn:
            await conn.execute(
                """UPDATE jobs SET
                     status = CASE WHEN NOT %s OR attempts >= max_attempts
                                   THEN 'failed' ELSE 'queued' END,
                     run_after = now() + make_interval(secs => %s * power(2, attempts - 1)),
                     last_error = %s, locked_at = NULL, updated_at = now()
                   WHERE id = %s""",
                (retry, self.backoff_base_s, error[:2000], job_id),
            )

    async def get(self, job_id: str) -> Job | None:
        async with self.pool.connection() as conn:
            try:
                cur = await conn.execute("SELECT * FROM jobs WHERE id = %s::uuid", (job_id,))
            except Exception:  # not a UUID
                return None
            row = await cur.fetchone()
        return _job(row) if row else None
