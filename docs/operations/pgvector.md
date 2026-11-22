# Operating pgvector

How the default store behaves under load and change, and what to watch. Everything here is
plain Postgres and pgvector; nothing in the template needs to change to apply it. The SQL is a
starting point: measure on your data before and after.

Requires pgvector 0.8 or newer (`hnsw.iterative_scan`). The Compose file and CI use
`pgvector/pgvector:0.8.1-pg17`. Check yours with
`SELECT extversion FROM pg_extension WHERE extname = 'vector';`.

## How the template lays out vectors

- One `chunks` table holds every physical index. The `embedding` column is an untyped
  `vector`, so indexes with different dimensions share the table.
- Each physical index gets a **partial** HNSW index, `chunks_hnsw_<index id>`, on
  `(embedding::vector(<dims>)) WHERE index_id = <id>`. Queries use the same expression and
  predicate, so each index has its own graph and dropping an index drops only its graph.
- `chunks.tsv` (GIN) serves BM25 (ADR 020); `chunks.metadata` (GIN, `jsonb_path_ops`) serves
  metadata filters.
- Re-ingesting a document deletes all its chunks and inserts new ones in one transaction
  (ADR 013). Every edit is therefore delete plus insert: this is the churn the rest of this
  page is about.

## Query settings (ADR 012)

Set per query by the store, from `vector_store.hnsw` in `config/app.yaml`:

| Setting | Default | Raise it when |
| --- | --- | --- |
| `ef_search` | 100 | Recall is low on large indexes (check hit@k in `chatbot eval`). Cost grows roughly linearly. |
| `iterative_scan` | `relaxed_order` | Keep it. `off` lets selective filters return fewer than `top_k` rows. |
| `max_scan_tuples` | 20000 | Filtered queries on a large index still come back short. |
| `exact_scan_threshold` | 2000 | Filters that match at most this many chunks skip HNSW and scan exactly; exact is cheap at that size and always right. |

pgvector also has `hnsw.scan_mem_multiplier` (default 1) for iterative scans that run out of
`work_mem`; set it with `ALTER DATABASE ... SET` if logs show the scan stopping early.

## Building indexes

The store creates a physical index's HNSW graph when the index is created, then pgvector
inserts into it row by row. That is right for steady ingestion but slow for a first load or a
large re-index. Two settings help either way:

```sql
-- For the session (or role) that runs ingestion or the re-index:
SET maintenance_work_mem = '2GB';           -- the graph should fit; pgvector warns when it does not
SET max_parallel_maintenance_workers = 4;   -- parallel HNSW builds (pgvector >= 0.6)
```

For a large `chatbot reindex`, building the graph after the data is loaded is much faster:

```sql
-- 1. After `chatbot reindex` has created the staging index, before it fills (or stop it,
--    then resume): find its id and drop its graph.
SELECT v.id, v.name FROM index_aliases a JOIN vector_indexes v ON v.id = a.index_id
WHERE a.alias = 'docs_next';
DROP INDEX IF EXISTS chunks_hnsw_<id>;

-- 2. Let `chatbot reindex` finish (searches on docs_next scan exactly meanwhile).

-- 3. Build the graph once, without blocking writes. Same expression and predicate as the
--    store's own (see PgVectorStore._create_physical), or queries will not use it.
CREATE INDEX CONCURRENTLY chunks_hnsw_<id> ON chunks
  USING hnsw ((embedding::vector(<dims>)) vector_cosine_ops)
  WITH (m = 16, ef_construction = 64)
  WHERE index_id = <id>;
```

Watch a build with
`SELECT phase, blocks_done, blocks_total FROM pg_stat_progress_create_index;`.

## Churn, vacuum and HNSW

Deleted chunks stay in the table and in the HNSW graph as dead tuples until vacuum removes
them. Until then they cost scan time, and an iterative scan may walk through many of them
before it finds `top_k` live rows. Vacuuming an HNSW index is also expensive, because pgvector
repairs the graph around every removed node.

Recommended settings for `chunks` when documents change often (frequent syncs, API pushes):

```sql
ALTER TABLE chunks SET (
  autovacuum_vacuum_scale_factor = 0.02,    -- vacuum after ~2% dead rows (default 20%)
  autovacuum_vacuum_threshold = 1000,
  autovacuum_analyze_scale_factor = 0.02,   -- keep planner stats fresh for filtered queries
  autovacuum_vacuum_cost_limit = 2000       -- let it finish instead of trickling
);
```

Check how far behind vacuum is:

```sql
SELECT n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1) AS dead_pct,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables WHERE relname = 'chunks';
```

After heavy churn (a re-chunk of most documents, a bulk delete, `chatbot index drop` of a large
index), rebuild the graphs first and vacuum second; pgvector's own advice, because vacuuming a
freshly built index is cheap:

```sql
REINDEX INDEX CONCURRENTLY chunks_hnsw_<id>;   -- one per live physical index
VACUUM (ANALYZE) chunks;
```

`REINDEX ... CONCURRENTLY` needs about twice the index size in free disk while it runs and
does not block reads or writes. Find the graphs and their sizes with:

```sql
SELECT indexrelid::regclass AS index, pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_index WHERE indrelid = 'chunks'::regclass AND indexrelid::regclass::text LIKE 'chunks_hnsw_%';
```

## Memory and size

An HNSW graph is fast only while it stays in memory. Rough size per chunk: the vector
(4 bytes x dims, so ~6 KB at 1536 dims, ~3 KB at 768) plus graph links (about `m` x 2 x 8
bytes). A million 1536-dim chunks is in the region of 8 GB for the graph alone; compare with
the query above and with `shared_buffers` and the machine's RAM. Options when it does not fit:
fewer dims (`text-embedding-3-small` accepts a smaller `dimensions`; that is a new model ID and
a re-index), `halfvec` storage (pgvector 0.7+, a change to the store), or Qdrant with
`vector_store.qdrant.on_disk: true` (ADR 024).

## After a re-index (ADR 023)

`chatbot index activate` keeps the old physical index for `chatbot index rollback`, so both
graphs and both sets of chunks stay on disk. When the new one has proved itself:

```bash
chatbot index list                 # the old one shows previous_of: [docs]
chatbot index drop <old name>      # deletes its chunks, documents, calibrations and graph
```

Then vacuum as above: the drop deletes the old index's rows from the shared tables.

## Backups

Everything (catalog, chunks, vectors, memory, jobs) is in one database, so a normal
`pg_dump`/`pg_basebackup` covers it. Restoring needs the same pgvector version or newer.
