# ADR 005: The embedding model is part of the index

Status: Accepted

## Decision

Each physical index records its embedding model and dimensions (`vector_indexes`). The configured name (`vector_store.index`) is an alias that points at one physical index. Queries refuse to run when the configured embedder differs from the index's model.

## Alternatives considered

One shared table, re-embedding in place.

## Why

Mixing vectors from two models silently returns garbage.

## Consequences

Changing the embedding model means ingesting into a new alias (milestone 1) or building a new index and flipping the alias (milestone 4). Storage is doubled briefly during a migration.
