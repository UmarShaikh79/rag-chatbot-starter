import asyncio

import pytest

from chatbot.core.types import ConversationContext, Turn
from chatbot.ingestion.jobs.queue import PostgresJobQueue
from chatbot.memory.postgres import PostgresMemoryStore

pytestmark = pytest.mark.integration


async def test_concurrent_claims_never_share_a_job(db):
    queue = PostgresJobQueue(db)
    ids = {await queue.enqueue("k", {"n": i}) for i in range(20)}
    claimed = await asyncio.gather(*(queue.claim(["k"]) for _ in range(25)))
    got = [j.id for j in claimed if j is not None]
    assert len(got) == len(set(got)) == 20
    assert set(got) == ids


async def test_dedup_key_only_while_pending(db):
    queue = PostgresJobQueue(db)
    a = await queue.enqueue("sync", {}, dedup_key="sync:s")
    assert await queue.enqueue("sync", {}, dedup_key="sync:s") == a
    job = await queue.claim(["sync"])
    await queue.complete(job.id, {"ok": True})
    assert await queue.enqueue("sync", {}, dedup_key="sync:s") != a


async def test_retry_with_backoff_then_fail(db):
    queue = PostgresJobQueue(db, backoff_base_s=0)
    job_id = await queue.enqueue("k", {}, max_attempts=2)
    for _ in range(2):
        job = await queue.claim(["k"])
        await queue.fail(job.id, "boom")
    job = await queue.get(job_id)
    assert job.status == "failed" and job.attempts == 2 and job.last_error == "boom"
    assert await queue.claim(["k"]) is None


async def test_abandoned_job_is_reclaimed(db):
    queue = PostgresJobQueue(db, lock_timeout_s=60)
    job_id = await queue.enqueue("k", {})
    await queue.claim(["k"])
    async with db.connection() as conn:
        await conn.execute("UPDATE jobs SET locked_at = now() - interval '2 minutes'")
    again = await queue.claim(["k"])
    assert again.id == job_id and again.attempts == 2


async def test_unknown_job_id_returns_none(db):
    assert await PostgresJobQueue(db).get("not-a-uuid") is None


async def test_memory_history_and_forget_user(db):
    memory = PostgresMemoryStore(db)
    queue = PostgresJobQueue(db)
    ctx = await memory.load("u1", None, 10)
    assert ctx.is_new
    await memory.save_turns(
        ctx, [Turn(role="user", content="hi [EMAIL]"), Turn(role="assistant", content="hello")]
    )
    loaded = await memory.load("u1", ctx.conversation_id, 10)
    assert [t.content for t in loaded.history] == ["hi [EMAIL]", "hello"]
    await queue.enqueue("ingest_documents", {"user_id": "u1", "documents": []})
    other = ConversationContext(conversation_id="c2", user_id="u2")
    await memory.save_turns(other, [Turn(role="user", content="keep me")])

    report = await memory.forget_user("u1")
    assert report.deleted["turns"] == 2 and report.deleted["conversations"] == 1
    assert report.deleted["jobs"] == 1
    async with db.connection() as conn:
        for table in ("turns", "conversations", "jobs"):
            cur = await conn.execute(f"SELECT count(*) AS n FROM {table} WHERE user_id = 'u1'")
            assert (await cur.fetchone())["n"] == 0
        cur = await conn.execute("SELECT count(*) AS n FROM turns WHERE user_id = 'u2'")
        assert (await cur.fetchone())["n"] == 1


async def test_history_window_is_most_recent(db):
    memory = PostgresMemoryStore(db)
    ctx = ConversationContext(conversation_id="c1", user_id=None)
    for i in range(6):
        await memory.save_turns(ctx, [Turn(role="user", content=f"m{i}")])
    loaded = await memory.load(None, "c1", 3)
    assert [t.content for t in loaded.history] == ["m3", "m4", "m5"]
