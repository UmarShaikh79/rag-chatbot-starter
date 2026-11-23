# Retrieval strategies

How the passages for a question are found: by meaning (**dense**), by exact words (**BM25**),
or both fused (**hybrid**), optionally helped by a rewritten follow-up (**query rewrite**) or a
hypothetical answer (**HyDE**). One setting, `retrieval.strategy`, picks the strategy.

Back to the [docs home](../README.md) · See also: [architecture, step 4](../architecture.md#4-one-chat-request-from-start-to-finish)

## At a glance

| | |
| --- | --- |
| **What it does** | Returns the `top_k` (8) passages most likely to answer the question, best first, each carrying every score it picked up on the way. |
| **Turned on by** | Always on; `retrieval.strategy` (`dense`, `bm25`, `hybrid`), `retrieval.hyde`, `retrieval.query_rewrite` in [`config/app.yaml`](../../config/app.yaml) |
| **Main files** | [`retrieval/pipeline.py`](../../src/chatbot/retrieval/pipeline.py) (`build_pipeline`, `RetrievalPipeline`), [`retrieval/dense.py`](../../src/chatbot/retrieval/dense.py) (`DenseRetriever`), [`retrieval/bm25.py`](../../src/chatbot/retrieval/bm25.py) (`Bm25Retriever`, `bm25_rank`), [`retrieval/hybrid.py`](../../src/chatbot/retrieval/hybrid.py) (`HybridRetriever`, `rrf_fuse`), [`retrieval/hyde.py`](../../src/chatbot/retrieval/hyde.py) (`HydeRetriever`), [`retrieval/rewrite.py`](../../src/chatbot/retrieval/rewrite.py) (`QueryRewriter`), the `Retriever` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | [Embeddings](embeddings.md) (dense, HyDE), a [store](vector-stores.md) with `keyword_search` (BM25, hybrid: pgvector), the cheap model (HyDE, query rewrite) |
| **Used by** | [`SimpleOrchestrator.retrieve`](../../src/chatbot/orchestration/simple.py); the hits go to [weak-retrieval detection](weak-retrieval-and-calibration.md), then the answer |
| **Decisions** | ADR [019](../adr/019-hybrid-retrieval-with-rank-fusion-and-a-gate-score.md) (hybrid with rank fusion and a gate score), ADR [020](../adr/020-bm25-in-postgres.md) (BM25 in Postgres), ADR [011](../adr/011-retrieval-thresholds-are-calibrated-never-constants.md) (calibrated thresholds) |

## What it does

Each strategy is good at something different:

| Strategy | Finds | Misses | Cost per question |
| --- | --- | --- | --- |
| `dense` (default) | Paraphrases: "get my money back" → "Refund policy" | Exact codes and rare names it never learned: `E309`, `SKU-4471` | One embedding call |
| `bm25` | Exact words, codes, IDs, product names | Different words for the same idea | One SQL query, no model |
| `hybrid` | Both | Little; it costs both | Both, in parallel |

Two optional helpers run before the search:

- **Query rewrite** turns a follow-up into a standalone question: after a question about
  storage, "And in the US?" becomes "Where are notes stored for US customers?". Only the
  search sees the rewrite; the answer still uses the user's words and the history.
- **[HyDE](../glossary.md#hyde)** asks the cheap model for a short passage that *would* answer
  the question, and searches with the question plus that passage. Its embedding sits closer
  to real answers than a short or vague question does. The passage is never shown or used as
  a fact.

### BM25 (keyword search)

[BM25](../glossary.md#bm25) scores a passage by the question's words it contains: rare words
count more (`E309` in 1 of 1,000 passages beats `account` in 900), repeats help with
diminishing returns, and long passages are slightly penalised. The formula, with every term
explained, is in the docstring of `bm25_rank` in
[`retrieval/bm25.py`](../../src/chatbot/retrieval/bm25.py). In production, pgvector computes
the same formula in SQL (`PgVectorStore.keyword_search`), over a `tsvector` column that
Postgres fills when a passage is stored. BM25 scores are unbounded (2.1, 7.4, …) and only
comparable within one index.

### Hybrid and RRF

Dense scores are cosines (0 to 1); BM25 scores are unbounded. Adding them would be
meaningless, so hybrid uses [reciprocal rank fusion (RRF)](../glossary.md#rrf), which looks
only at **ranks**: each passage gets `weight / (rrf_k + rank)` from every list it appears in.
A passage ranked 1st by dense and 3rd by BM25 scores `1/61 + 1/63 ≈ 0.0323`; one ranked 1st by
dense only scores `1/61 ≈ 0.0164`. Agreement wins.

Because a fused score says nothing about relevance on its own, it is never used to decide
"answer or ask". Each passage keeps its original scores (`scores["dense"]`, `scores["bm25"]`),
and the [gate](../glossary.md#gate-score) reads one of those.

## How it works

At startup, [`build_pipeline(settings, embedder, store, alias, llms, prompts)`](../../src/chatbot/retrieval/pipeline.py)
reads `retrieval.*` and nests the parts:

1. `one(name)` builds each named retriever from the registry
   (`registry.create("retriever", name, embedder, store, alias, settings=settings)`), and wraps
   it in `HydeRetriever` if HyDE is on and the name is in `hyde.apply_to`.
2. `strategy: hybrid` wraps the list in `HybridRetriever(retrievers, candidates, rrf_k, weights)`.
3. Everything goes into a `RetrievalPipeline`, which adds the optional
   [reranker](reranking.md) and knows which score is the gate.

Per question, in [`SimpleOrchestrator.retrieve`](../../src/chatbot/orchestration/simple.py):

1. **Rewrite** (optional): `QueryRewriter.rewrite(question, history)`. Skipped on a first
   message (`only_with_history`), when the call fails, or when the rewrite is longer than
   `max_chars`; then the question is used as is.
2. **Retrieve:** `RetrievalPipeline.retrieve(query, top_k, filters)` calls the retriever:
   - `DenseRetriever.retrieve`: check the index matches the embedder, `embed_query`, then
     `store.search`.
   - `Bm25Retriever.retrieve`: `store.keyword_search`.
   - `HybridRetriever.retrieve`: run every retriever at once (`asyncio.gather`) for
     `candidates` (20) hits each, then `rrf_fuse` and keep `k`.
   - `HydeRetriever.retrieve`: `hypothetical(question)` with the `hyde/v1` prompt, then the
     inner retriever with "question + passage" (or the question alone if generation failed).
3. **Guard the context:** passages that look like injected instructions are dropped
   ([guardrails](../architecture.md#4-one-chat-request-from-start-to-finish)).
4. **Assess:** [`assess`](../../src/chatbot/retrieval/weak.py) decides whether retrieval is
   strong enough to answer ([weak retrieval and calibration](weak-retrieval-and-calibration.md)).

The pipeline's **scorer** names the gate score and every step that shaped it, for example
`dense|hybrid(hyde(dense),bm25)`. Calibrated thresholds are stored under that name, so changing
any step needs a new calibration.

## Inputs and outputs

**In:** the question (or its rewrite), `k` = `retrieval.top_k`, and the request's `filters`.

**Out:** `(hits, usage)`: up to `k` `ScoredChunk`s, best first, and the tokens spent
(embedding, plus the cheap model for HyDE). The top three hits with `strategy: hybrid` on the
offline profile, for "How long is the refund window for annual plans?" (real output):

| `document_id` | `score` (RRF) | `retriever` | `scores` |
| --- | --- | --- | --- |
| `samples:refund-policy.md` | 0.0328 | `hybrid` | `{"dense": 0.2894, "bm25": 9.2409, "rrf": 0.0328}` |
| `samples:refund-policy.md` | 0.0323 | `hybrid` | `{"dense": 0.169, "bm25": 6.1886, "rrf": 0.0323}` |
| `samples:refund-policy.md` | 0.0313 | `hybrid` | `{"dense": 0.1127, "bm25": 4.6512, "rrf": 0.0313}` |

The RRF score orders the list; the gate reads `scores["dense"]` (or `bm25` with
`retrieval.gate: bm25`). A passage found by only one retriever has only that retriever's
score, so it can't count towards `min_hits` under the other's gate.

## Turn it on, change it, turn it off

Pick a strategy with one setting ([configuration: `retrieval`](../configuration.md#retrieval)):

```bash
CHATBOT__RETRIEVAL__STRATEGY=hybrid          # dense | bm25 | hybrid | a retriever you register
```

| Setting | Default | Notes |
| --- | --- | --- |
| `retrieval.top_k` | 8 | Passages returned; the answer uses the first `answer.max_context_chunks` (6). |
| `retrieval.bm25.k1` / `b` | 1.2 / 0.75 | Repeat saturation / length penalty. |
| `retrieval.hybrid.retrievers` | `[dense, bm25]` | At least two, no repeats. |
| `retrieval.hybrid.candidates` | 20 | Hits each retriever returns before fusion. |
| `retrieval.hybrid.rrf_k` | 60 | Larger flattens the difference between ranks. |
| `retrieval.hybrid.weights` | `{}` | e.g. `{dense: 1.0, bm25: 0.5}`; unnamed retrievers weigh 1.0. |
| `retrieval.query_rewrite.enabled` | false | One cheap-model call on follow-ups (`rewrite_query/v1`). |
| `retrieval.hyde.enabled` | false | One cheap-model call per question (`hyde/v1`). |
| `retrieval.hyde.apply_to` | `[dense]` | Only these retrievers get the hypothetical text. |
| `retrieval.hyde.include_query` | true | Search with "question + passage", not the passage alone. |

**After any change, recalibrate and measure:**

```bash
make calibrate-apply    # thresholds for the new scorer, stored with the index
make eval               # did retrieval.mrr and abstention improve?
```

Measured on the offline profile (hashing embedder, 35 questions, 2026-11-23), calibrating each
gate on the same index:

| Scorer | `min_score` / `min_hits` | Answer recall | Abstention |
| --- | --- | --- | --- |
| `dense\|dense` | 0.085 / 2 | 0.92 | 0.20 |
| `dense\|hybrid(dense,bm25)` | 0.085 / 2 | 0.92 | 0.20 |
| `bm25\|hybrid(dense,bm25)` (`retrieval.gate: bm25`) | 2.11 / 2 | 0.96 | 0.70 |

The hashing embedder can't tell an off-topic question that shares words with the documents
from a real one; real embedding models separate them far better. Measure on your own data.

**Turning things off:** set `strategy: dense` and the helpers' `enabled: false` (the defaults).
Nothing to uninstall: BM25 is plain SQL, HyDE and rewrite only use the cheap model you already
have.

## Extend it

A retriever is anything with `name` and `retrieve(query, k, filters) -> (hits, usage)` (the
`Retriever` Protocol). Add `index()` so the pipeline can check the index, and a `signature`
for the calibration key. Register a factory:

```python
@registry.register("retriever", "title_boost")
def make_title_boost(embedder, store, alias, **kwargs):
    return TitleBoostRetriever(DenseRetriever(embedder, store, alias))
```

Then `retrieval.strategy: title_boost`, or list it in `retrieval.hybrid.retrievers`. The
[tutorial](../tutorial.md) builds this retriever end to end, with tests and calibration.

Ideas that fit the same shape: MMR (diversify near-duplicate passages), a time-decay boost for
recent documents, a metadata boost, a SPLADE or other sparse retriever. Put your score in
`hit.scores[<your name>]` as well as `hit.score`; set `retrieval.gate` to it if it should
decide "answer or ask". To use another keyword engine, override `keyword_search` on the store
([vector stores](vector-stores.md#extend-it)).

## Test it

```bash
uv run pytest tests/unit/test_retrieval_quality.py tests/unit/test_tutorial.py
```

| Test file | Covers |
| --- | --- |
| [`test_retrieval_quality.py`](../../tests/unit/test_retrieval_quality.py) | BM25 prefers rare terms and finds exact codes; BM25 needs a store with `keyword_search`; RRF fuses ranks and keeps component scores; the hybrid signature and gate; inconsistent settings refused; HyDE embeds question + passage; query rewrite runs only on follow-ups and never reaches the answer prompt |
| [`test_tutorial.py`](../../tests/unit/test_tutorial.py) | the tutorial's custom retriever, as written |
| [`test_pgvector.py`](../../tests/integration/test_pgvector.py) | BM25 in SQL, following document replacement (`make test-db`) |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `retrieval uses bm25 but the vector store QdrantStore has no keyword_search` | BM25 or hybrid on Qdrant | `strategy: dense`, or pgvector. |
| `retrieval.gate dense is not one of the retrievers ['bm25']` at startup | The gate names a retriever that isn't running | Fix `retrieval.gate`, or use `auto`. |
| `retrieval.hyde.apply_to [...] matches none of [...]` | HyDE is on for retrievers not in the strategy | Fix `hyde.apply_to`. |
| Unknown retriever name at startup | A typo, or your module isn't imported | Check `retrieval.strategy`; import it from `domain/__init__.py`. |
| Log: "retrieval thresholds are provisional" | The scorer changed and has no calibration | `make calibrate-apply`. |
| Exact codes (`E309`) not found | Dense alone blurs rare tokens | `strategy: hybrid`. |
| Follow-ups ("and for teams?") get clarifying questions | The search only sees the follow-up | `query_rewrite.enabled: true`. |

## Related

- Next steps for the hits: [reranking](reranking.md), then
  [weak retrieval and calibration](weak-retrieval-and-calibration.md).
- Where searches run: [vector stores](vector-stores.md); what they compare: [embeddings](embeddings.md).
- Build your own: [tutorial](../tutorial.md), [extending: retrievers](../extending.md#add-or-replace-a-retriever).
- Measuring: [evals](../../evals/README.md#metrics).
- Terms: [dense retrieval](../glossary.md#dense-retrieval), [BM25](../glossary.md#bm25),
  [hybrid retrieval](../glossary.md#hybrid-retrieval), [RRF](../glossary.md#rrf),
  [HyDE](../glossary.md#hyde), [scorer](../glossary.md#scorer).
