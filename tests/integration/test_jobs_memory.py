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


async def test_summaries_and_facts_roundtrip_and_forget_user(db):
    memory = PostgresMemoryStore(db)
    ctx = ConversationContext(conversation_id="c1", user_id="u1")
    await memory.save_turns(ctx, [Turn(role="user", content=f"q{i}") for i in range(3)])
    turns = await memory.turns_after("c1", 0)
    assert [t.content for t in turns] == ["q0", "q1", "q2"] and turns[0].id < turns[2].id

    state = await memory.memory_state("c1")
    assert state.summary == "" and state.summarized_until == 0 and state.user_id == "u1"
    state.summary, state.summarized_until, state.facts_until = "S", turns[1].id, turns[2].id
    await memory.save_summary(state)
    assert await memory.memory_state("c1") == state
    assert [t.content for t in await memory.turns_after("c1", turns[1].id)] == ["q2"]
    assert await memory.memory_state("missing") is None
    await memory.save_summary(state.model_copy(update={"conversation_id": "missing"}))
    assert await memory.memory_state("missing") is None  # no orphan summary rows

    added = await memory.add_facts("u1", ["The user uses Plus.", "The user is a teacher."], "c1", 2)
    assert added == 2
    assert await memory.add_facts("u1", ["the user uses plus"], "c1", 2) == 0  # dedup
    assert await memory.add_facts("u1", ["The user lives in Oslo."], "c1", 2) == 1
    facts = await memory.list_facts("u1")
    assert [f.fact for f in facts] == ["The user lives in Oslo.", "The user uses Plus."]
    loaded = await memory.load("u1", "c1", 10, facts_limit=5)
    assert loaded.summary == "S" and len(loaded.facts) == 2
    assert await memory.delete_fact("u2", facts[0].id) is False  # not theirs
    assert await memory.delete_fact("u1", facts[0].id) is True

    other = ConversationContext(conversation_id="c2", user_id="u2")
    await memory.save_turns(other, [Turn(role="user", content="keep me")])
    await memory.add_facts("u2", ["The user uses Free."], "c2", 10)

    report = await memory.forget_user("u1")
    assert report.deleted["user_facts"] == 1
    assert report.deleted["conversation_summaries"] == 1
    assert report.deleted["turns"] == 3 and report.deleted["conversations"] == 1
    async with db.connection() as conn:
        for table in ("user_facts", "conversation_summaries", "turns", "conversations", "jobs"):
            cur = await conn.execute(f"SELECT count(*) AS n FROM {table} WHERE user_id = 'u1'")
            assert (await cur.fetchone())["n"] == 0
    assert len(await memory.list_facts("u2")) == 1
