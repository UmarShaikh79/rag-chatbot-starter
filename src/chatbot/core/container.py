"""Wiring: turns Settings into concrete components through the registry.

This is the one place that knows which implementation backs each Protocol. To swap one,
register your own factory under the same name in ``chatbot.domain`` (imported last).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any

from psycopg_pool import AsyncConnectionPool

from chatbot.core.interfaces import (
    LLM,
    Chunker,
    Embedder,
    JobQueue,
    MemoryStore,
    Retriever,
    VectorStore,
)
from chatbot.core.prompts import PromptRegistry
from chatbot.core.registry import registry
from chatbot.core.settings import Settings
from chatbot.guardrails.pipeline import GuardPipeline
from chatbot.observability.instrument import MeteredEmbedder, MeteredLLM, MeteredOrchestrator

BUILTIN_MODULES = [
    "chatbot.providers.llm.openai",
    "chatbot.providers.llm.fake",
    "chatbot.providers.llm.ollama",
    "chatbot.providers.embeddings.openai",
    "chatbot.providers.embeddings.hashing",
    "chatbot.providers.embeddings.ollama",
    "chatbot.stores.pgvector",
    "chatbot.stores.qdrant",  # imports qdrant-client only when the store is built
    "chatbot.ingestion.sources.files",
    "chatbot.ingestion.sources.sql",
    "chatbot.ingestion.parsers",
    "chatbot.ingestion.chunkers.recursive",
    "chatbot.retrieval.dense",
    "chatbot.retrieval.bm25",
    "chatbot.providers.rerankers.lexical",
    "chatbot.providers.rerankers.cohere",
    "chatbot.providers.rerankers.cross_encoder",  # imports sentence-transformers lazily
    "chatbot.guardrails.groundedness",
    "chatbot.guardrails.pii",
    "chatbot.guardrails.injection",
    "chatbot.orchestration.simple",
    "chatbot.orchestration.langgraph",  # imports langgraph only when it is selected
    "chatbot.domain",  # last: your overrides win
]


def load_modules() -> None:
    for name in BUILTIN_MODULES:
        importlib.import_module(name)


def make_llm(settings: Settings, role: str) -> LLM:
    profile = settings.llm_profile(role)
    return registry.create("llm", profile.provider, profile)


def make_embedder(settings: Settings) -> Embedder:
    profile = settings.embed_profile()
    return registry.create("embedder", profile.provider, profile)


def make_chunker(settings: Settings) -> Chunker:
    cfg = settings.ingestion.chunker
    return registry.create("chunker", cfg.type, cfg)


def metered_llm(settings: Settings, role: str, llm: LLM) -> LLM:
    """Tracing, metrics and cost accounting around any LLM (ADR 026, ADR 028)."""
    if isinstance(llm, MeteredLLM):
        return llm
    profile = settings.llm_profile(role)
    return MeteredLLM(llm, role, profile.provider, profile.price_per_mtok)  # type: ignore[return-value]


def metered_embedder(settings: Settings, embedder: Embedder) -> Embedder:
    if isinstance(embedder, MeteredEmbedder):
        return embedder
    profile = settings.embed_profile()
    return MeteredEmbedder(embedder, profile.provider, profile.price_per_mtok)  # type: ignore[return-value]


def make_source(settings: Settings, name: str) -> Any:
    cfg = settings.source(name)
    return registry.create("source", cfg.type, cfg, settings.root_dir)


@dataclass
class Components:
    settings: Settings
    pool: AsyncConnectionPool | None
    answer_llm: LLM
    cheap_llm: LLM
    embedder: Embedder
    chunker: Chunker
    store: VectorStore
    retriever: Retriever
    memory: MemoryStore
    queue: JobQueue
    guards: GuardPipeline
    prompts: PromptRegistry
    orchestrator: Any
    pipeline: Any
    memory_manager: Any = None
    usage_store: Any = None  # where usage records go (ADR 028); None: not stored
    extras: dict[str, Any] = field(default_factory=dict)

    async def close(self) -> None:
        closer = getattr(self.store, "close", None)  # e.g. the Qdrant client
        if closer is not None:
            await closer()
        if self.pool is not None:
            await self.pool.close()


def build(
    settings: Settings,
    pool: AsyncConnectionPool | None = None,
    **overrides: Any,
) -> Components:
    """Build every component. Keyword overrides replace a part (tests pass fakes here)."""
    load_modules()
    from chatbot.ingestion.jobs.queue import PostgresJobQueue
    from chatbot.ingestion.pipeline import IngestionPipeline
    from chatbot.memory.manager import MemoryManager
    from chatbot.memory.postgres import PostgresMemoryStore
    from chatbot.observability.accounting import PostgresUsageStore
    from chatbot.retrieval.pipeline import build_pipeline
    from chatbot.retrieval.rewrite import QueryRewriter

    def part(name: str, factory: Any) -> Any:
        """The part called ``name``: the override passed to ``build`` if there is one, else
        whatever ``factory()`` builds.

        Each factory below is a ``lambda`` on purpose: it delays building the real part until
        it is needed, so a test that passes ``memory=InMemoryMemoryStore()`` never creates the
        Postgres one, and an offline run never builds an OpenAI client. Read
        ``part("memory", lambda: PostgresMemoryStore(pool))`` as "the memory store: the one
        given, or a new Postgres one".
        """
        if name in overrides:
            return overrides[name]
        return factory()

    alias = settings.vector_store.index
    # Every model call is wrapped once here: a span, token and cost metrics, and an entry in
    # the usage ledger. Providers stay plain adapters.
    answer_llm = metered_llm(
        settings, "answer", part("answer_llm", lambda: make_llm(settings, "answer"))
    )
    cheap_llm = metered_llm(
        settings, "cheap", part("cheap_llm", lambda: make_llm(settings, "cheap"))
    )
    embedder = metered_embedder(settings, part("embedder", lambda: make_embedder(settings)))
    chunker = part("chunker", lambda: make_chunker(settings))
    store = part(
        "store",
        lambda: registry.create("vector_store", settings.vector_store.backend, settings, pool),
    )
    memory = part("memory", lambda: PostgresMemoryStore(pool))
    queue = part("queue", lambda: PostgresJobQueue(pool, settings.ingestion.worker.lock_timeout_s))
    prompts = part("prompts", lambda: PromptRegistry(settings.root_dir / "prompts"))
    llms = {"answer": answer_llm, "cheap": cheap_llm}
    retriever = part(
        "retriever",
        lambda: build_pipeline(settings, embedder, store, alias, llms, prompts),
    )
    rewriter = part(
        "rewriter",
        lambda: (
            QueryRewriter(
                settings.retrieval.query_rewrite,
                llms[settings.retrieval.query_rewrite.model_role],
                prompts,
            )
            if settings.retrieval.query_rewrite.enabled
            else None
        ),
    )
    gcfg = settings.guardrails.groundedness
    judge = part(
        "judge",
        lambda: registry.create("groundedness_judge", gcfg.judge, gcfg, llms, prompts),
    )
    guards = part(
        "guards",
        lambda: GuardPipeline.from_config(settings.guardrails, llms, prompts),
    )
    memory_manager = part(
        "memory_manager",
        lambda: MemoryManager(settings.memory, memory, cheap_llm, prompts, guards, queue),
    )
    usage_store = part(
        "usage_store",
        lambda: (
            PostgresUsageStore(pool)
            if pool is not None and settings.observability.usage.persist
            else None
        ),
    )
    # orchestrator: simple (default) | langgraph | any you register. Whichever it is, one
    # wrapper gives every turn a root span, turn metrics and a usage record.
    orchestrator = part(
        "orchestrator",
        lambda: MeteredOrchestrator(
            registry.create(
                "orchestrator",
                settings.orchestrator,
                settings,
                answer_llm=answer_llm,
                cheap_llm=cheap_llm,
                retriever=retriever,
                memory=memory,
                guards=guards,
                prompts=prompts,
                memory_manager=memory_manager,
                rewriter=rewriter,
                judge=judge,
            ),
            settings.orchestrator,
            usage_store,
        ),
    )
    pipeline = part(
        "pipeline",
        lambda: IngestionPipeline(
            store, embedder, chunker, alias, settings.ingestion.embed_batch_size
        ),
    )
    return Components(
        settings=settings,
        pool=pool,
        answer_llm=answer_llm,
        cheap_llm=cheap_llm,
        embedder=embedder,
        chunker=chunker,
        store=store,
        retriever=retriever,
        memory=memory,
        queue=queue,
        guards=guards,
        prompts=prompts,
        orchestrator=orchestrator,
        pipeline=pipeline,
        memory_manager=memory_manager,
        usage_store=usage_store,
        extras={"rewriter": rewriter, "judge": judge},
    )
