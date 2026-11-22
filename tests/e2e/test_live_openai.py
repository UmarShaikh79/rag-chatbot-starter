"""Opt-in: the real OpenAI path. Run with OPENAI_API_KEY set and RUN_LIVE_TESTS=1.

Uses the default profile (OpenAI chat + text-embedding-3-small) against Postgres, and checks
that the model answers with citations and abstains on questions the corpus cannot answer.
"""

import json
import os

import pytest

from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.core.settings import load_settings
from chatbot.core.types import ChatRequest
from tests.conftest import ROOT

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not (os.environ.get("OPENAI_API_KEY") and os.environ.get("RUN_LIVE_TESTS") == "1"),
        reason="set OPENAI_API_KEY and RUN_LIVE_TESTS=1",
    ),
]


async def test_live_answer_and_abstain(db):
    settings = load_settings(
        root=ROOT, profile="dev", overrides={"vector_store": {"index": "live"}}
    )
    c = container.build(settings, db)
    await c.pipeline.sync_source(make_source(settings, "samples"))
    golden = [
        json.loads(x) for x in (ROOT / "evals/datasets/golden.jsonl").read_text().splitlines()
    ]

    answer = await c.orchestrator.run(ChatRequest(message=golden[0]["question"]))
    assert answer.kind == "answer" and answer.citations
    assert "30" in answer.answer

    for q in [g for g in golden if not g["answerable"]]:
        resp = await c.orchestrator.run(ChatRequest(message=q["question"]))
        assert resp.kind == "clarify", (q["question"], resp.answer)
