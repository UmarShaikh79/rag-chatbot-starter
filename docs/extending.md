# Extending the template

Every swappable part is a `typing.Protocol` in `src/chatbot/core/interfaces.py`. Implementations
register a factory under a name; config picks the name; `core/container.py` builds the parts.
Put your code in `src/chatbot/domain/` (imported last, so your registrations override built-ins)
or edit the built-ins directly: it is your copy.

## Add a source

Rows from a SQL database need no code: use `type: sql` (see
[configuration](configuration.md#ingestion)). For anything else (a CMS, a ticketing system, an
object store), implement `iter_documents()`:

```python
# src/chatbot/domain/cms_source.py
from collections.abc import AsyncIterator
import os

import httpx

from chatbot.core.registry import registry
from chatbot.core.types import RawDocument


class CmsPages:
    def __init__(self, name: str, base_url: str, token: str):
        self.name, self.base_url, self.token = name, base_url, token

    async def iter_documents(self) -> AsyncIterator[RawDocument]:
        async with httpx.AsyncClient(base_url=self.base_url) as client:
            resp = await client.get("/pages", headers={"Authorization": f"Bearer {self.token}"})
            resp.raise_for_status()
            for page in resp.json():
                yield RawDocument(
                    id=f"{self.name}:{page['id']}",
                    source=self.name,
                    uri=page["url"],
                    title=page["title"],
                    media_type="text/html",
                    content=page["html"].encode(),
                )


@registry.register("source", "cms")
def make(cfg, root_dir):
    return CmsPages(cfg.name, cfg.options["base_url"], os.environ[cfg.options["token_env"]])
```

Import it from `src/chatbot/domain/__init__.py`, then configure it:

```yaml
ingestion:
  sources:
    - {name: samples, type: files, path: samples/docs}
    - {name: help, type: cms, options: {base_url: "https://cms.example.com/api", token_env: CMS_TOKEN}}
```

`chatbot ingest --source help --wait` syncs it. Only changed documents are re-embedded and
documents that disappeared are removed, but only after the source was read to the end: a
source that raises part way leaves the index as it was. If your app would rather push records,
use `POST /v1/ingest` with `documents` instead and skip the source class.

## Add a parser

A parser has `media_types` and `parse(raw) -> ParsedDocument`. Return one `Section` per heading
(or page) so chunks never straddle sections and citations can name them.

```python
from chatbot.core.registry import registry
from chatbot.ingestion.parsers.base import build, normalise
from chatbot.core.types import Section


class CsvParser:
    media_types = ("text/csv",)

    def parse(self, raw):
        rows = raw.content.decode().splitlines()
        return build(raw, [Section(text=normalise("\n".join(rows)))])


registry.add("parser", "csv", CsvParser)
```

Also map the extension in `ingestion/sources/files.py` (`MEDIA_TYPES`).

## Add or replace a retriever

```python
@registry.register("retriever", "dense_mmr")
def make(embedder, store, alias, **kwargs):  # kwargs: settings=...
    return MyRetriever(embedder, store, alias)  # needs .name, .retrieve() and .index()
```

Set `retrieval.strategy: dense_mmr`, or list it in `retrieval.hybrid.retrievers` to fuse it
with others. Return `(hits, usage)`, put your score in `hit.scores["dense_mmr"]` as well as
`hit.score`, and give the class a `signature` (it becomes part of the calibration key). Then
run `chatbot calibrate --apply` (ADR 011, ADR 021). If your score should be the gate, set
`retrieval.gate` (or extend the `Literal` in `RetrievalSection`).

Keyword search lives on the store (`keyword_search`, ADR 020). To use ParadeDB `pg_search`,
OpenSearch or another engine, subclass `PgVectorStore` and override that one method.

## Add a reranker

```python
@registry.register("reranker", "voyage")
def make(profile):  # a RerankerProfile from config/models.yaml
    return MyReranker(profile)  # .signature and async .rerank(query, hits, top_n)
```

Use `chatbot.providers.rerankers.apply_scores(hits, scores, top_n)` to record the scores.
Retry rate limits yourself and raise `RerankError` when you give up: chat turns then fall back to
the retrieval order (`retrieval.rerank.fallback`), and `is_patient()` tells you when calibrate or
eval wants you to wait longer.
Add an entry under `rerankers:` in `config/models.yaml` and point `retrieval.rerank.model` at it.

## Add eval questions

Append lines to `evals/datasets/golden.jsonl` (see `evals/README.md`). Include unanswerable
questions that sound plausible for your domain: they are what calibration learns abstention
from. Re-run `chatbot calibrate --apply`, then `chatbot eval --update-baseline` and commit the
baseline with the questions.

## Add a PII or injection detector

Detectors are the easiest way to strengthen the guards (ADR 018). A PII detector returns spans;
an injection detector returns reasons (empty when the text looks safe). Register one and list
its name in config:

```python
# src/chatbot/domain/detectors.py
import re

from chatbot.core.registry import registry
from chatbot.core.types import PiiSpan

EMPLOYEE_ID = re.compile(r"\bEMP-\d{6}\b")


class EmployeeIds:
    name = "employee_ids"

    def find(self, text):
        return [
            PiiSpan(start=m.start(), end=m.end(), label="EMPLOYEE_ID")
            for m in EMPLOYEE_ID.finditer(text)
        ]


@registry.register("pii_detector", "employee_ids")
def make_employee_ids(cfg):  # cfg is the guardrails section
    return EmployeeIds()


class HostedClassifier:
    name = "hosted"

    async def detect(self, text, stage):  # stage: "input" or "context"
        flagged = await call_your_moderation_api(text)
        return ["hosted"] if flagged else []


@registry.register("injection_detector", "hosted")
def make_hosted(cfg, llms, prompts):
    return HostedClassifier()
```

```yaml
guardrails:
  pii: {detectors: [regex, employee_ids]}
  injection: {detectors: [heuristic, hosted]}
```

The configured redactor is used everywhere PII is handled: input, output, the streaming
redactor, persistence and memory.

## Add a guard

Guards have `name`, `stage` (`input`, `context` or `output`) and `async check(payload)`
returning `allow`, `modify` (with new text) or `block`. `payload.metadata` carries
`system_prompt` at the output stage. Add yours to `GuardPipeline.from_config` in
`guardrails/pipeline.py`. A `block` at the output stage turns the answer into a refusal, also in
streaming mode.

## Change how memory works

`memory/manager.py` holds the summary and fact logic; the prompts are `prompts/summarize/v1.yaml`
and `prompts/extract_facts/v1.yaml`. To use a memory service instead (Mem0, Zep, ...), replace
`MemoryManager` with a class that has the same `after_turn`, `refresh` and `facts_in_prompt`
members, pass it to `container.build(..., memory_manager=...)`, and keep `forget_user` deleting
whatever it stores.

## Map identity-provider claims

`auth/jwt.py` turns a verified token into a `Principal` (`subject`, `scopes`, `admin`). If your
provider puts permissions in another claim (`roles`, `groups`, `permissions`), extend
`scopes_of` there. The principal is available in routes as
`Depends(chatbot.auth.principal.current_principal)`.

## Add an LLM or embedding provider

Implement `generate()` and `stream()` (or `embed_documents()` / `embed_query()` with `model_id`
and `dims`), register it with `@registry.register("llm", "myprovider")`, and add a profile to
`config/models.yaml` with `provider: myprovider`. `providers/llm/ollama.py` is a small
example over plain HTTP. Any OpenAI-compatible server (vLLM, LM Studio, Azure OpenAI with a
compatible endpoint) works with `provider: openai` and a `base_url`.

When the upstream fails after your retries, raise `chatbot.core.errors.ProviderError` (or let an
`httpx` error through): the API answers 502 with your message, not 500. Don't put keys or raw
upstream bodies in that message; clients see it.

Whatever provider you add is traced and priced without extra code: the container wraps every
LLM and embedder it builds (`observability/instrument.py`). Set `price_per_mtok` in its profile
to get costs.

## Change the orchestration flow

A chat turn is a list of steps on `SimpleOrchestrator` (`check_input`, `retrieve`, `answer`,
`clarify`, `check_grounded`, `check_output`). Each reads and updates one `TurnState`
(`orchestration/state.py`) and sets `state.route` to the next step. To change the flow:

- **Change a step**: subclass `SimpleOrchestrator`, override the method, register the subclass
  with `@registry.register("orchestrator", "mine")` in `chatbot.domain` and set
  `orchestrator: mine`. Tracing, cost accounting and the SSE contract still apply.
- **Add steps, loops or tools**: start from the LangGraph adapter
  (`uv sync --extra langgraph`, `orchestrator: langgraph`, ADR 029). Subclass
  `LangGraphOrchestrator` and change `build_graph()`: add a node (a tool call, a lookup in your
  CRM), route to it from `answer` by setting `state.route`, and add an edge back. Nodes that
  stream use `get_stream_writer()` to send delta events. `tests/unit/test_langgraph.py` adds a
  node in a few lines.

Keep the rule that exactly one terminal event ends a stream (ADR 014): set `state.response`
through `_finish` and let `terminal()` build the event.

## Add spans, metrics or a tracing backend

`chatbot.observability.tracing` has `span`, `count` and `observe`; they are no-ops until a
backend is on, so call them anywhere (see [observability.md](observability.md)). To send spans
to another place, add an exporter in `build_otel` or, simpler, point
`OTEL_EXPORTER_OTLP_ENDPOINT` at a Collector and configure it there.

## Add a vector store

Subclass `chatbot.stores.catalog.CatalogStore`: it keeps aliases, documents, calibrations,
re-indexing and rollback in Postgres, so a store only adds its vector parts:
`_create_physical` and `_drop_physical` (a collection or index per physical index),
`replace_document`, `delete_document` and `search`. Set `backend = "mystore"`, register a
factory with `@registry.register("vector_store", "mystore")`, and select it with
`vector_store.backend: mystore`. `stores/qdrant.py` is the worked example. Optional
capabilities: `keyword_search` (BM25 and hybrid retrieval) and `forget_user_documents` (when
vectors carry user data outside Postgres). Move an existing index into it with
`chatbot reindex`.

Filters must mean the same in every store: apply metadata keys the way
`chatbot.core.interfaces.metadata_matches` does (a scalar matches the value or any element of a
list; a list needs every element), and add your store to
`tests/integration/test_store_filters.py`, which runs the same cases against each store.

## Change prompts

Copy `prompts/answer/v2.yaml` to `v3.yaml`, edit, and set `answer.prompt: answer/v3`. Keep the
`[n]` citation instruction and the abstain token unless you also change
`orchestration/citations.py` and the orchestrator. `tests/unit/test_registry_prompts.py` renders
every prompt, so a missing variable fails in CI.

## Add a job kind

Add a handler in `ingestion/jobs/handlers.py` and enqueue it with `queue.enqueue(kind, payload)`.
Include `user_id` in the payload when the job holds user data so `forget_user` deletes it. The
worker gives every job its own trace (`job:<kind>`) and usage record, attributed to the
payload's `user_id`. A failed job is retried unless `chatbot.core.errors.is_permanent(exc)` says
another attempt would fail the same way (4xx statuses other than 408/409/425/429, or a
`ChatbotError` with `permanent = True`); raise your errors `from` the HTTP error, or set
`permanent`, so the worker can tell. A custom `JobQueue` needs `fail(job_id, error, retry=True)`.
