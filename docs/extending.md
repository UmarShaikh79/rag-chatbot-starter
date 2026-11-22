# Extending the template

Every swappable part is a `typing.Protocol` in `src/chatbot/core/interfaces.py`. Implementations
register a factory under a name; config picks the name; `core/container.py` builds the parts.
Put your code in `src/chatbot/domain/` (imported last, so your registrations override built-ins)
or edit the built-ins directly: it is your copy.

## Add a source (for example, rows from your database)

```python
# src/chatbot/domain/crm_source.py
from collections.abc import AsyncIterator
import psycopg
from chatbot.core.registry import registry
from chatbot.core.types import RawDocument


class CrmArticles:
    def __init__(self, name: str, dsn: str):
        self.name, self.dsn = name, dsn

    async def iter_documents(self) -> AsyncIterator[RawDocument]:
        async with await psycopg.AsyncConnection.connect(self.dsn) as conn:
            cur = await conn.execute("select id, title, body from articles where published")
            async for id_, title, body in cur:
                yield RawDocument(
                    id=f"{self.name}:{id_}",
                    source=self.name,
                    uri=f"crm://articles/{id_}",
                    title=title,
                    media_type="text/markdown",
                    content=body.encode(),
                )


@registry.register("source", "crm")
def make(cfg, root_dir):
    import os

    return CrmArticles(cfg.name, os.environ[cfg.options["dsn_env"]])
```

Import it from `src/chatbot/domain/__init__.py`, then configure it:

```yaml
ingestion:
  sources:
    - {name: samples, type: files, path: samples/docs}
    - {name: crm, type: crm, options: {dsn_env: CRM_DATABASE_URL}}
```

`chatbot ingest --source crm --wait` syncs it. Only changed rows are re-embedded and rows that
disappeared are removed. If your app would rather push records, use `POST /v1/ingest` with
`documents` instead and skip the source class.

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
`config/models.yaml` with `provider: myprovider`. Any OpenAI-compatible server (vLLM, LM Studio,
Azure OpenAI with a compatible endpoint) works with `provider: openai` and a `base_url`.

## Change prompts

Copy `prompts/answer/v2.yaml` to `v3.yaml`, edit, and set `answer.prompt: answer/v3`. Keep the
`[n]` citation instruction and the abstain token unless you also change
`orchestration/citations.py` and the orchestrator. `tests/unit/test_registry_prompts.py` renders
every prompt, so a missing variable fails in CI.

## Add a job kind

Add a handler in `ingestion/jobs/handlers.py` and enqueue it with `queue.enqueue(kind, payload)`.
Include `user_id` in the payload when the job holds user data so `forget_user` deletes it.
