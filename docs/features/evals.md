# Evals

How you know a change made the bot better and not worse: a **golden set** of questions with
known answers runs through the real pipeline and is scored on retrieval, answers and
abstention; a **gate** fails CI when a score drops below its floor or falls too far from the
committed baseline. The command reference and metric definitions are in
[evals/README.md](../../evals/README.md); this guide follows the code.

Back to the [docs home](../README.md) · See also: [evals reference](../../evals/README.md)

## At a glance

| | |
| --- | --- |
| **What it does** | Runs every golden-set question through retrieval and through the full orchestrator; scores hit@k, recall, MRR, answer rate, required phrases, citation precision, faithfulness, abstention, errors, latency and cost; checks the scores against floors and a baseline. |
| **Run with** | `chatbot eval` (`make eval`: with `--gate --out eval-report.json`), `chatbot eval --retrieval-only` (`make eval-retrieval`), `chatbot eval --update-baseline` |
| **Main files** | [`evals/runner.py`](../../src/chatbot/evals/runner.py) (`run_retrieval`, `run_answers`, `summarize`), [`evals/metrics.py`](../../src/chatbot/evals/metrics.py), [`evals/gate.py`](../../src/chatbot/evals/gate.py) (`check`), [`evals/dataset.py`](../../src/chatbot/evals/dataset.py) (`GoldenItem`, `load_dataset`), [`evals/commands.py`](../../src/chatbot/evals/commands.py) (`evaluate`, `calibrate`); data in [`evals/datasets/golden.jsonl`](../../evals/datasets/golden.jsonl), [`evals/gate.yaml`](../../evals/gate.yaml), [`evals/baselines/`](../../evals/baselines) |
| **Depends on** | An ingested index for the active profile; for answer metrics, the configured models (no cost on `offline`) |
| **Used by** | CI's `evals` job (offline profile, gated), the nightly `evals-live` workflow (dev profile, report only), you before merging a change |
| **Decisions** | ADR [010](../adr/010-evals-as-a-ci-gate.md) (evals as a CI gate), ADR [022](../adr/022-eval-runner-and-regression-gate.md) (runner and regression gate), ADR [021](../adr/021-calibration-per-index-and-scorer.md) (calibration) |

## What it does

The golden set ships with 35 questions about the sample documents: 25 answerable (with the
documents that hold the answer and phrases a correct answer contains) and 10 unanswerable
(the bot should ask instead). One line per question:

```json
{"id": "q01", "question": "How long is the refund window for annual plans?", "answerable": true, "expected_documents": ["samples:refund-policy.md"], "must_include": ["30 days"]}
```

A real run on the offline profile (hashing embedder, fake model; calibrated first), which
passed its gate:

```json
{
  "count.questions": 35, "count.answerable": 25, "count.unanswerable": 10,
  "retrieval.hit@1": 0.88, "retrieval.hit@3": 1.0, "retrieval.hit@top_k": 1.0,
  "retrieval.recall@top_k": 1.0, "retrieval.mrr": 0.9333,
  "answer.answer_rate": 0.88, "answer.must_include_rate": 0.56,
  "answer.citation_precision": 0.9091, "answer.faithfulness": 1.0,
  "abstention.unanswerable_rate": 0.4,
  "errors": 0, "latency.p50_ms": 13.8, "latency.p95_ms": 21.9,
  "usage.tokens": 16229, "usage.cost_usd": null
}
```

with `meta` naming what was measured: the dataset fingerprint (`golden.jsonl@6a6eb883`), the
index, the embedding model, the scorer (`dense|dense`), whether it was calibrated, the answer
model and the faithfulness judge. The offline numbers guard against regressions; they don't
measure real quality (the fake model quotes, the hashing embedder matches words). The
measured quality with real models is in the [README](../../README.md).

## How it works

[`evaluate(components, dataset, retrieval_only)`](../../src/chatbot/evals/commands.py), called by
`chatbot eval`:

1. **Load** the dataset (`load_dataset`): each line becomes a `GoldenItem`; answerable items
   must name `expected_documents`, unanswerable ones may not have expected answers. Lines
   starting with `#` are comments.
2. **Retrieval pass** (`run_retrieval`): every question through the retrieval pipeline,
   `evals.concurrency` (4) at a time, inside `patient_reranking()` (rate limits are waited out,
   never fallen back from). Each row keeps the hits with their gate scores, which is also what
   `chatbot calibrate` sweeps ([weak retrieval and calibration](weak-retrieval-and-calibration.md)).
3. **Answer pass** (`run_answers`, skipped with `--retrieval-only`): every question through
   `orchestrator.run`. The eval components use in-memory conversation memory and job queue, so
   nothing is written to your database. Per question: `kind`, citations, latency, usage;
   `must_include` (all phrases present after normalising case, quotes, dashes, markdown and
   contractions; a list entry means "any of these"); citation precision; faithfulness from the
   groundedness judge (`evals.faithfulness_judge`). One failing question is recorded as an
   error and doesn't stop the run.
4. **Summarize** (`summarize`): document-level retrieval metrics (a document counts once even if
   several of its passages were retrieved), answer metrics over answerable questions,
   abstention over unanswerable ones, errors, p50/p95 latency, total tokens and cost.

Then `cmd_eval` in [`cli.py`](../../src/chatbot/cli.py) prints the summary, writes `--out`, and with
`--gate` runs [`gate.check(summary, rules, baseline)`](../../src/chatbot/evals/gate.py):

- every metric under `min` must be at or above its floor, and (with `max_drop`) not more than
  that below the committed baseline `evals/baselines/<profile>.json`;
- every metric under `max` must be at or below its ceiling (`errors: 0`);
- a metric the run didn't produce (answer metrics with `--retrieval-only`) is skipped.

The gate rules are loaded **before** the run, so a profile without rules fails at once instead
of after a paid run. In GitHub Actions the summary is also written to the job summary as a table.

**In CI** (`.github/workflows/ci.yml`, job `evals`): Postgres, migrate, ingest the samples,
`chatbot calibrate --apply --check`, `chatbot eval --gate --out eval-report.json` on the offline
profile; the report is uploaded as an artifact. `evals-live.yml` runs the dev profile nightly
when the repository has an `OPENAI_API_KEY` secret, and reports without gating.

## Inputs and outputs

**In:** the dataset (`evals.dataset`, or `--dataset`), the active profile's index and models,
the gate rules and baseline for the profile.

**Out:** the summary and `meta` (printed as JSON); with `--out`, the full per-question report
(`retrieval` and `answers` rows: hits, scores, kind, answer, citations, latency, errors,
unsupported claims); with `--update-baseline`, a new `evals/baselines/<profile>.json`; exit code
1 when the gate fails, with each failure printed. A real failure, with the MRR floor raised to
0.95 in a scratch gate file (with `--retrieval-only`, the abstention floor in the same file was
skipped):

```text
eval gate FAILED:
  retrieval.mrr = 0.9333 is below the floor 0.95
```

## Turn it on, change it, turn it off

```bash
make eval                                # gated, report in eval-report.json
make eval-retrieval                      # retrieval only: no model cost
uv run chatbot eval --update-baseline    # after a deliberate improvement; commit the file
```

| Setting | Default | Notes |
| --- | --- | --- |
| `evals.dataset` | `evals/datasets/golden.jsonl` | |
| `evals.gate` / `baselines_dir` | `evals/gate.yaml` / `evals/baselines` | |
| `evals.hit_at` | `[1, 3]` | Extra hit@k values reported. |
| `evals.faithfulness_judge` | `llm` | `lexical` (offline) or `off`. |
| `evals.concurrency` | 4 | Questions in flight; lower it for rate-limited keys. |
| `evals.calibration.*` | | [Weak retrieval and calibration](weak-retrieval-and-calibration.md). |

**Your own golden set** is the most valuable thing to add when you adopt the template:

1. Write 30–100 real questions users ask about your documents, with the document IDs that
   answer them (`chatbot index list` and `GET /v1/sources` show IDs; files are
   `<source>:<path>`) and one or two phrases a right answer must contain.
2. Add at least 10 **unanswerable** questions: things users ask that your documents don't cover.
3. `make calibrate-apply`, then `make eval`. Set floors in `evals/gate.yaml` a little under what
   you measure, `--update-baseline`, commit both.
4. From then on, every change to chunking, retrieval, prompts or models is measured the same way.

Calibrating and evaluating on the same questions flatters the numbers; with a larger set, keep
a held-out file for `chatbot eval --dataset`.

**Turning it off:** remove the `evals` job from CI (not recommended: it's what catches
regressions), or run with `--gate` only where you want it enforced.

## Extend it

- **A new metric:** add a function to [`evals/metrics.py`](../../src/chatbot/evals/metrics.py),
  fill it per row in `run_answers` (add the field to `AnswerRow`), and average it in
  `summarize` under a new key. It can then be gated in `evals/gate.yaml` by that key.
- **Slices:** tag questions (`"tags": ["exact-term"]`) and compute a summary per tag from the
  `--out` report, or in `summarize`.
- **Another judge:** register a `groundedness_judge` (`judge(answer, sources) -> Judgement`) and
  set `evals.faithfulness_judge` to its name.
- **From your own script or test:** `evaluate(components, dataset)` and
  `calibrate(components, dataset, apply)` take any set of components, including in-memory ones.

## Test it

```bash
uv run pytest tests/unit/test_evals.py
```

[`test_evals.py`](../../tests/unit/test_evals.py): document-level metrics; the golden set is
valid and balanced; inconsistent items are rejected; the calibration sweep; floors, ceilings
and the baseline drop; the committed gate covers every profile; evaluate scores retrieval and
answers; calibrate stores thresholds for the pipeline; an unknown profile fails before running;
the report is written even when the gate fails.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| "the index is empty: run `chatbot ingest` before `chatbot eval`" | Nothing ingested for this profile's index | `make ingest` (with the same `CHATBOT_PROFILE`). |
| Gate fails for an unknown profile before running | No rules for `CHATBOT_PROFILE` in `evals/gate.yaml` | Add the profile, or run without `--gate`. |
| `retrieval.*` near 0 | `expected_documents` IDs don't match the indexed IDs | Use the IDs `GET /v1/sources` shows (`samples:refund-policy.md`). |
| `must_include_rate` low but answers look right | A real model rephrases | Add alternatives: `[["not refunded", "no refund"]]`. |
| `abstention.unanswerable_rate` low | Thresholds let off-topic questions through | Calibrate; add more unanswerable questions; rerank. |
| Gate fails with "dropped more than 0.05 from the baseline" | A real regression, or a deliberate change | Investigate; if intended, `--update-baseline` and commit. |
| Live evals are slow or rate-limited | Hosted rate limits | Lower `evals.concurrency`; a trial Cohere key allows 10 rerank calls a minute. |

## Related

- Thresholds from the same questions: [weak retrieval and calibration](weak-retrieval-and-calibration.md).
- What the answer metrics judge: [answers and streaming](answers-and-streaming.md),
  [guardrails: groundedness](guardrails.md#groundedness).
- Cost of a run: [usage and cost](usage-and-cost.md).
- Metric definitions and commands: [evals reference](../../evals/README.md).
- Terms: [eval](../glossary.md#eval), [golden set](../glossary.md#golden-set),
  [calibration](../glossary.md#calibration).
