# Tutorial: add your own retriever

In about 30 minutes you add a new way of finding passages, plug it in with one setting, and
measure whether it helps. Along the way you use every idea from the
[architecture](architecture.md): a Protocol, the registry, config, scores, calibration and
tests. The same steps work for any part (a scorer, a source, a guard, a model provider).

**What you'll build:** a *title-boost retriever*. It runs normal [dense
retrieval](glossary.md#dense-retrieval), then gives a small bonus to passages whose document
title shares words with the question. Titles often name the topic exactly ("Refund policy"),
so this can lift the right passage when two are close.

**You need:** the repo running (the [README quick start](../README.md#quick-start-no-api-key-needed));
the `offline` profile is fine. The code below is run by
[`tests/unit/test_tutorial.py`](../tests/unit/test_tutorial.py) on every change, so it works as
written.

Back to the [docs home](README.md).

---

## Step 1: understand what a retriever must do

A retriever is anything that matches the `Retriever` Protocol in
[`core/interfaces.py`](../src/chatbot/core/interfaces.py):

```python
class Retriever(Protocol):
    name: str

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]: ...
```

- **In:** the question, how many passages to return (`k`), and optional metadata filters.
- **Out:** up to `k` passages, best first, each a `ScoredChunk` (the chunk, its `score`, which
  retriever produced it, and a `scores` dict), plus the token usage of any model calls.

Two more attributes make it a good citizen: `signature` names how scores are produced (it
becomes part of the [calibration key](glossary.md#scorer)), and `index()` lets the pipeline
check the index exists.

## Step 2: write it

Create `src/chatbot/domain/title_boost.py`:

```python
# src/chatbot/domain/title_boost.py
from __future__ import annotations

from typing import Any

from chatbot.core.registry import registry
from chatbot.core.types import IndexInfo, ScoredChunk, Usage
from chatbot.retrieval.bm25 import tokenize
from chatbot.retrieval.dense import DenseRetriever


class TitleBoostRetriever:
    """Dense retrieval, plus a bonus when the question's words appear in a passage's title."""

    name = "title_boost"
    signature = "title_boost(dense)"  # part of the calibration key: change it, recalibrate

    def __init__(self, dense: DenseRetriever, bonus: float = 0.05) -> None:
        self.dense = dense
        self.bonus = bonus  # added to the score for each shared word

    async def index(self) -> IndexInfo | None:
        return await self.dense.index()

    async def retrieve(
        self, query: str, k: int, filters: dict[str, Any]
    ) -> tuple[list[ScoredChunk], Usage]:
        # 1. Ask the dense retriever for more candidates than we need, so a passage just
        #    outside the top k can still move up.
        hits, usage = await self.dense.retrieve(query, k * 2, filters)

        # 2. Rescore: dense similarity plus a bonus per word shared with the title.
        question_words = set(tokenize(query))
        rescored = []
        for hit in hits:
            title = str(hit.chunk.metadata.get("title", ""))
            shared = len(question_words & set(tokenize(title)))
            score = hit.score + self.bonus * shared
            rescored.append(
                hit.model_copy(
                    update={
                        "score": score,
                        "retriever": self.name,
                        # Keep the dense score; add ours under our name (the gate reads it).
                        "scores": {**hit.scores, self.name: score},
                    }
                )
            )

        # 3. Best first, and only k of them.
        rescored.sort(key=lambda hit: hit.score, reverse=True)
        return rescored[:k], usage


@registry.register("retriever", "title_boost")
def make_title_boost(embedder, store, alias, **kwargs: Any) -> TitleBoostRetriever:
    # The pipeline calls every retriever factory as (embedder, store, alias, settings=...).
    return TitleBoostRetriever(DenseRetriever(embedder, store, alias))
```

What each part does:

- `tokenize` is the same word splitter BM25 uses: lower-case words, no stopwords ("the",
  "how"), so "How long is the refund window?" becomes `{"long", "refund", "window"}`.
- `hit.model_copy(update=...)` makes a changed copy; `ScoredChunk` is a Pydantic model and the
  originals stay untouched.
- Putting our score in `scores["title_boost"]` matters: with `retrieval.gate: auto`, a
  pipeline with one retriever gates on that retriever's own score, so the
  [weak-retrieval check](glossary.md#min_score-and-min_hits) reads `scores["title_boost"]`.
- The factory is what the [registry](architecture.md#2-the-registry-turns-names-into-parts)
  stores. Its name, `title_boost`, is what config will say.

## Step 3: register it

The `@registry.register` line runs when the module is imported. Import it from
`src/chatbot/domain/__init__.py`, which the container imports after every built-in module:

```python
# src/chatbot/domain/__init__.py  (add at the end)
from chatbot.domain import title_boost  # noqa: F401
```

## Step 4: switch it on

One setting, in `.env`:

```bash
CHATBOT__RETRIEVAL__STRATEGY=title_boost
```

(or `retrieval.strategy: title_boost` in `config/app.yaml`). Then restart: `make up` rebuilds
the image with your file, or `uv run chatbot serve` without Docker. An unknown name would stop
startup with the list of known retrievers, so a typo can't go unnoticed.

Ask something and look at the scores in the response's citations:

```bash
uv run chatbot ask "How long is the refund window for annual plans?"
```

## Step 5: calibrate, then measure

A new retriever means a new score scale, so the thresholds that decide "answer or ask" must be
recomputed for it ([calibration](glossary.md#calibration)). Until you do, the API logs that
the thresholds are provisional.

```bash
make calibrate-apply     # thresholds for scorer "title_boost|title_boost(dense)", stored with the index
make eval                # retrieval and answer metrics, checked against evals/gate.yaml
```

Compare with the default: run `make eval` once with `CHATBOT__RETRIEVAL__STRATEGY=dense` and
once with `title_boost`, and look at `retrieval.mrr` (is the right document ranked higher?) and
`abstention.unanswerable_rate` (does it still decline what the documents don't cover?). If the
numbers go down, lower the bonus, or drop the idea: measuring is the point. See
[evals](../evals/README.md#metrics) for what each metric means.

## Step 6: test it

Tests use in-memory parts, so they need no database. A minimal one:

```python
# tests/unit/test_title_boost.py
from chatbot.core import container
from chatbot.core.container import make_source
from chatbot.providers.embeddings.hashing import HashingEmbedder
from chatbot.providers.llm.fake import FakeLLM
from chatbot.testing.fakes import InMemoryJobQueue, InMemoryMemoryStore, InMemoryVectorStore
from tests.conftest import offline_settings


async def test_title_boost_ranks_and_gates_on_its_own_score():
    settings = offline_settings(retrieval={"strategy": "title_boost"})
    c = container.build(
        settings,
        pool=None,
        answer_llm=FakeLLM(),
        cheap_llm=FakeLLM(),
        embedder=HashingEmbedder(384),
        store=InMemoryVectorStore(),
        memory=InMemoryMemoryStore(),
        queue=InMemoryJobQueue(),
    )
    await c.pipeline.sync_source(make_source(settings, "samples"))

    hits, _ = await c.retriever.retrieve("How long is the refund window?", 3, {})
    assert hits[0].chunk.document_id == "samples:refund-policy.md"
    assert all("title_boost" in h.scores for h in hits)
    assert c.retriever.scorer == "title_boost|title_boost(dense)"
```

Run it with `uv run pytest tests/unit/test_title_boost.py`.

## What you used

| Idea | Where it showed up |
| --- | --- |
| [Protocols](architecture.md#1-parts-are-protocols) | `TitleBoostRetriever` has `name` and `retrieve`; nothing to inherit |
| [The registry](architecture.md#2-the-registry-turns-names-into-parts) | `@registry.register("retriever", "title_boost")` |
| [`domain/`](architecture.md#8-where-your-code-goes) | your file, imported last |
| [Config](architecture.md#6-how-a-setting-reaches-the-code) | `CHATBOT__RETRIEVAL__STRATEGY=title_boost` |
| [Scores and the gate](glossary.md#gate-score) | `scores["title_boost"]` |
| [Calibration](glossary.md#calibration) and [evals](../evals/README.md) | `make calibrate-apply`, `make eval` |

## Going further

- **Combine it:** list it with BM25 in `retrieval.hybrid.retrievers: [title_boost, bm25]` and
  set `retrieval.strategy: hybrid`; [RRF](glossary.md#rrf) merges the two lists.
- **Rerank on top:** `retrieval.rerank.enabled: true` re-scores your top passages with a
  reranking model.
- **Other parts, same steps:** a document source, a parser, a guard, a model provider: see
  [extending](extending.md).
