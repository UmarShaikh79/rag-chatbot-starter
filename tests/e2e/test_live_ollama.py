"""Opt-in: the real Ollama path. Set OLLAMA_TEST_URL (e.g. http://localhost:11434) after
pulling the models of config/profiles/local-ollama.yaml (`make ollama-pull`).

Ingests the sample corpus with nomic-embed-text and asks one question with the local chat
model. Slow on CPU-only machines (a minute or more).
"""

import os

import pytest

from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.core.settings import load_settings
from chatbot.core.types import ChatRequest
from tests.conftest import ROOT

pytestmark = [
    pytest.mark.ollama,
    pytest.mark.skipif(not os.environ.get("OLLAMA_TEST_URL"), reason="set OLLAMA_TEST_URL"),
]


async def test_local_models_answer_with_citations(db, monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", os.environ["OLLAMA_TEST_URL"])
    settings = load_settings(root=ROOT, profile="local-ollama", environ={})
    c = container.build(settings, db)
    report = await c.pipeline.sync_source(make_source(settings, "samples"))
    assert len(report.indexed) == 8 and not report.failed
    info = await c.store.get_index("docs_local")
    assert info.embedding_model == "nomic-embed-text" and info.dims == 768

    hits, _ = await c.retriever.retrieve("How long is the refund window for annual plans?", 3, {})
    assert hits[0].chunk.document_id == "samples:refund-policy.md"
    resp = await c.orchestrator.run(
        ChatRequest(message="How long is the refund window for annual plans?")
    )
    assert resp.kind in ("answer", "clarify")  # a small local model may still abstain
    if resp.kind == "answer":
        assert resp.citations and "<think>" not in resp.answer
