# ADR 012: Filtered vector search on pgvector

Status: Accepted

## Decision

pgvector 0.8 or newer. Each physical index has a partial HNSW index on `(embedding::vector(dims)) WHERE index_id = N`. Each query sets `hnsw.ef_search`, `hnsw.iterative_scan = relaxed_order` and `hnsw.max_scan_tuples`, then re-sorts hits by score. Filters that match at most `exact_scan_threshold` chunks run an exact scan instead. An integration test proves a selective filter still returns `top_k` rows (and that it does not with iterative scan off).

## Alternatives considered

Post-filtering in Python; a separate vector database.

## Why

HNSW filters after the graph search, so selective filters can return too few rows.

## Consequences

Deletes leave dead tuples. The operations guide, [docs/operations/pgvector.md](../operations/pgvector.md), covers autovacuum for `chunks`, `REINDEX INDEX CONCURRENTLY` after heavy churn, and tuning these settings.
