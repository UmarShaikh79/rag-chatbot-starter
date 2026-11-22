# ADR 022: Eval runner and regression gate

Status: Accepted (milestone 3); implements ADR 010

## Decision

`chatbot eval` runs the golden set twice: through the retrieval pipeline (hit@k, recall@k,
MRR, per document) and through the orchestrator (answer rate, required phrases, citation
precision, faithfulness from the groundedness judge, abstention on unanswerable questions,
latency, tokens). Conversations and memory jobs stay in memory during a run.

`--gate` checks the summary against `evals/gate.yaml` for the active `CHATBOT_PROFILE`:
floors (`min`), ceilings (`max`) and `max_drop` from the committed baseline
(`evals/baselines/<profile>.json`, refreshed only with `--update-baseline`).

CI's `evals` job runs the offline profile on every push and pull request: migrate, ingest the
samples, `calibrate --apply --check`, `eval --gate`. A scheduled workflow
(`evals-live.yml`) runs the hosted profile nightly when an `OPENAI_API_KEY` secret exists,
and only reports until its floors are tuned.

## Alternatives considered

- Cached model responses replayed on pull requests (the plan's first idea): the cache goes
  stale with every prompt or retrieval change and needs its own tooling; the deterministic
  offline profile gives the same "no key, no flakiness" property for regressions in code.
- Hosted eval platforms (Langfuse datasets, Braintrust, Ragas): useful later and pluggable,
  but not a requirement for a template.

## Why

Retrieval or prompt changes are measured, not eyeballed; the offline gate catches code
regressions on every pull request without a key, and the nightly run measures real quality.

## Consequences

- Offline answers are extractive quotes, so offline floors guard against regressions, not
  absolute quality. Tune the `dev`/`prod` floors after the first live run.
- The lexical faithfulness judge is blind to paraphrase and negation; use `llm` with real
  models.
