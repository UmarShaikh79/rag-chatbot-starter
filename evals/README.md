# Evals

`datasets/golden.jsonl` holds 34 questions about the sample corpus: 24 answerable (with the
documents that hold the answer and phrases a correct answer contains) and 10 unanswerable
ones, which should not get an answer. Replace it with questions about your own documents.

## Commands

```bash
chatbot calibrate            # sweep min_score + min_hits for the current pipeline (dry run)
chatbot calibrate --apply    # ...and store them with the index (ADR 011, ADR 021)
chatbot eval                 # retrieval + answer metrics for the whole set
chatbot eval --retrieval-only        # no answer generation, no LLM cost
chatbot eval --gate --out report.json  # exit 1 below evals/gate.yaml; full per-question report
chatbot eval --update-baseline       # save this run as evals/baselines/<profile>.json
```

All of them use the active profile (`CHATBOT_PROFILE`) and an already ingested index. Eval
runs keep conversations and memory jobs in memory, so they leave nothing in your database;
`calibrate --apply` writes one row to `retrieval_calibrations`.

The Docker image includes this folder, so the same commands run in a container:
`docker compose -f docker/compose.yaml exec api chatbot calibrate --apply`, or
`kubectl -n <namespace> exec deploy/chatbot-api -- chatbot calibrate --apply` on Kubernetes. The dataset is
the one baked into the image: rebuild after editing it. `--update-baseline` writes inside the
container, so run it on your machine (`uv run`) to get a file you can commit.

## Metrics

| metric | meaning |
| --- | --- |
| `retrieval.hit@k` | share of answerable questions with an expected document in the first k documents |
| `retrieval.recall@top_k` | share of expected documents found in the top_k hits |
| `retrieval.mrr` | mean reciprocal rank of the first expected document |
| `answer.answer_rate` | answerable questions that got `kind: answer` |
| `answer.must_include_rate` | answerable questions whose answer contains every `must_include` phrase |
| `answer.citation_precision` | share of cited documents that are expected documents |
| `answer.faithfulness` | share of answer claims the cited sources support (groundedness judge) |
| `abstention.unanswerable_rate` | unanswerable questions that got `clarify` or `refuse` |
| `errors`, `latency.*`, `usage.*` | failures, p50/p95 latency, tokens and cost (if prices are set) |

Retrieval metrics count documents, not chunks. Faithfulness uses
`evals.faithfulness_judge`: `llm` (the `judge_groundedness/v1` prompt) or `lexical`
(word overlap; the offline profile's fake model is not a judge).

## Calibration

`chatbot calibrate` replays retrieval for every question and tries thresholds midway between
the observed gate scores, with `min_hits` from 1 to 3. It keeps the pairs that still serve at
least 90% of answerable questions (`evals.calibration.target_answer_recall`) and picks the one
that turns away the most unanswerable questions. The result is stored per index (embedding
model) and scorer (gate score plus pipeline, e.g. `dense|hybrid(dense,bm25)`), so switching on
rerank or HyDE needs a new calibration; until then the API warns that thresholds are
provisional.

On the offline profile the hashing embedder cannot tell most unanswerable questions apart
(abstention about 0.1 to 0.3 with dense, about 0.7 with BM25 as the gate), which is why the
calibration report matters more than any default.

Calibrating and evaluating on the same questions flatters the numbers. With a larger set,
calibrate on one part and evaluate on another (`--dataset`).

## CI gate

`evals/gate.yaml` has floors per profile; `max_drop` also fails a metric that falls more
than that below `evals/baselines/<profile>.json`. CI's `evals` job runs the offline profile:
migrate, ingest the samples, `calibrate --apply --check`, `eval --gate`. The nightly
`evals-live` workflow runs the `dev` profile against OpenAI when the repository has an
`OPENAI_API_KEY` secret; it reports only until you tune the `dev` floors and commit a baseline.

## Dataset fields

| field | meaning |
| --- | --- |
| `id` | stable ID |
| `question` | what the user asks |
| `answerable` | false when the corpus does not contain the answer |
| `expected_documents` | document IDs that contain the answer (required when answerable) |
| `must_include` | phrases a correct answer contains (case-insensitive) |
| `tags` | optional labels for slicing, e.g. `exact-term` |
