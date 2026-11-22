# ADR 021: Calibration per index and scorer

Status: Accepted (milestone 3); refines ADR 011

## Decision

`chatbot calibrate` runs the golden set through the configured retrieval pipeline and sweeps
`min_score` (midpoints between observed gate scores) together with `min_hits` (1 to
`evals.calibration.max_min_hits`). For each pair it measures answer recall (answerable
questions where retrieval is strong and an expected document reaches the model) and
abstention (unanswerable questions where retrieval is weak). It keeps pairs with answer
recall at or above `target_answer_recall` (0.9), then maximises abstention, then recall,
then margin.

`--apply` stores the result in `retrieval_calibrations`, keyed by index (so by embedding
model, ADR 005) and **scorer**: the gate score plus the pipeline that produced it, e.g.
`dense|dense`, `dense|hybrid(dense,bm25)`, `rerank|rerank[rerank-v5.0-fast](hybrid(dense,bm25))`.
At query time the orchestrator looks up the row for its own scorer; with none, it uses the
provisional config values and logs a warning once per scorer.

## Alternatives considered

- One pair per index (milestone 1's columns): silently wrong after enabling rerank or HyDE.
- Optimising F1 or accuracy: treats a missed answer and an invented one as equally bad; the
  recall floor states the trade-off explicitly instead.

## Why

Scores change scale with the embedding model, the reranker, HyDE and the gate signal. Keying
by the full pipeline makes "you changed the pipeline, recalibrate" impossible to miss.

## Consequences

- The dataset needs enough unanswerable questions to measure abstention (the command warns
  below `min_unanswerable`, 5). The sample set has 10 of 34.
- Calibrating and evaluating on the same questions flatters both; with a larger set, hold
  part of it out for `chatbot eval`.
- Migration 0003 moves milestone 1 values to scorer `dense|dense` and drops the old columns.

Addendum (after the first Qdrant run): `min_hits` is calibrated on the whole corpus, so it
is lowered for requests that search less of it: to `retrieval.min_hits_filtered` (default 1)
when the request has filters, and never above the number of chunks retrieval returned.
Without this, a question filtered to one document (one customer, one product) was judged weak
whenever that document had fewer strong chunks than the corpus-wide `min_hits`. `min_score` is
never relaxed.
