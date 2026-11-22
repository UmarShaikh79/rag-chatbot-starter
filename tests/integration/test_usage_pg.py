"""usage_records in Postgres (ADR 028): write, report by day/user/model/operation, and
forget_user unlinking a user from their records while keeping the totals."""

from datetime import UTC, datetime, timedelta

import pytest

from chatbot.memory.postgres import PostgresMemoryStore
from chatbot.observability.accounting import PostgresUsageStore
from chatbot.observability.usage import Ledger, UsageCall

pytestmark = pytest.mark.integration


def record(user_id, operation="chat", cost=0.25):
    ledger = Ledger()
    ledger.user_id, ledger.conversation_id = user_id, f"conv-{user_id}"
    ledger.calls = [
        UsageCall(
            kind="chat",
            model="big",
            role="answer",
            purpose="answer",
            prompt_tokens=100,
            completion_tokens=20,
            cost_usd=cost,
        ),
        UsageCall(
            kind="embedding",
            model="emb",
            role="embed",
            purpose="query",
            embedding_tokens=7,
            cost_usd=None,
        ),
    ]
    return ledger.record(operation, "trace-1")


async def test_record_and_report(db):
    store = PostgresUsageStore(db)
    await store.record(record("a"))
    await store.record(record("a"))
    await store.record(record("b", operation="job:memory_refresh"))
    await store.record(record(None).model_copy(update={"calls": []}))  # nothing spent: skipped

    by_user = await store.report("user")
    assert [(r.key, r.operations) for r in by_user.rows] == [("a", 2), ("b", 1)]
    assert by_user.total.prompt_tokens == 300 and by_user.total.embedding_tokens == 21
    assert by_user.total.cost_usd == pytest.approx(0.75)
    assert by_user.total.unpriced_calls == 3

    by_model = {r.key: r for r in (await store.report("model")).rows}
    assert by_model["big"].operations == 3 and by_model["big"].cost_usd == pytest.approx(0.75)
    assert by_model["emb"].cost_usd is None and by_model["emb"].unpriced_calls == 3

    by_op = {r.key: r.operations for r in (await store.report("operation")).rows}
    assert by_op == {"chat": 2, "job:memory_refresh": 1}
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert [r.key for r in (await store.report("day")).rows] == [today]
    later = await store.report("day", since=datetime.now(UTC) + timedelta(hours=1))
    assert later.rows == [] and later.total.cost_usd is None
    assert (await store.report("user", user_id="b")).total.operations == 1


async def test_forget_user_keeps_totals_but_not_the_user(db):
    store = PostgresUsageStore(db)
    await store.record(record("a"))
    await store.record(record("b"))
    report = await PostgresMemoryStore(db).forget_user("a")
    assert report.deleted["usage_records_anonymised"] == 1
    rows = {r.key: r.operations for r in (await store.report("user")).rows}
    assert rows == {"b": 1, None: 1}
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT count(*) AS n FROM usage_records WHERE conversation_id = 'conv-a'"
        )
        assert (await cur.fetchone())["n"] == 0
