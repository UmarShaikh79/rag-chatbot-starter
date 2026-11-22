# ADR 019: Hybrid retrieval with rank fusion and a gate score

Status: Accepted (milestone 3)

## Decision

Retrieval is a small pipeline built from config: one retriever (`dense`, `bm25`, or your own),
or `hybrid`, which runs several and fuses their rankings with reciprocal rank fusion (RRF,
`k = 60`, optional per-retriever weights). HyDE wraps the retrievers named in
`retrieval.hyde.apply_to`; a reranker, when enabled, re-scores `rerank.candidates` hits and
keeps `top_k`. Query rewriting for follow-ups runs in the orchestrator before retrieval.

Every hit keeps the score each step gave it (`ScoredChunk.scores`: `dense`, `bm25`, `rrf`,
`rerank`). Weak-retrieval thresholds apply to one named **gate score** (`retrieval.gate`,
`auto` = rerank if enabled, else dense, else bm25), never to the fused RRF score.

## Alternatives considered

- Score fusion (min-max or z-score normalisation, then a weighted sum): needs per-query
  normalisation, which throws away the absolute relevance a threshold needs anyway.
- Thresholding the RRF score: it depends only on ranks, so a nonsense question still gets a
  top hit of `1/61`; weak retrieval could never be detected.
- LangChain's `EnsembleRetriever` and similar: a framework dependency for 30 lines of code.

## Why

RRF is robust across score scales and needs no tuning to beat either retriever alone on
mixed query types (exact codes and product names vs paraphrases). Keeping component scores
lets the "ask, never invent" rule (ADR 006) keep working after fusion.

## Consequences

- A hybrid hit found only by BM25 has no dense score. It cannot count towards `min_hits`, but
  it may reach the model once retrieval is strong (`Assessment.usable`).
- Each extra step costs latency: hybrid runs its retrievers concurrently; HyDE and rewriting
  add one cheap-model call each; rerank adds a model or API call.
- Any change to the pipeline changes its calibration key (ADR 021).
