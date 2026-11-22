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
def make(embedder, store, alias):
    return MyRetriever(embedder, store, alias)  # needs .name, .retrieve() and .index()
```

Set `retrieval.strategy: dense_mmr`. Return `(hits, usage)` with `score` on a stable scale and
recalibrate `min_score` for it (ADR 011).

## Add a guard

Guards have `name`, `stage` (`input`, `context` or `output`) and `async check(payload)`
returning `allow`, `modify` (with new text) or `block`. Add yours to
`GuardPipeline.from_config` in `guardrails/pipeline.py`. For example, Microsoft Presidio for
names and addresses:

```python
from chatbot.core.types import GuardResult
from presidio_analyzer import AnalyzerEngine
from presidio_anonymizer import AnonymizerEngine


class PresidioGuard:
    name, stage = "presidio", "input"

    def __init__(self):
        self.analyzer, self.anonymizer = AnalyzerEngine(), AnonymizerEngine()

    async def check(self, payload):
        found = self.analyzer.analyze(text=payload.text, language="en")
        if not found:
            return GuardResult(action="allow", text=payload.text)
        text = self.anonymizer.anonymize(text=payload.text, analyzer_results=found).text
        return GuardResult(
            action="modify", text=text, reasons=[f"pii:{r.entity_type}" for r in found]
        )
```

## Add an LLM or embedding provider

Implement `generate()` and `stream()` (or `embed_documents()` / `embed_query()` with `model_id`
and `dims`), register it with `@registry.register("llm", "myprovider")`, and add a profile to
`config/models.yaml` with `provider: myprovider`. Any OpenAI-compatible server (vLLM, LM Studio,
Azure OpenAI with a compatible endpoint) works with `provider: openai` and a `base_url`.

## Change prompts

Copy `prompts/answer/v1.yaml` to `v2.yaml`, edit, and set `answer.prompt: answer/v2`. Keep the
`[n]` citation instruction and the abstain token unless you also change
`orchestration/citations.py` and the orchestrator. `tests/unit/test_registry_prompts.py` renders
every prompt, so a missing variable fails in CI.

## Add a job kind

Add a handler in `ingestion/jobs/handlers.py` and enqueue it with `queue.enqueue(kind, payload)`.
Include `user_id` in the payload when the job holds user data so `forget_user` deletes it.
