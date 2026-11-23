# Usage and cost

How many tokens each chat turn, background job and ingest run used, and what it cost: priced
from the per-model prices in `config/models.yaml`, returned with every chat response, and
stored as one row per operation so you can report by day, user, model or operation.

Back to the [docs home](../README.md) · See also: [observability reference: cost](../observability.md#cost-and-token-accounting)

## At a glance

| | |
| --- | --- |
| **What it does** | Records every model, embedding and rerank call (model, role, purpose, tokens, cost, duration) in a per-operation ledger; returns the turn's total in `usage`; writes one `usage_records` row per turn, job or CLI run; reports over them. |
| **Turned on by** | On by default with Postgres: `observability.usage.persist`. Costs need `price_per_mtok` (or rerank prices) in [`config/models.yaml`](../../config/models.yaml). |
| **Main files** | [`observability/usage.py`](../../src/chatbot/observability/usage.py) (`priced`, `priced_mixed`, `UsageCall`, `UsageRecord`, `Ledger`, `metering`, `record_call`), [`observability/instrument.py`](../../src/chatbot/observability/instrument.py) (`MeteredLLM`, `MeteredEmbedder`, `MeteredOrchestrator`, `metered_operation`), [`observability/accounting.py`](../../src/chatbot/observability/accounting.py) (`PostgresUsageStore`, `UsageReport`), [`api/routes/usage.py`](../../src/chatbot/api/routes/usage.py), `chatbot usage` in [`cli.py`](../../src/chatbot/cli.py) |
| **Depends on** | Postgres for the records; prices for costs |
| **Used by** | `usage` in every chat response, `GET /v1/usage`, `chatbot usage` / `make usage`, the `chatbot.cost` metric, `usage.*` in eval reports |
| **Decisions** | ADR [028](../adr/028-usage-records-per-operation.md) (usage records per operation) |

## What it does

Every chat response carries the turn's total:

```json
"usage": {"prompt_tokens": 554, "completion_tokens": 24, "embedding_tokens": 11, "cost_usd": null}
```

(`cost_usd` is `null` here because the offline fake model has no price.) And every operation
leaves a row you can report on. A real report from a development database, by model, last 7
days:

```text
$ chatbot usage --by model --days 7
* 7146 calls had no price: set price_per_mtok in config/models.yaml (ADR 028)
model                           ops     prompt completion  embedding cost_usd
fake-extractive                1709    1324410      25015          0         n/a*
gemma3:4b                         6       1025        220          0         n/a*
gpt-6.2-sol                      15       6565        641          0       0.0195
gpt-6-luna                       10       1735        629          0       0.0005
hashing-v1                     5401          0          0    4000968         n/a*
nomic-embed-text                 22          0          0       2071         n/a*
qwen3.5:9b                        8       5016        469          0         n/a*
text-embedding-3-small           41          0          0       1804       0.0000
TOTAL                          7212    1338751      26974    4004843      0.0201*
```

and by operation:

```text
$ chatbot usage --by operation --days 7
operation                       ops     prompt completion  embedding cost_usd
chat                           1410    1318961      26378      10161      0.0198*
job:ingest_documents             16          0          0    3985612      0.0000*
job:memory_refresh               84      19790        596          0      0.0002*
job:sync_source                  11          0          0       9070      0.0000*
TOTAL                          1521    1338751      26974    4004843      0.0201*
```

**Never a silent guess:** a call to a model without a price counts its tokens, leaves its cost
out, and is counted in `unpriced_calls`; totals that include such calls are marked `*`, so a
report can't look complete when it isn't.

## How it works

1. **A ledger per operation.** `MeteredOrchestrator` opens `metering()` around each chat turn;
   `metered_operation(...)` does the same around each background job and
   `chatbot ingest --inline`. The ledger lives in a [ContextVar](../glossary.md#contextvar), so
   concurrent turns never mix; a nested operation also counts towards the outer one.
2. **Every call is recorded.** The container wraps every LLM and embedder in `MeteredLLM` /
   `MeteredEmbedder`. Each call prices its tokens with the profile's `price_per_mtok`
   (`input` and `output` for chat, `input` for embeddings) and calls `record_call(UsageCall(...))`
   with `kind`, `model`, `role` (answer, cheap, embed, rerank), `purpose` (answer, clarify, hyde,
   rewrite_query, summarize, extract_facts, guard_injection, judge_groundedness, documents,
   query, rerank), tokens, cost and duration. The Cohere reranker records its own call, priced
   from what Cohere billed (`price_per_ksearch` or `price_per_mtok.input`).
3. **The response total.** The orchestrator adds up the priced usage of its calls into
   `state.usage` (`priced`, `priced_mixed` for retrieval, which can mix embedding, cheap-model
   and rerank costs) and returns it as `usage`.
4. **The record.** When the operation ends, even if it failed (its calls were billed anyway),
   `ledger.record(operation, trace_id)` builds a `UsageRecord` (operation, trace ID, user,
   conversation, totals, the list of calls, `unpriced_calls`), and
   `PostgresUsageStore.record` inserts it. An operation with no model calls (a message blocked
   by the input guards) writes no row. A write error is logged and never fails the operation.
5. **Reports.** `PostgresUsageStore.report(group_by, since, until, user_id)` sums the rows by day
   (UTC), user or operation, or unnests the calls to sum by model. Eval runs write no records.

## Inputs and outputs

**In:** prices in `config/models.yaml`; the calls made during an operation.

**Out:**

- `usage` in each chat response: `prompt_tokens`, `completion_tokens`, `embedding_tokens`,
  `cost_usd` (the sum of the priced calls; `null` when none was priced, so a turn that mixes
  priced and unpriced models under-reports: check `unpriced_calls` in its record).
- A `usage_records` row: `operation` (`chat`, `job:sync_source`, `job:ingest_documents`,
  `job:memory_refresh`, `ingest:<source>`), `trace_id`, `user_id`, `conversation_id`, totals,
  `unpriced_calls`, and `calls` (JSON, one entry per call).
- A `UsageReport` from `GET /v1/usage` (`group_by`, `since`, `until`, `rows`, `total`; each row
  `key`, `operations`, tokens, `cost_usd`, `unpriced_calls`). `operations` counts rows (turns,
  jobs), or calls when grouped by model.

## Turn it on, change it, turn it off

**Set prices** (USD per million tokens) for the models you use:

```yaml
llm:
  openai:answer:
    price_per_mtok: {input: 2.00, output: 10.00}
embeddings:
  openai:text-embedding-3-small:
    price_per_mtok: {input: 0.02}
rerankers:
  cohere:rerank:
    price_per_ksearch: 2.0          # or price_per_mtok: {input: ...}, whichever Cohere bills you
```

The OpenAI profiles ship with prices read on the date noted in the file; check them. Local
models (Ollama) cost nothing per call: leave them unpriced, or set `{input: 0, output: 0}` if
you want reports without `*`.

**Report:**

```bash
chatbot usage                        # last 30 days by day
chatbot usage --by user --days 7     # who spent what
chatbot usage --by model --json      # for scripts
make usage BY=operation              # the same inside the Docker stack
curl -s "localhost:8000/v1/usage?group_by=user&since=2026-11-01T00:00:00Z" -H "X-API-Key: $KEY"
```

`GET /v1/usage` needs an API key or an admin token ([authentication](authentication.md)).

| Setting | Default | Notes |
| --- | --- | --- |
| `observability.usage.persist` | true | `false`: no rows, `GET /v1/usage` answers 404; responses still carry `usage`. |
| `price_per_mtok` (LLM, embedding profiles) | OpenAI: set; others: none | |
| `price_per_ksearch`, `price_per_mtok.input` (reranker profiles) | none | |

**Privacy:** forgetting a user keeps their rows for accounting but removes `user_id` and
`conversation_id` from them ([memory](memory.md)). The calls list holds no text.

**Retention:** rows are never deleted automatically; prune old ones with SQL on
`usage_records.created_at` if you need to.

## Extend it

- **Count your own operation** (a script, a new job kind): wrap it, and every model call inside
  is recorded and priced:

  ```python
  from chatbot.observability.instrument import metered_operation

  async with metered_operation("digest:weekly", components.usage_store, user_id=user_id):
      await build_weekly_digest(components, user_id)
  ```

- **A provider that bills its own way** (per request, per character): price the call yourself
  and `record_call(UsageCall(kind="rerank", model=..., role="rerank", purpose="rerank",
  cost_usd=...))`, as `CohereReranker` does.
- **Budgets and alerts:** query `usage_records` (or the `chatbot.cost` metric) per user per day,
  and refuse or warn in a custom orchestrator step when a user is over budget.
- **Another destination:** give the container a different `usage_store` with
  `record(UsageRecord)` and `report(...)`.

## Test it

```bash
uv run pytest tests/unit/test_usage_accounting.py
make test-db     # tests/integration/test_usage_pg.py
```

| Test file | Covers |
| --- | --- |
| [`test_usage_accounting.py`](../../tests/unit/test_usage_accounting.py) | models wrapped once; the turn's record matches the response's usage and cost; unpriced models counted, not guessed; a streamed turn recorded once; an abandoned stream still records what was spent; memory refresh in the worker counts; nested operations count towards the outer one; records written even when the work fails; report grouping and filters; the API is admin-only and says when records are off |
| [`test_usage_pg.py`](../../tests/integration/test_usage_pg.py) | records and reports in Postgres; forgetting a user keeps totals but not the user |
| [`test_retrieval_quality.py`](../../tests/unit/test_retrieval_quality.py) | rerank cost from the billed unit; a priced Cohere call lands in usage and the ledger |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `cost_usd` is `null`, or lower than expected | Some models of the turn have no price | Set `price_per_mtok` for every model the turn uses (`answer`, `cheap`, `embed`, rerank). |
| Reports end in `*` | Some calls have no price | The first line says how many; price them or accept it. |
| `GET /v1/usage` 404 "usage records are off" | `observability.usage.persist: false`, or no Postgres | Turn it on. |
| `GET /v1/usage` 403 | An end-user token | Use an API key or an admin token. |
| Costs don't match the provider's invoice | Cached-input discounts, batch pricing or price changes aren't modelled | Treat the numbers as an upper bound; update prices when they change. |
| No row for a blocked message | No model was called | Expected. |

## Related

- Prices and model profiles: [models and providers](models-and-providers.md),
  [reranking](reranking.md).
- Traces and the `chatbot.cost` metric: [observability](observability.md).
- Jobs that write records: [background jobs](background-jobs.md).
- Cost per eval run (`usage.*` in the report): [evals](evals.md).
