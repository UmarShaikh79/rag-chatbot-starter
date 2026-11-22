"""Milestone 3 retrieval: BM25, hybrid fusion, HyDE, rewriting, rerank, gate and calibration."""

import json
from typing import ClassVar

import httpx
import pytest

from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.core.errors import ChatbotError
from chatbot.core.settings import ConfigError, RerankerProfile, RetrievalSection
from chatbot.core.types import Calibration, ChatRequest, Chunk, ScoredChunk, Turn
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.providers.llm.fake import FakeLLM
from chatbot.providers.rerankers import RerankError, patient_reranking
from chatbot.providers.rerankers.cohere import PATIENT_DELAYS, QUICK_DELAYS, CohereReranker
from chatbot.providers.rerankers.cross_encoder import CrossEncoderReranker
from chatbot.providers.rerankers.lexical import LexicalReranker
from chatbot.retrieval.bm25 import bm25_rank, tokenize
from chatbot.retrieval.hybrid import rrf_fuse
from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore, InMemoryVectorStore
from tests.conftest import offline_settings


def sc(cid: str, score: float, retriever: str = "dense", text: str = "") -> ScoredChunk:
    chunk = Chunk(id=cid, document_id=f"doc-{cid}", position=0, text=text or cid, content_hash="h")
    return ScoredChunk(chunk=chunk, score=score, retriever=retriever, scores={retriever: score})


async def build(llm: FakeLLM | None = None, cheap: FakeLLM | None = None, **overrides):
    settings = offline_settings(**overrides)
    c = container.build(
        settings,
        pool=None,
        answer_llm=llm or FakeLLM(),
        cheap_llm=cheap or FakeLLM(),
        embedder=HashingEmbedder(384),
        store=InMemoryVectorStore(),
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
    )
    await c.pipeline.sync_source(make_source(settings, "samples"))
    return c


# ---------------------------------------------------------------- BM25


def test_bm25_prefers_rare_terms_and_ignores_stopwords():
    docs = ["the clock is wrong", "error E309 clock", "billing and invoices"]
    ranked = bm25_rank("what does E309 mean", docs)
    assert ranked[0][0] == 1 and len(ranked) == 1
    assert bm25_rank("the of and", docs) == []
    assert tokenize("What is E309?") == ["e309"]


async def test_bm25_strategy_finds_exact_codes():
    c = await build(retrieval={"strategy": "bm25"})
    hits, _ = await c.retriever.retrieve("E204", 3, {})
    assert hits[0].chunk.document_id == "samples:sync-troubleshooting.md"
    assert hits[0].retriever == "bm25" and c.retriever.scorer == "bm25|bm25"


async def test_bm25_needs_a_store_with_keyword_search():
    class NoKeywords:
        async def get_index(self, alias):
            return None

    with pytest.raises(ChatbotError, match="keyword_search"):
        container.build(
            offline_settings(retrieval={"strategy": "bm25"}),
            pool=None,
            answer_llm=FakeLLM(),
            cheap_llm=FakeLLM(),
            embedder=HashingEmbedder(384),
            store=NoKeywords(),
            memory=InMemoryMemoryStore(),
            queue=InMemoryJobQueue(),
        )


# ---------------------------------------------------------------- hybrid


def test_rrf_fuses_ranks_and_keeps_component_scores():
    dense = [sc("a", 0.9), sc("b", 0.8)]
    bm25 = [sc("b", 12.0, "bm25"), sc("c", 7.0, "bm25")]
    fused = rrf_fuse([("dense", dense), ("bm25", bm25)], rrf_k=60)
    assert [h.chunk.id for h in fused] == ["b", "a", "c"]  # b is in both rankings
    assert fused[0].scores == {"dense": 0.8, "bm25": 12.0, "rrf": pytest.approx(1 / 62 + 1 / 61)}
    assert fused[0].retriever == "hybrid" and fused[0].score == fused[0].scores["rrf"]
    weighted = rrf_fuse([("dense", dense), ("bm25", bm25)], weights={"dense": 0.0})
    assert weighted[0].chunk.id == "b" and weighted[1].chunk.id == "c"


async def test_hybrid_pipeline_signature_and_gate():
    c = await build(retrieval={"strategy": "hybrid"})
    assert c.retriever.scorer == "dense|hybrid(dense,bm25)"
    hits, _ = await c.retriever.retrieve("What does sync error E309 mean?", 4, {})
    assert hits[0].chunk.document_id == "samples:sync-troubleshooting.md"
    assert "rrf" in hits[0].scores and "dense" in hits[0].scores


def test_settings_reject_inconsistent_retrieval():
    with pytest.raises(ValueError, match="rerank"):
        RetrievalSection(gate="rerank")
    with pytest.raises(ValueError, match="gate bm25"):
        RetrievalSection(strategy="dense", gate="bm25")
    with pytest.raises(ValueError, match="hyde"):
        RetrievalSection(strategy="bm25", hyde={"enabled": True})
    with pytest.raises(ValueError, match="repeat"):
        RetrievalSection(strategy="hybrid", hybrid={"retrievers": ["dense", "dense"]})
    with pytest.raises(ConfigError, match="rerankers"):
        offline_settings(retrieval={"rerank": {"enabled": True, "model": "nope"}})
    assert RetrievalSection(strategy="hybrid").gate_signal() == "dense"
    assert RetrievalSection(strategy="bm25").gate_signal() == "bm25"
    assert RetrievalSection(rerank={"enabled": True}).gate_signal() == "rerank"
    # The gate a failed rerank falls back to.
    assert RetrievalSection(rerank={"enabled": True}).gate_signal(rerank=False) == "dense"
    with_rerank_gate = RetrievalSection(gate="rerank", rerank={"enabled": True})
    assert with_rerank_gate.gate_signal(rerank=False) == "dense"
    bm25 = RetrievalSection(strategy="bm25", gate="bm25", rerank={"enabled": True})
    assert bm25.gate_signal(rerank=False) == "bm25"


# ---------------------------------------------------------------- HyDE and rewriting


async def test_hyde_embeds_question_plus_hypothetical_answer():
    cheap = FakeLLM(script=["Annual plans can be refunded within 30 days of purchase."])
    c = await build(cheap=cheap, retrieval={"hyde": {"enabled": True}})
    assert c.retriever.scorer == "dense|hyde(dense)"
    hits, usage = await c.retriever.retrieve("money back?", 3, {})
    assert hits[0].chunk.document_id == "samples:refund-policy.md"
    assert usage.completion_tokens > 0 and usage.embedding_tokens > 0  # both are counted
    assert "money back?" in cheap.calls[0][-1].content


async def test_query_rewrite_only_runs_on_follow_ups():
    c = await build(retrieval={"query_rewrite": {"enabled": True}})
    rewriter = c.extras["rewriter"]
    same, usage = await rewriter.rewrite("Where are notes stored by default?", [])
    assert same == "Where are notes stored by default?" and usage.prompt_tokens == 0
    history = [
        Turn(role="user", content="How long is the refund window for annual plans?"),
        Turn(role="assistant", content="30 days [1]."),
    ]
    text, _ = await rewriter.rewrite("and monthly?", history)
    assert text.startswith("How long is the refund window") and text.endswith("and monthly?")


async def test_rewritten_query_drives_retrieval_but_not_the_answer_prompt():
    answer = FakeLLM()
    c = await build(llm=answer, retrieval={"query_rewrite": {"enabled": True}})
    c.orchestrator.rewriter.llm = FakeLLM(script=["How long do refunds take to arrive?"])
    first = await c.orchestrator.run(ChatRequest(message="Which payment methods are accepted?"))
    resp = await c.orchestrator.run(
        ChatRequest(message="and how long?", conversation_id=first.conversation_id)
    )
    prompt = answer.calls[-1][-1].content
    assert "5 to 10 business days" in prompt  # sources came from the rewritten query
    assert "Question: and how long?" in prompt  # the model still sees the user's words
    assert resp.conversation_id == first.conversation_id


# ---------------------------------------------------------------- rerank


async def test_lexical_reranker_reorders_and_records_scores():
    hits = [sc("x", 0.9, text="billing invoices"), sc("y", 0.5, text="refund window annual")]
    out, _ = await LexicalReranker().rerank("refund window", hits, 1)
    assert [h.chunk.id for h in out] == ["y"]
    assert out[0].retriever == "rerank" and out[0].scores["dense"] == 0.5
    assert out[0].scores["rerank"] == out[0].score > 0


async def test_rerank_pipeline_reranks_candidates_down_to_top_k():
    c = await build(
        retrieval={
            "strategy": "hybrid",
            "rerank": {"enabled": True, "model": "lexical:overlap", "candidates": 12},
        }
    )
    assert c.retriever.scorer == "rerank|rerank[lexical-v1](hybrid(dense,bm25))"
    hits, _ = await c.retriever.retrieve("How many tags can one note have?", 3, {})
    assert len(hits) == 3 and all(h.retriever == "rerank" for h in hits)
    assert hits[0].chunk.document_id == "samples:getting-started.md"


async def test_cohere_reranker_maps_results_by_index(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.12},
                ]
            },
        )

    monkeypatch.setenv("COHERE_API_KEY", "k")
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    rr = CohereReranker(RerankerProfile(provider="cohere", model="rerank-x"), client)
    out, _ = await rr.rerank("q", [sc("a", 0.9), sc("b", 0.8)], 2)
    assert [h.chunk.id for h in out] == ["b", "a"] and out[0].scores["rerank"] == 0.91
    assert seen["url"] == "https://api.cohere.com/v2/rerank" and seen["auth"] == "Bearer k"
    assert seen["body"]["model"] == "rerank-x" and seen["body"]["documents"] == ["a", "b"]
    assert rr.signature == "rerank[rerank-x]"


async def test_cohere_reranker_needs_a_key(monkeypatch):
    monkeypatch.delenv("COHERE_API_KEY", raising=False)
    rr = CohereReranker(RerankerProfile(provider="cohere", model="m"))
    with pytest.raises(ChatbotError, match="COHERE_API_KEY"):
        await rr.rerank("q", [sc("a", 0.9)], 1)


class Cohere:
    """A Cohere endpoint that answers from a script: status codes, or exceptions to raise."""

    OK: ClassVar[dict] = {
        "results": [{"index": 0, "relevance_score": 0.7}],
        "meta": {"billed_units": {"search_units": 1}},
    }

    def __init__(self, *script, retry_after: str | None = None) -> None:
        self.script = list(script)
        self.calls = 0
        self.slept: list[float] = []
        self.retry_after = retry_after

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        step = self.script.pop(0) if self.script else 200
        if isinstance(step, Exception):
            raise step
        headers = {"retry-after": self.retry_after} if self.retry_after and step != 200 else {}
        body = self.OK if step == 200 else {"message": "nope"}
        return httpx.Response(step, json=body, headers=headers)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def reranker(self) -> CohereReranker:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return CohereReranker(RerankerProfile(provider="cohere", model="m"), client, self.sleep)


async def test_cohere_retries_rate_limits_and_server_errors(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "k")
    api = Cohere(429, 503)
    out, _ = await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert out[0].scores["rerank"] == 0.7
    assert api.calls == 3 and api.slept == list(QUICK_DELAYS)


async def test_cohere_retries_timeouts(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "k")
    api = Cohere(httpx.ReadTimeout("slow"), httpx.ConnectError("refused"))
    out, _ = await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert out and api.calls == 3


async def test_cohere_gives_up_quickly_in_chat_and_waits_when_patient(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "k")
    api = Cohere(*[429] * 20)
    with pytest.raises(RerankError, match="after 3 attempts: HTTP 429"):
        await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert api.calls == 3

    # calibrate / eval: long enough to outlast a trial key's 10 calls per minute.
    api = Cohere(*[429] * 6)
    with patient_reranking():
        out, _ = await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert out and api.calls == 7 and api.slept == list(PATIENT_DELAYS[:6])
    assert sum(PATIENT_DELAYS) >= 120


async def test_cohere_respects_retry_after(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "k")
    api = Cohere(429, retry_after="20")
    with patient_reranking():
        await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert api.slept == [20.0]
    # In a chat turn a long Retry-After is not waited for: the normal short delay is used.
    api = Cohere(429, retry_after="20")
    await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert api.slept == [QUICK_DELAYS[0]]


async def test_cohere_does_not_retry_client_errors(monkeypatch):
    monkeypatch.setenv("COHERE_API_KEY", "k")
    api = Cohere(401)
    with pytest.raises(RerankError, match="HTTP 401"):
        await api.reranker().rerank("q", [sc("a", 0.9)], 1)
    assert api.calls == 1 and api.slept == []


class BrokenReranker:
    signature = "rerank[broken]"

    async def rerank(self, query, hits, top_n):
        raise RerankError("Cohere rerank failed after 3 attempts: HTTP 429")


async def test_failed_rerank_answers_from_retrieval_order_with_its_calibration():
    """A rate limit or outage must not take chat down. The hits keep their retrieval scores,
    so they are gated by the retriever's calibration, not the reranker's."""
    c = await build(retrieval={"rerank": {"enabled": True, "model": "lexical:overlap"}})
    c.retriever.reranker = BrokenReranker()
    question = ChatRequest(message="How long is the refund window for annual plans?")
    resp = await c.orchestrator.run(question)
    assert resp.kind == "answer" and "rerank_unavailable" in resp.warnings
    assert c.retriever.fallback.scorer == "dense|dense"

    index = await c.retriever.index()
    await c.store.save_calibration(
        index, Calibration(scorer="dense|dense", min_score=0.99, min_hits=1)
    )
    assert (await c.orchestrator.run(question)).kind == "clarify"  # dense calibration applied

    # A turn whose rerank works is not marked.
    c.retriever.reranker = LexicalReranker()
    resp = await c.orchestrator.run(question)
    assert "rerank_unavailable" not in resp.warnings


async def test_rerank_failure_fails_the_request_when_fallback_is_off_or_patient():
    c = await build(
        retrieval={"rerank": {"enabled": True, "model": "lexical:overlap", "fallback": False}}
    )
    c.retriever.reranker = BrokenReranker()
    with pytest.raises(RerankError):
        await c.retriever.retrieve("refund window", 3, {})

    c = await build(retrieval={"rerank": {"enabled": True, "model": "lexical:overlap"}})
    c.retriever.reranker = BrokenReranker()
    with patient_reranking(), pytest.raises(RerankError):  # calibrate / eval never fall back
        await c.retriever.retrieve("refund window", 3, {})


async def test_cross_encoder_runs_the_model_off_the_event_loop():
    class Model:
        def predict(self, pairs):
            return [len(text) / 100 for _, text in pairs]

    rr = CrossEncoderReranker(RerankerProfile(provider="cross_encoder", model="m"), Model())
    out, _ = await rr.rerank("q", [sc("short", 0.9), sc("much longer text", 0.1)], 2)
    assert out[0].chunk.id == "much longer text"


# ---------------------------------------------------------------- calibration in the flow


async def test_calibration_for_this_pipeline_overrides_config():
    c = await build()
    question = ChatRequest(message="How long is the refund window for annual plans?")
    assert (await c.orchestrator.run(question)).kind == "answer"
    index = await c.retriever.index()
    # A calibration for another pipeline does not apply...
    await c.store.save_calibration(
        index, Calibration(scorer="bm25|bm25", min_score=99.0, min_hits=1)
    )
    assert (await c.orchestrator.run(question)).kind == "answer"
    # ...one for this pipeline does.
    await c.store.save_calibration(
        index, Calibration(scorer=c.retriever.scorer, min_score=0.99, min_hits=1)
    )
    assert (await c.orchestrator.run(question)).kind == "clarify"


# ---------------------------------------------------------------- groundedness


async def test_ungrounded_answer_becomes_a_clarifying_question():
    llm = FakeLLM(script=["Refunds are paid in gold bars delivered by drone within an hour [1]."])
    c = await build(
        llm=llm,
        guardrails={"output": {"groundedness_check": True}, "groundedness": {"judge": "lexical"}},
    )
    resp = await c.orchestrator.run(ChatRequest(message="How long do refunds take to arrive?"))
    assert resp.kind == "clarify" and resp.citations == []


async def test_grounded_answer_passes_and_warn_mode_only_warns():
    c = await build(
        guardrails={"output": {"groundedness_check": True}, "groundedness": {"judge": "llm"}}
    )
    ok = await c.orchestrator.run(ChatRequest(message="How long do refunds take to arrive?"))
    assert ok.kind == "answer"  # the fake judge (word overlap) finds the quote supported

    llm = FakeLLM(script=["Refunds are paid in gold bars delivered by drone within an hour [1]."])
    c = await build(
        llm=llm,
        guardrails={
            "output": {"groundedness_check": True},
            "groundedness": {"judge": "lexical", "on_fail": "warn"},
        },
    )
    resp = await c.orchestrator.run(ChatRequest(message="How long do refunds take to arrive?"))
    assert resp.kind == "answer" and "groundedness:low" in resp.warnings


async def test_llm_judge_failure_follows_on_error():
    from chatbot.core.prompts import PromptRegistry
    from chatbot.guardrails.groundedness import LlmJudge
    from tests.conftest import ROOT

    judge = LlmJudge(
        FakeLLM(script=["not json"]), PromptRegistry(ROOT / "prompts"), "judge_groundedness/v1"
    )
    result = await judge.judge("An answer.", [sc("a", 0.9, text="An answer.")])
    assert result.error and result.score == 0.0


def test_rerank_cost_uses_whichever_unit_was_billed():
    from chatbot.providers.rerankers import rerank_cost

    assert rerank_cost({"search_units": 1}, 2.0, {}) == 0.002  # $2 per 1,000 searches
    assert rerank_cost({"input_tokens": 1_000_000}, None, {"input": 0.05}) == 0.05
    assert rerank_cost({"input_tokens": 1298}, None, {}) is None  # no price set: unpriced
    assert rerank_cost({}, 2.0, {"input": 0.05}) is None


async def test_priced_cohere_call_lands_in_usage_and_the_ledger(monkeypatch):
    from chatbot.observability.usage import metering

    monkeypatch.setenv("COHERE_API_KEY", "k")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "results": [{"index": 0, "relevance_score": 0.9}],
                "meta": {"billed_units": {"input_tokens": 1298}},
            },
        )

    profile = RerankerProfile(provider="cohere", model="rerank-x", price_per_mtok={"input": 2.0})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with metering() as ledger:
        _, usage = await CohereReranker(profile, client).rerank("q", [sc("a", 0.9)], 1)
    assert usage.cost_usd == pytest.approx(1298 * 2.0 / 1_000_000)
    [call] = ledger.calls
    assert (call.kind, call.model, call.role) == ("rerank", "rerank-x", "rerank")
    assert call.cost_usd == usage.cost_usd
    assert ledger.total().cost_usd == usage.cost_usd


def test_priced_mixed_keeps_a_cost_already_priced():
    """Retrieval usage = embedding tokens (priced by the orchestrator) + a rerank call that
    priced itself. Before, the rerank cost was overwritten."""
    from chatbot.core.types import Usage
    from chatbot.observability.usage import priced_mixed

    usage = Usage(embedding_tokens=10_000, cost_usd=0.003)
    out = priced_mixed(usage, {}, {"input": 0.02})
    assert out.cost_usd == pytest.approx(10_000 * 0.02 / 1_000_000 + 0.003)
    assert priced_mixed(Usage(cost_usd=0.003), {}, {}).cost_usd == 0.003  # rerank only
    assert priced_mixed(Usage(embedding_tokens=5, cost_usd=0.003), {}, {}).cost_usd is None
    assert priced_mixed(Usage(embedding_tokens=5), {}, {"input": 0.02}).cost_usd == pytest.approx(
        5 * 0.02 / 1_000_000
    )
