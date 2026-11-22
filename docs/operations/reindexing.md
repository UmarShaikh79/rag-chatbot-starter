# Re-indexing: changing the embedding model, chunker or store

An index is built for one embedding model, one chunker version and one vector store
(ADR 005). Change any of them and the existing vectors no longer fit: the API refuses to search
an index built with a different model (HTTP 409 naming the fix), and ingestion refuses to
write to it. `chatbot reindex` builds the replacement beside the live index, so the API keeps
answering from the old one until you switch (ADR 023).

| You changed | Re-index? |
| --- | --- |
| `models.embed`, or an embedding profile's model, dims or prefixes | Yes |
| `ingestion.chunker.*` | Yes (or let the next sync re-chunk in place, ADR 013) |
| `vector_store.backend` (pgvector to Qdrant or back) | Yes |
| `vector_store.hnsw.m` / `ef_construction`, `text_search_config` | Yes, to apply them |
| Chat models, prompts, retrieval strategy, rerank, thresholds | No (recalibrate instead) |

## The workflow

Run the commands with the **new** configuration (a new `models.yaml` entry, a profile, or
`CHATBOT__...` variables), from a machine or container that can reach the database and the
new embedding model. The running API and workers keep the old configuration meanwhile.

```bash
# 1. Build. Re-reads every configured source; rebuilds API-pushed documents from the text
#    the index kept; runs a second, cheap pass for changes made meanwhile.
chatbot reindex
#    -> builds e.g. docs__nomic_embed_text__768__20261122093000 behind alias "docs_next"

# 2. Check it before anyone sees it: thresholds belong to the index (ADR 021).
CHATBOT__VECTOR_STORE__INDEX=docs_next chatbot calibrate --apply
CHATBOT__VECTOR_STORE__INDEX=docs_next chatbot eval --gate

# 3. Switch, as part of the deploy that rolls out the new configuration.
chatbot index activate
#    restart the API and workers with the new configuration

# Something wrong? Point the alias back (and redeploy the old configuration).
chatbot index rollback

# 4. Later, free the space (see operations/pgvector.md for vacuum afterwards).
chatbot index list
chatbot index drop docs__text_embedding_3_small__1536

# Retire an alias you no longer serve (a test index, an old corpus); its indexes stay, then
# drop them. The alias in vector_store.index is refused without --force.
chatbot index unalias docs_test
```

With Docker: `make reindex`, `make index-list`, `make index-activate`, `make index-rollback`
(they run inside the `api` container, so set the new configuration in `.env` and recreate the
containers first, or use `docker compose run` with the variables).

Small deployment, nothing to check: `chatbot reindex --activate` builds and switches in one go.

## What can go wrong

- **"documents ... could not be rebuilt"**: documents pushed through the API before this
  template version (migration 0004) did not keep their text, and no configured source has
  them. Push them again with `CHATBOT__VECTOR_STORE__INDEX=docs_next` (they land in the new
  index only) and rerun `chatbot reindex`, or pass `--allow-missing` to leave them out. Until
  then the build stays in status `building` and `activate` refuses it (`--force` overrides).
- **The build stopped half way** (crash, deploy): rerun `chatbot reindex`. It resumes the
  build behind `docs_next`; documents already done are skipped by content hash.
- **`docs_next` was built with other settings**: `chatbot reindex` refuses to mix them. Pass
  `--restart` for a fresh build and drop the abandoned one with `chatbot index drop`.
- **409 errors right after activating**: processes still on the old configuration. Restart
  them with the new one; or roll back.
- **Writes during the build**: each pass copies what changed in the live index, but a change
  made after the second pass started is not copied. If the live index is busy, rerun
  `chatbot reindex` just before `activate`; it is quick when little changed.

## Moving between pgvector and Qdrant

The catalog (aliases, documents, calibrations) stays in Postgres for both stores (ADR 024), so
moving is a re-index with `vector_store.backend` changed:

```bash
uv sync --extra qdrant
CHATBOT__VECTOR_STORE__BACKEND=qdrant chatbot reindex --activate
```

BM25 and hybrid retrieval need pgvector's `keyword_search`; set `retrieval.strategy: dense`
when moving to Qdrant.
