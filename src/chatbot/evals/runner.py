"""Runs the golden dataset through the configured pipeline and scores it.

Two passes:

* retrieval: every question through the retrieval pipeline (rerank, HyDE included),
  scored hit@k, recall@top_k and MRR against ``expected_documents``. It also records each
  hit's gate score, which is what ``chatbot calibrate`` sweeps.
* answers (skipped with ``--retrieval-only``): every question through the orchestrator,
  scored on answer rate, required phrases, citation precision, faithfulness (the
  groundedness judge) and, for unanswerable questions, abstention.

The orchestrator runs with in-memory conversation memory and job queue, so evals never
write conversations or memory jobs to your database.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from pydantic import BaseModel

from chatbot.core.errors import error_text
from chatbot.core.types import ChatRequest, ScoredChunk, Usage
from chatbot.evals import metrics as m
from chatbot.evals.dataset import GoldenItem


class RetrievedHit(BaseModel):
    document_id: str
    chunk_id: str
    score: float
    gate_score: float | None


class RetrievalRow(BaseModel):
    id: str
    answerable: bool
    expected: list[str]
    hits: list[RetrievedHit]
    tags: list[str] = []


class AnswerRow(BaseModel):
    id: str
    answerable: bool
    kind: str
    answer: str
    cited: list[str]
    must_include: float | None = None
    citation_precision: float | None = None
    faithfulness: float | None = None
    unsupported: list[str] = []
    latency_ms: float
    usage: Usage = Usage()
    error: str | None = None


class EvalReport(BaseModel):
    summary: dict[str, Any]
    retrieval: list[RetrievalRow]
    answers: list[AnswerRow] = []
    meta: dict[str, Any] = {}


async def _bounded(n: int, coros: list[Any]) -> list[Any]:
    sem = asyncio.Semaphore(n)

    async def run(c: Any) -> Any:
        async with sem:
            return await c

    return await asyncio.gather(*(run(c) for c in coros))


async def run_retrieval(
    retriever: Any,
    items: list[GoldenItem],
    top_k: int,
    concurrency: int = 4,
    texts: dict[str, str] | None = None,
) -> list[RetrievalRow]:
    """``texts`` (optional) collects chunk text by ID, for judging answers later."""
    gate = getattr(retriever, "gate", "dense")

    async def one(item: GoldenItem) -> RetrievalRow:
        hits: list[ScoredChunk]
        hits, _ = await retriever.retrieve(item.question, top_k, {})
        if texts is not None:
            texts.update({h.chunk.id: h.chunk.text for h in hits})
        return RetrievalRow(
            id=item.id,
            answerable=item.answerable,
            expected=item.expected_documents,
            tags=item.tags,
            hits=[
                RetrievedHit(
                    document_id=h.chunk.document_id,
                    chunk_id=h.chunk.id,
                    score=round(h.score, 6),
                    gate_score=None if h.gate_score(gate) is None else round(h.gate_score(gate), 6),  # type: ignore[arg-type]
                )
                for h in hits
            ],
        )

    return await _bounded(concurrency, [one(i) for i in items])


async def run_answers(
    orchestrator: Any,
    judge: Any,
    items: list[GoldenItem],
    concurrency: int = 4,
    texts: dict[str, str] | None = None,
) -> list[AnswerRow]:
    """``judge`` scores faithfulness (None skips it); ``texts`` gives it full chunk text
    where a citation only carries a snippet."""
    texts = texts or {}

    async def one(item: GoldenItem) -> AnswerRow:
        start = time.perf_counter()
        try:
            resp = await orchestrator.run(ChatRequest(message=item.question))
        except Exception as exc:  # one broken question must not hide the rest
            return AnswerRow(
                id=item.id,
                answerable=item.answerable,
                kind="error",
                answer="",
                cited=[],
                latency_ms=round((time.perf_counter() - start) * 1000, 1),
                error=error_text(exc),
            )
        latency = round((time.perf_counter() - start) * 1000, 1)
        row = AnswerRow(
            id=item.id,
            answerable=item.answerable,
            kind=resp.kind,
            answer=resp.answer,
            cited=[c.document_id for c in resp.citations],
            latency_ms=latency,
            usage=resp.usage,
        )
        if item.answerable and resp.kind == "answer":
            row.must_include = m.includes_all(resp.answer, item.must_include)
            row.citation_precision = m.citation_precision(row.cited, item.expected_documents)
        if judge is not None and resp.kind == "answer" and resp.citations:
            sources = [
                ScoredChunk.model_validate(
                    {
                        "chunk": {
                            "id": c.chunk_id,
                            "document_id": c.document_id,
                            "position": 0,
                            "text": texts.get(c.chunk_id, c.snippet),
                            "content_hash": "",
                        },
                        "score": c.score,
                        "retriever": "eval",
                    }
                )
                for c in resp.citations
            ]
            judged = await judge.judge(resp.answer, sources)
            row.faithfulness = None if judged.error else round(judged.score, 4)
            row.unsupported = judged.unsupported
        return row

    return await _bounded(concurrency, [one(i) for i in items])


def summarize(
    retrieval: list[RetrievalRow],
    answers: list[AnswerRow],
    top_k: int,
    hit_at: list[int],
) -> dict[str, Any]:
    answerable = [r for r in retrieval if r.answerable]
    docs = {r.id: [h.document_id for h in r.hits] for r in answerable}
    out: dict[str, Any] = {
        "count.questions": len(retrieval),
        "count.answerable": len(answerable),
        "count.unanswerable": len(retrieval) - len(answerable),
    }
    for k in sorted(set(hit_at)):
        out[f"retrieval.hit@{k}"] = m.mean(
            [m.hit_at(docs[r.id], r.expected, k) for r in answerable]
        )
    out["retrieval.hit@top_k"] = m.mean(
        [m.hit_at(docs[r.id], r.expected, top_k) for r in answerable]
    )
    out["retrieval.recall@top_k"] = m.mean(
        [m.recall_at(docs[r.id], r.expected, top_k) for r in answerable]
    )
    out["retrieval.mrr"] = m.mean([m.reciprocal_rank(docs[r.id], r.expected) for r in answerable])
    if not answers:
        return out
    ans = [a for a in answers if a.answerable]
    unans = [a for a in answers if not a.answerable]
    answered = [a for a in ans if a.kind == "answer"]
    out["answer.answer_rate"] = m.mean([float(a.kind == "answer") for a in ans])
    out["answer.must_include_rate"] = m.mean(
        [a.must_include if a.must_include is not None else 0.0 for a in ans]
    )
    out["answer.citation_precision"] = m.mean(
        [a.citation_precision for a in answered if a.citation_precision is not None]
    )
    faith = [a.faithfulness for a in answers if a.faithfulness is not None]
    out["answer.faithfulness"] = m.mean(faith)
    out["abstention.unanswerable_rate"] = m.mean([float(a.kind != "answer") for a in unans])
    out["errors"] = sum(a.error is not None for a in answers)
    latencies = [a.latency_ms for a in answers]
    out["latency.p50_ms"] = m.percentile(latencies, 0.5)
    out["latency.p95_ms"] = m.percentile(latencies, 0.95)
    total = Usage()
    for a in answers:
        total = total.add(a.usage)
    out["usage.tokens"] = total.prompt_tokens + total.completion_tokens + total.embedding_tokens
    out["usage.cost_usd"] = None if total.cost_usd is None else round(total.cost_usd, 6)
    return out
