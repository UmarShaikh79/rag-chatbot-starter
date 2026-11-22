# Background jobs and the worker

How slow work runs outside the request: ingesting documents and refreshing memory go into a
queue in Postgres, and a separate *worker* process takes them out and runs them, with retries.

Back to the [docs home](../README.md) · See also: [architecture](../architecture.md#the-big-picture)

## At a glance

| | |
| --- | --- |
| **What it does** | A job queue in the Postgres `jobs` table and a worker that claims jobs safely (several workers at once), retries transient failures with backoff, and records each job's result. |
| **Turned on by** | Always on; the worker runs as its own process: `chatbot worker` (the `worker` service in Compose, the `chatbot-worker` Deployment in Kubernetes) |
| **Main files** | [`ingestion/jobs/queue.py`](../../src/chatbot/ingestion/jobs/queue.py) (`PostgresJobQueue`), [`ingestion/jobs/worker.py`](../../src/chatbot/ingestion/jobs/worker.py) (`Worker`), [`ingestion/jobs/handlers.py`](../../src/chatbot/ingestion/jobs/handlers.py) (the job kinds) |
| **Depends on** | Postgres; the ingestion pipeline and memory manager (the work itself) |
| **Used by** | [Sources and parsers](sources-and-parsers.md) (syncs), memory (summaries and facts), `POST /v1/ingest`, `chatbot ingest` |
| **Decisions** | ADR [004](../adr/004-postgres-for-everything-first.md) (Postgres before a broker), ADR [028](../adr/028-usage-records-per-operation.md) (a usage record per job) |

## What it does

Embedding a few hundred documents takes minutes, and a chat request shouldn't wait for a
conversation summary. So the API only *enqueues* that work and answers at once (`202
Accepted` with a job ID); the worker does it.

- **Safe with many workers:** a job is claimed with `FOR UPDATE SKIP LOCKED`, so two workers
  never take the same job. Add replicas for more throughput.
- **Retries transient failures** (a provider down, a rate limit, a timeout) with exponential
  backoff, up to `ingestion.worker.max_attempts`. Permanent failures (a wrong key, a
  forbidden or missing model, a bad request) fail at once with the reason.
- **Survives crashes:** a job left `running` longer than `lock_timeout_s` (the worker died) can
  be claimed again.
- **No duplicates:** syncing a source that already has a queued or running sync returns that
  job instead of a second one.
- **Observable:** every job gets its own trace (`job:<kind>`), log lines with its trace ID,
  metrics, and a usage record of the model calls it made.

## How it works

**Enqueue** ([`PostgresJobQueue.enqueue`](../../src/chatbot/ingestion/jobs/queue.py)): insert a
row with `kind`, `payload`, `max_attempts` and an optional `dedup_key`. If a queued or running
job has the same `dedup_key`, its ID is returned instead (a unique partial index enforces it).

**Claim** (`claim(kinds)`): in one statement, take the oldest job of a known kind that is either
`queued` and due (`run_after <= now()`), or `running` but locked longer than `lock_timeout_s`;
set it `running`, add 1 to `attempts`, stamp `locked_at`.

**Run** ([`Worker.run_once`](../../src/chatbot/ingestion/jobs/worker.py)):

1. Claim a job; if none, sleep `poll_interval_s` (an idle worker polls once a second).
2. Give it a new trace ID and open a usage ledger
   ([`metered_operation`](../../src/chatbot/observability/instrument.py)) attributed to the
   payload's `user_id`.
3. Call the handler for its kind ([`make_handlers`](../../src/chatbot/ingestion/jobs/handlers.py)).
4. On success: `complete(job_id, result)` stores the handler's result, status `succeeded`.
5. On an exception: `fail(job_id, error, retry=...)`.
   - [`is_permanent(exc)`](../../src/chatbot/core/errors.py) decides `retry`: a 4xx other than
     408/409/425/429, or an error marked permanent, is not retried.
   - Otherwise the job goes back to `queued` with `run_after = now() + 2 s × 2^(attempts − 1)`
     (2 s, 4 s, 8 s ...), or to `failed` once `attempts` reaches `max_attempts`.
   - `last_error` keeps `"Type: message"` with credentials scrubbed.

`Worker.run` starts `concurrency` such loops side by side. On SIGTERM it stops claiming and
finishes the jobs it holds (Kubernetes gives it 120 s); a job cut off is reclaimed after
`lock_timeout_s`.

**The job kinds:**

| Kind | Enqueued by | Payload | Does |
| --- | --- | --- | --- |
| `sync_source` | `POST /v1/ingest {"source": ...}`, `chatbot ingest`, the nightly CronJob | `source`, `force` | `IngestionPipeline.sync_source` on that source ([sources and parsers](sources-and-parsers.md)) |
| `ingest_documents` | `POST /v1/ingest {"documents": [...]}` | `source`, `documents`, `user_id` | `IngestionPipeline.ingest_inline` |
| `memory_refresh` | the memory manager after a chat turn (when `memory.run_in: worker`) | `conversation_id`, `user_id` | update the conversation's summary and the user's facts |

## Inputs and outputs

**A job** (`Job` in [`core/types.py`](../../src/chatbot/core/types.py), one row of `jobs`),
as `GET /v1/jobs/{id}` returns it:

| Field | Meaning |
| --- | --- |
| `id` | UUID |
| `kind`, `payload` | what to do, and its arguments |
| `status` | `queued` → `running` → `succeeded` or `failed` (or back to `queued` for a retry) |
| `attempts` / `max_attempts` | tries so far / the limit |
| `last_error` | the last failure, credentials scrubbed |
| `result` | the handler's return value: for ingest jobs an `IngestReport` (`indexed`, `unchanged`, `removed`, `failed`, `chunks`, `usage`) |
| `created_at`, `updated_at` | timestamps |

**`POST /v1/ingest`** answers `202 {"job_id": ..., "status_url": "/v1/jobs/<id>"}`; poll the
status URL. See [api: `POST /v1/ingest`](../api.md#post-v1ingest).

## Turn it on, change it, turn it off

**Run a worker:** `make up` starts one in Docker; without Docker, `uv run chatbot worker` in a
second terminal. Without a worker, jobs queue up and nothing is ingested.

**Run jobs without a worker** (scripts, CI, one-off loads):

```bash
chatbot ingest --wait     # enqueue, then process the queue in this process until it's empty
chatbot ingest --inline   # skip the queue entirely: sync directly, no retries
```

**Settings** ([configuration: `ingestion`](../configuration.md#ingestion)):

| Setting | Default | Notes |
| --- | --- | --- |
| `ingestion.worker.concurrency` | `2` | Jobs one worker process runs at once. Or run more worker replicas. |
| `ingestion.worker.max_attempts` | `3` | For ingest jobs (`POST /v1/ingest`, `chatbot ingest`). Memory-refresh jobs always allow 3. |
| `ingestion.worker.poll_interval_s` | `1.0` | Idle wait between empty claims. |
| `ingestion.worker.lock_timeout_s` | `600` | A `running` job older than this is reclaimed. Keep it above your longest job. |
| `memory.run_in` | `worker` | `inline` runs memory refresh inside the chat request instead of as a job. |

**Scheduled syncs:** the Kubernetes example has a `chatbot-ingest` CronJob that enqueues a sync
of every source at 03:00 UTC (suspended in the dev overlay); unchanged documents are skipped,
so it's cheap. Outside Kubernetes, run `chatbot ingest` from cron.

**Turning it off** isn't useful (ingestion needs it), but you can avoid running a worker
process by ingesting with `--inline` and setting `memory.run_in: inline`.

**Replacing the queue** (Redis, SQS): implement the `JobQueue` Protocol (`enqueue`, `claim`,
`complete`, `fail(job_id, error, retry=True)`, `get`) and pass it as the `queue` part. The
in-memory `InMemoryJobQueue` in [`testing/fakes.py`](../../src/chatbot/testing/fakes.py) is a
short, complete example.

## Extend it

**A new kind of job** (re-generate thumbnails, export a report, call a webhook): add a handler
in [`make_handlers`](../../src/chatbot/ingestion/jobs/handlers.py) and enqueue it:

```python
async def export_report(job: Job) -> dict[str, Any]:
    rows = await build_report(job.payload["month"])
    return {"rows": len(rows)}  # becomes the job's result


handlers["export_report"] = export_report

# anywhere you have the components:
await c.queue.enqueue("export_report", {"month": "2026-11", "user_id": owner})
```

- Put `user_id` in the payload when the job holds a user's data, so `forget_user` deletes it
  and the usage record is attributed to them.
- Raise an error `from` the HTTP error (or set `permanent = True` on a `ChatbotError`) so the
  worker can tell a permanent failure from a transient one.
- Make the handler safe to run twice: a crashed worker's job is run again.

See [extending: add a job kind](../extending.md#add-a-job-kind).

## Test it

```bash
uv run pytest tests/unit/test_worker_pipeline.py     # retries, permanent errors, dedup (in memory)
make test-db                                          # tests/integration/test_jobs_memory.py on Postgres
```

| Test file | Covers |
| --- | --- |
| [`test_worker_pipeline.py`](../../tests/unit/test_worker_pipeline.py) | retry then fail; permanent errors fail at once; rate limits are retried; dedup reuses a pending job |
| [`test_jobs_memory.py`](../../tests/integration/test_jobs_memory.py) | concurrent claims never share a job; dedup; backoff then fail; `retry=False`; an abandoned job is reclaimed |
| [`test_ingest_to_answer.py`](../../tests/e2e/test_ingest_to_answer.py) | API enqueue → worker → index → chat |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `POST /v1/ingest` returns a job that stays `queued` | No worker is running | `make up`, or `uv run chatbot worker`, or `chatbot ingest --wait`. |
| A job is `failed` after one attempt | A permanent error (wrong key, forbidden model, bad request) | Read `last_error`, fix the cause, enqueue again. |
| A job retries and then fails | The provider or database kept failing | `last_error` and the worker log (same trace ID) say why. |
| A job runs twice | A worker died mid-job; it was reclaimed after `lock_timeout_s` | Expected; keep handlers safe to repeat. Ingest is: unchanged documents are skipped. |
| Two syncs of the same source at once | Not possible: the second returns the first's job ID | |
| `memory_refresh` jobs pile up | Worker not running, or the cheap model is failing | Start a worker; or `memory.run_in: inline`. |

## Related

- What the sync jobs do: [sources and parsers](sources-and-parsers.md).
- Big rebuilds that don't use the queue: [re-indexing and aliases](reindexing.md).
- Tracing jobs and their cost: [observability](../observability.md).
- Terms: [job](../glossary.md#job), [worker](../glossary.md#worker),
  [trace ID](../glossary.md#trace-id).
