# Re-indexing and aliases

How to change the embedding model, the chunker or the vector store of documents already
indexed, without downtime: build a new [index](../glossary.md#index) beside the live one, check
it, then switch the [alias](../glossary.md#alias) in one step, with a one-command rollback.

This guide explains the idea and the code. The step-by-step runbook, with what can go wrong,
is [operations: re-indexing](../operations/reindexing.md).

Back to the [docs home](../README.md) · See also: [architecture, step 5](../architecture.md#5-one-document-from-file-to-vector)

## At a glance

| | |
| --- | --- |
| **What it does** | Builds the index for a new configuration behind `<alias>_next` while the API keeps answering from the old one; then switches, rolls back, or retires aliases and old indexes. |
| **Run with** | `chatbot reindex`, `chatbot index list \| activate \| rollback \| unalias \| drop` (or `make reindex`, `make index-*`) |
| **Main files** | [`ingestion/reindex.py`](../../src/chatbot/ingestion/reindex.py) (`reindex`), [`stores/catalog.py`](../../src/chatbot/stores/catalog.py) (`CatalogStore`: aliases, `activate`, `rollback`, `remove_alias`, `drop_index`), the `IndexAdmin` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | [Sources and parsers](sources-and-parsers.md) and [chunking](chunking.md) (to rebuild documents), the vector store, Postgres (the catalog) |
| **Decisions** | ADR [005](../adr/005-the-embedding-model-is-part-of-the-index.md) (the model is part of the index), ADR [023](../adr/023-reindex-beside-the-live-index-then-switch.md) (build beside, then switch), ADR [021](../adr/021-calibration-per-index-and-scorer.md) (thresholds per index), ADR [024](../adr/024-qdrant-behind-the-store-interface-with-a-postgres-catalog.md) (Postgres catalog for Qdrant too) |

## What it does

Vectors from two embedding models live in different spaces and can't be compared: a question
embedded with model B can't be searched against passages embedded with model A. So an index
belongs to **one embedding model, one chunker version and one store**, and the code refuses to
mix them (an API search on a mismatched index answers 409 and names the fix).

To move anyway, without the bot going dark:

- Code and config never name a physical index; they name an **alias** (`docs`, or
  `docs_offline` and `docs_local` in those profiles). The alias points at one physical index,
  for example `docs__text_embedding_3_small__1536`.
- `chatbot reindex` (run with the **new** configuration) creates a second physical index and
  points a *staging alias*, `docs_next`, at it. It fills it from your sources while the API,
  still on the old configuration, keeps reading `docs`.
- When it's ready and checked, `chatbot index activate` moves `docs` to the new index in one
  transaction and remembers the old one. `chatbot index rollback` moves it back.

**When do you need it?**

| You changed | Re-index? |
| --- | --- |
| The embedding model (`models.embed`, or its model, dims or prefixes) | **Yes**: the old index is refused |
| The vector store (`vector_store.backend`) | **Yes** |
| HNSW build settings, the text-search language | Yes, to apply them |
| The chunker settings | Optional: each document is re-chunked in place on its next sync ([chunking](chunking.md#turn-it-on-change-it-turn-it-off)); re-index to do it all at once |
| Chat models, prompts, retrieval strategy, rerank, thresholds | No: [recalibrate](../glossary.md#calibration) instead |

## How it works

[`reindex(store, pipeline, sources, alias, ...)`](../../src/chatbot/ingestion/reindex.py), called
by `chatbot reindex`:

1. **Create or resume** the new index behind `<alias>_next` (`create_index`). A previous
   unfinished build with the same settings is resumed; one with different settings is refused
   (`--restart` starts over).
2. **Fill it** (`_fill`): every configured source is synced into the new index (the normal
   [sync](sources-and-parsers.md#how-it-works)), and every other live document (pushed through
   the API) is rebuilt from the text the live index kept (`iter_documents` → `ingest_parsed`).
3. **Catch up:** the same pass runs once more, cheaply (unchanged documents are skipped by
   content hash), to pick up changes made during the first.
4. **Mark it ready** (`mark_ready`) with the build report, unless some document could not be
   rebuilt (then it stays `building`, and `activate` refuses it without `--force`).
5. **Optional switch:** `--activate` calls `activate` right away; normally you check first.

Then, from the CLI ([`cli.py`](../../src/chatbot/cli.py) `_index_admin`, calling
[`CatalogStore`](../../src/chatbot/stores/catalog.py)):

| Command | Store method | Does |
| --- | --- | --- |
| `chatbot index list` | `list_indexes` | every physical index: model, dims, chunker, store, status, which aliases point at it, which use it as their rollback target |
| `chatbot index activate [--force]` | `activate(alias, alias_next)` | in one transaction: point `alias` at the staged index, keep the old one as `previous`, delete `alias_next` |
| `chatbot index rollback` | `rollback(alias)` | swap `alias` and its `previous` index |
| `chatbot index unalias NAME [--force]` | `remove_alias(name)` | delete an alias (a test index, a retired corpus); its indexes stay. The configured alias is refused without `--force` |
| `chatbot index drop NAME` | `drop_index(name)` | delete a physical index, its documents and vectors; refused while an alias points at it |

Each switch takes a Postgres advisory lock on the alias, so two deploys can't race. The
catalog (aliases, documents, calibrations) is in Postgres for both pgvector and Qdrant, so the
same commands work for both stores.

## Inputs and outputs

**In:** the new configuration (environment variables or a profile) and the configured sources.

**Out:** a `ReindexReport` (printed as JSON): `alias` and `staging` (`docs_next`), `old_index`
and `new_index`, the new index's `backend`, `embedding_model`, `dims` and `chunker_version`,
`resumed`, one entry in `passes` per pass (an `IngestReport` per source plus one for documents
rebuilt from stored text, labelled `(stored)`), `missing` (documents that couldn't be
rebuilt), and whether the index is `ready` and was `activated`. The exit code is 1 when documents are missing and the index
isn't ready.

## Turn it on, change it, turn it off

The short version (the [runbook](../operations/reindexing.md#the-workflow) has every detail):

```bash
# with the NEW configuration (e.g. CHATBOT__MODELS__EMBED=ollama:nomic-embed-text)
chatbot reindex                                            # build docs_next beside docs
CHATBOT__VECTOR_STORE__INDEX=docs_next chatbot calibrate --apply
CHATBOT__VECTOR_STORE__INDEX=docs_next chatbot eval --gate # check before anyone sees it
chatbot index activate                                     # switch; restart API and workers with the new config
chatbot index rollback                                     # if something is wrong
chatbot index drop <old index name>                        # later, to free the space
```

Small deployment, nothing to check: `chatbot reindex --activate` builds and switches in one go.
With Docker: `make reindex`, `make index-list`, `make index-activate`, `make index-rollback`.

**Settings:** `vector_store.index` is the alias the app serves. Point it at the staging alias
(`CHATBOT__VECTOR_STORE__INDEX=docs_next`) to calibrate or evaluate the new index before
switching. See [configuration: `vector_store`](../configuration.md#vector_store).

**Turning it off:** there's nothing running; it only acts when you run the commands. If you
never change models or stores, you never need it.

## Extend it

Re-indexing works with any store that implements the `IndexAdmin` Protocol (`create_index`,
`mark_ready`, `activate`, `rollback`, `remove_alias`, `drop_index`, `list_indexes`,
`resolve_any`, `iter_documents`). Both built-in stores get it by subclassing `CatalogStore`,
which keeps the catalog in Postgres and leaves only the vectors to the store
(`_create_physical` and `_drop_physical`). A new store (Weaviate, Milvus, ...) that subclasses
`CatalogStore` gets re-indexing, aliases and rollback for free. See
[extending: add a vector store](../extending.md#add-a-vector-store).

## Test it

```bash
make test-db     # tests/integration/test_reindex.py, tests/e2e/test_cli_reindex.py, and the store tests
```

| Test file | Covers |
| --- | --- |
| [`test_reindex.py`](../../tests/integration/test_reindex.py) | a new embedder builds beside the live index, then switches and rolls back; chunker-only changes; resume and catch-up; documents without stored text block the switch; ingesting into the staging alias during a build |
| [`test_cli_reindex.py`](../../tests/e2e/test_cli_reindex.py) | the CLI commands end to end |
| [`test_pgvector.py`](../../tests/integration/test_pgvector.py) | the index refuses another model; `unalias` keeps the index |
| [`test_settings.py`](../../tests/unit/test_settings.py) | `chatbot index unalias` refuses the configured alias without `--force` |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| API answers 409 "Index 'docs' was built with ..." | The configuration names a different embedding model than the index | Re-index (above), or switch `models.embed` back. |
| 409 right after `activate` | Processes still running the old configuration | Restart the API and workers with the new one, or `chatbot index rollback`. |
| `activate` says the index is still building | Some documents couldn't be rebuilt | See [runbook: what can go wrong](../operations/reindexing.md#what-can-go-wrong); `--allow-missing` or `--force` if you accept it. |
| `'docs_next' already points at ... which does not match` | An older build with other settings | `chatbot reindex --restart`, then `chatbot index drop` the abandoned one. |
| `drop` refuses: "in use by alias" | An alias still points at it | Activate or roll back to another index first, or `chatbot index unalias` the alias. |
| Answers got worse after switching | Thresholds belong to the index and weren't recalibrated | `chatbot calibrate --apply` on the new index; or roll back. |

## Related

- The runbook, including moving between pgvector and Qdrant:
  [operations: re-indexing](../operations/reindexing.md).
- How documents are rebuilt: [sources and parsers](sources-and-parsers.md),
  [chunking](chunking.md).
- Vacuum after dropping an index: [operating pgvector](../operations/pgvector.md).
- Terms: [index](../glossary.md#index), [alias](../glossary.md#alias),
  [calibration](../glossary.md#calibration), [scorer](../glossary.md#scorer).
