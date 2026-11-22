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

BUILTIN_MODULES = [
    "chatbot.providers.llm.openai",
    "chatbot.providers.llm.fake",
    "chatbot.providers.embeddings.openai",
    "chatbot.providers.embeddings.hashing",
    "chatbot.stores.pgvector",
    "chatbot.ingestion.sources.files",
    "chatbot.ingestion.parsers",
    "chatbot.ingestion.chunkers.recursive",
    "chatbot.retrieval.dense",
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
    extras: dict[str, Any] = field(default_factory=dict)

    async def close(self) -> None:
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
    from chatbot.memory.postgres import PostgresMemoryStore
    from chatbot.orchestration.simple import SimpleOrchestrator

    def part(name: str, factory: Any) -> Any:
        return overrides[name] if name in overrides else factory()

    alias = settings.vector_store.index
    answer_llm = part("answer_llm", lambda: make_llm(settings, "answer"))
    cheap_llm = part("cheap_llm", lambda: make_llm(settings, "cheap"))
    embedder = part("embedder", lambda: make_embedder(settings))
    chunker = part("chunker", lambda: make_chunker(settings))
    store = part(
        "store",
        lambda: registry.create("vector_store", settings.vector_store.backend, settings, pool),
    )
    retriever = part(
        "retriever",
        lambda: registry.create("retriever", settings.retrieval.strategy, embedder, store, alias),
    )
    memory = part("memory", lambda: PostgresMemoryStore(pool))
    queue = part("queue", lambda: PostgresJobQueue(pool, settings.ingestion.worker.lock_timeout_s))
    guards = part("guards", lambda: GuardPipeline.from_config(settings.guardrails))
    prompts = part("prompts", lambda: PromptRegistry(settings.root_dir / "prompts"))
    orchestrator = part(
        "orchestrator",
        lambda: SimpleOrchestrator(
            settings, answer_llm, cheap_llm, retriever, memory, guards, prompts
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
    )
