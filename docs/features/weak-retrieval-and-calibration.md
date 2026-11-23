# Weak retrieval and calibration

How the bot decides between **answering** and **asking a clarifying question**: it answers
only when enough retrieved passages score high enough. "High enough" isn't guessed: it's
measured on your questions and documents by `chatbot calibrate`, and stored with the index.

Back to the [docs home](../README.md) · See also: [architecture, step 4](../architecture.md#4-one-chat-request-from-start-to-finish)

## At a glance

| | |
| --- | --- |
| **What it does** | After retrieval, counts the passages whose [gate score](../glossary.md#gate-score) reaches `min_score`. Fewer than `min_hits`: retrieval is *weak*, and the bot asks a clarifying question (or refuses) instead of answering. `chatbot calibrate` picks the two thresholds from the golden set. |
| **Turned on by** | Always on; `retrieval.gate`, `min_score`, `min_hits`, `min_hits_filtered`, `thresholds_from`, `answer.on_weak_retrieval` and `evals.calibration` in [`config/app.yaml`](../../config/app.yaml) |
| **Main files** | [`retrieval/weak.py`](../../src/chatbot/retrieval/weak.py) (`assess`, `split`, `thresholds`), [`evals/calibrate.py`](../../src/chatbot/evals/calibrate.py) (`sweep`), [`evals/commands.py`](../../src/chatbot/evals/commands.py) (`calibrate`), `RetrievalSection.gate_signal` in [`core/settings.py`](../../src/chatbot/core/settings.py), `get_calibration` / `save_calibration` in [`stores/catalog.py`](../../src/chatbot/stores/catalog.py) |
| **Depends on** | [Retrieval](retrieval-strategies.md) scores, the [golden set](../glossary.md#golden-set) in [`evals/datasets/golden.jsonl`](../../evals/datasets/golden.jsonl) |
| **Used by** | [`SimpleOrchestrator.retrieve`](../../src/chatbot/orchestration/simple.py) (routes to answer or clarify) and `clarify` |
| **Decisions** | ADR [006](../adr/006-weak-retrieval-asks-never-invents.md) (ask, never invent), ADR [011](../adr/011-retrieval-thresholds-are-calibrated-never-constants.md) (thresholds are calibrated, never constants), ADR [021](../adr/021-calibration-per-index-and-scorer.md) (per index and scorer), ADR [019](../adr/019-hybrid-retrieval-with-rank-fusion-and-a-gate-score.md) (the gate score) |

## What it does

A retriever always returns *something*, even for "What's the weather in Paris?". If those
passages went to the model, it would be tempted to answer from them. So before answering, the
bot checks retrieval was strong:

> **Strong** when at least `min_hits` passages have a gate score ≥ `min_score`.

Otherwise it [abstains](../glossary.md#abstain): it asks a clarifying question, naming the
topics it did find, or with `answer.on_weak_retrieval: refuse` says *"I couldn't find
information about that in the knowledge base."* Real output on the offline profile:

```json
{
  "answer": "Could you tell me a bit more about what you are looking for?",
  "citations": [],
  "kind": "clarify"
}
```

**Why calibrate?** A score only means something on its own scale. On the sample documents,
calibration chose these `min_score` values (2026-11-22/23):

| Gate score | Calibrated `min_score` |
| --- | --- |
| Dense, OpenAI `text-embedding-3-small` | 0.41 |
| Dense, Ollama `nomic-embed-text` | 0.68 |
| Dense, offline hashing embedder | 0.085 |
| BM25 (offline index) | 2.11 |
| Cohere `rerank-v5.0-fast` | 0.28 |

A fixed `min_score: 0.35` would be too strict for one and too loose for another. `chatbot calibrate`
tries thresholds on questions with known answers and picks the pair that keeps answerable
questions answered while turning away the most unanswerable ones.

**Which score is the gate?** `retrieval.gate: auto` picks:

| Pipeline | Gate |
| --- | --- |
| Reranking on | `rerank` |
| `dense`, or `hybrid` with dense in it | `dense` |
| `bm25` alone (or your single retriever) | that retriever's own score |

Never the fused RRF score of hybrid retrieval: ranks say nothing about relevance.

## How it works

**At chat time**, in [`SimpleOrchestrator.retrieve`](../../src/chatbot/orchestration/simple.py),
after retrieval and the context guard:

1. `calibration = await pipeline.used().calibration(index)`: the stored thresholds for this
   index and this **scorer** (the gate plus the pipeline, e.g. `dense|hybrid(dense,bm25)`), or
   none. After a [rerank fallback](reranking.md#how-it-works) it's the retriever-only pipeline's.
2. [`assess(hits, cfg, calibration, gate, filtered, available)`](../../src/chatbot/retrieval/weak.py):
   1. `thresholds(cfg, calibration)`: the calibrated pair if there is one and
      `thresholds_from: index`; otherwise `min_score` / `min_hits` from config, marked
      provisional (a warning is logged once per scorer).
   2. **Lower `min_hits`** (never `min_score`) in two cases: the request has filters
      (`min_hits_filtered`, default 1: a slice of the corpus has fewer passages to agree), or
      retrieval returned fewer passages than `top_k` (requiring more strong hits than exist
      would always clarify).
   3. `split(hits, gate, min_score, min_hits)`: *strong* = hits whose gate score ≥ `min_score`;
      *usable* = strong hits plus hits with no gate score (a hybrid hit found only by BM25, when
      the gate is dense); *weak* = fewer than `min_hits` strong hits.
3. Route: weak → `clarify`; strong → `answer` with the first `answer.max_context_chunks` (6)
   usable hits. The span `retrieval` records the gate, thresholds and whether they were
   calibrated; the metric `chatbot.retrieval.requests` counts weak and strong turns.

A second safety net sits in the answer step: if the model finds the sources don't contain the
answer it replies `INSUFFICIENT_CONTEXT` (the prompt's abstain marker), and if it cites nothing
while `answer.citations: required`, the turn becomes a clarification too.

**At calibration time**, [`calibrate(components, dataset, apply)`](../../src/chatbot/evals/commands.py):

1. Replays retrieval for every golden-set question (inside `patient_reranking()`, so a hosted
   reranker waits out rate limits rather than falling back).
2. [`sweep(rows, max_context, target_answer_recall, max_min_hits, min_unanswerable)`](../../src/chatbot/evals/calibrate.py):
   - Candidate thresholds are the **midpoints** between neighbouring observed gate scores (so a
     tiny score drift doesn't flip a question), plus "everything passes" and "nothing passes";
     each is tried with `min_hits` 1 to 3.
   - For each pair: **answer recall** = share of answerable questions where retrieval is strong
     *and* an expected document is among the passages the model would see; **abstention** =
     share of unanswerable questions where retrieval is weak.
   - Keep pairs with answer recall ≥ `target_answer_recall` (0.9), or the best recall found;
     pick the highest abstention, then recall, then the widest margin.
3. With `--apply`, stores a `Calibration` with the index under the scorer key.

## Inputs and outputs

**In (chat):** the retrieved hits, the retrieval settings, the stored calibration (if any).

**Out:** an `Assessment`: `strong`, `usable`, `weak`, `gate`, `min_score`, `min_hits`,
`calibrated`, `warnings`.

**Out (`chatbot calibrate`)**, real output on the offline profile (2026-11-23):

```json
{
  "scorer": "dense|dense",
  "min_score": 0.08519,
  "min_hits": 2,
  "answer_recall": 0.92,
  "abstention": 0.2,
  "met_target": true,
  "dataset": "golden.jsonl@6a6eb883",
  "frontier": [ ... the best pair for each min_hits ... ],
  "stored": false
}
```

`dataset` names the file and a fingerprint of its content, so you can tell which questions a
calibration came from. `frontier` shows the trade-off for each `min_hits`. Warnings go to
stderr, for example when no threshold separates unanswerable questions, or when the set has
fewer than 5 unanswerable questions.

`GET /v1/sources` shows the active scorer and the calibration in use
([API](../api.md)).

## Turn it on, change it, turn it off

It's always on. After ingesting, and after **any** change to the embedding model, the
strategy, HyDE, the reranker or the corpus:

```bash
make calibrate          # dry run: what would the thresholds be?
make calibrate-apply    # store them with the index
make eval               # check answers and abstention with them
```

| Setting | Default | Notes |
| --- | --- | --- |
| `retrieval.gate` | `auto` | Or `dense`, `bm25`, `rerank` by hand. |
| `retrieval.min_score` / `min_hits` | 0.35 / 1 | Provisional until calibrated (`offline`: 0.08; `local-ollama`: 0.65). |
| `retrieval.min_hits_filtered` | 1 | `min_hits` for requests with filters; `null` keeps `min_hits`. |
| `retrieval.thresholds_from` | `index` | `config` ignores stored calibrations. |
| `answer.on_weak_retrieval` | `clarify` | `refuse`: a fixed sentence, no model call. |
| `evals.calibration.target_answer_recall` | 0.9 | Share of answerable questions that must still be answered. |
| `evals.calibration.max_min_hits` | 3 | `min_hits` values tried. |
| `evals.calibration.min_unanswerable` | 5 | Warn below this many unanswerable questions. |

**Choosing the target:** a higher `target_answer_recall` answers more but lets more off-topic
questions through; a lower one clarifies more. For a support bot, 0.9 is a sensible start.

**Your own questions:** replace or extend [`evals/datasets/golden.jsonl`](../../evals/datasets/golden.jsonl)
with real questions about your documents, including **unanswerable** ones (questions users ask
that the documents don't cover). Ten or more unanswerable questions make abstention a
measurement rather than a guess. Calibrating and evaluating on the same questions flatters the
numbers; with a larger set, calibrate on one part (`--dataset`) and evaluate on another.
Fields are in [evals: dataset fields](../../evals/README.md#dataset-fields).

**In CI**, the `evals` job runs `chatbot calibrate --apply --check` (exit 1 when the recall
target is missed) before `chatbot eval --gate`.

**Turning it off** isn't recommended (ADR 006), but `thresholds_from: config` with
`min_score` at the bottom of your score scale and `min_hits: 1` effectively answers everything,
leaving only the prompt's `INSUFFICIENT_CONTEXT` path.

## Extend it

- **Another gate score:** put your retriever's score in `hit.scores["<name>"]`, then gate on it.
  The single-retriever case already uses its own score; for a fixed choice, add the name to
  the `gate` `Literal` in `RetrievalSection` ([`core/settings.py`](../../src/chatbot/core/settings.py)).
- **Another decision rule** (e.g. "the best score minus the second best"): change `split` in
  [`retrieval/weak.py`](../../src/chatbot/retrieval/weak.py), and make
  `evaluate_point` in [`evals/calibrate.py`](../../src/chatbot/evals/calibrate.py) use the same
  rule, or calibration and serving disagree.
- **Another clarifying question:** edit the `clarify` prompt (a new version under
  [`prompts/clarify/`](../../prompts/clarify)) and point `answer.clarify_prompt` at it.
- **Store calibrations elsewhere:** implement `get_calibration` / `save_calibration` (the
  `CalibrationStore` Protocol); `CatalogStore` keeps them in Postgres.

## Test it

```bash
uv run pytest tests/unit/test_retrieval_citations.py tests/unit/test_evals.py
```

| Test file | Covers |
| --- | --- |
| [`test_retrieval_citations.py`](../../tests/unit/test_retrieval_citations.py) | `min_score` and `min_hits` apply together; calibrated thresholds win; uncalibrated indexes warn; the gate uses the named score, not the fused rank; filtered requests and short result lists lower `min_hits` |
| [`test_evals.py`](../../tests/unit/test_evals.py) | the sweep separates answerable from unanswerable, uses `min_hits` when one strong hit isn't enough, warns when the target is out of reach; `calibrate` stores thresholds for the pipeline; the golden set is valid and balanced |
| [`test_retrieval_quality.py`](../../tests/unit/test_retrieval_quality.py) | a calibration for this pipeline overrides config; a failed rerank uses the retriever's calibration |
| [`test_pgvector.py`](../../tests/integration/test_pgvector.py) | calibrations are stored per scorer (`make test-db`) |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Log: "retrieval thresholds are provisional" | No calibration for this index and scorer | `make calibrate-apply`. |
| Everything gets a clarifying question | `min_score` above your scale (a new model or reranker, uncalibrated) | Calibrate; check `GET /v1/sources`. |
| Off-topic questions get answers | `min_score` too low, or the embedder can't separate them | Calibrate with unanswerable questions in the set; rerank; a better embedding model. |
| Warning: "no threshold keeps 90% of answerable questions" | Retrieval misses expected documents | Fix retrieval first (chunking, hybrid, rerank); check `expected_documents`. |
| Warning: "only N unanswerable questions" | Too few in the golden set | Add real off-topic questions. |
| Filtered requests clarify too often | `min_hits` too high for a slice | `min_hits_filtered: 1` (the default). |
| Answers changed after re-indexing | Thresholds belong to the index | Calibrate the new index before switching ([re-indexing](reindexing.md)). |

## Related

- Before: [retrieval strategies](retrieval-strategies.md), [reranking](reranking.md).
- Measuring quality: [evals](../../evals/README.md#calibration).
- Terms: [calibration](../glossary.md#calibration), [`min_score` and `min_hits`](../glossary.md#min_score-and-min_hits),
  [gate score](../glossary.md#gate-score), [scorer](../glossary.md#scorer),
  [abstain](../glossary.md#abstain), [clarifying question](../glossary.md#clarifying-question).
