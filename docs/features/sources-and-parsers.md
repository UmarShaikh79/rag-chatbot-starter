# Sources and parsers

How documents get into the system: where they are read from (*sources*) and how their bytes
become clean text with structure (*parsers*). The first stage of
[ingestion](../glossary.md#ingestion); [chunking](chunking.md) and embedding come next.

Back to the [docs home](../README.md) · See also: [architecture, step 5](../architecture.md#5-one-document-from-file-to-vector)

## At a glance

| | |
| --- | --- |
| **What it does** | Reads documents from folders, SQL queries or the API, parses Markdown, text, HTML, PDF and DOCX into titled sections, and keeps the index in line with the source (new, changed and removed documents). |
| **Turned on by** | `ingestion.sources` in [`config/app.yaml`](../../config/app.yaml) (one `samples` folder by default); `POST /v1/ingest` for pushed documents |
| **Run with** | `make ingest`, `chatbot ingest`, `POST /v1/ingest`, the nightly Kubernetes CronJob |
| **Main files** | [`ingestion/sources/files.py`](../../src/chatbot/ingestion/sources/files.py), [`ingestion/sources/sql.py`](../../src/chatbot/ingestion/sources/sql.py), [`ingestion/parsers/`](../../src/chatbot/ingestion/parsers/__init__.py), [`ingestion/pipeline.py`](../../src/chatbot/ingestion/pipeline.py) |
| **Depends on** | [Background jobs](background-jobs.md) (sync runs as a job), [chunking](chunking.md), an embedder, a vector store |
| **Decisions** | ADR [013](../adr/013-re-ingesting-a-document-is-an-atomic-replace.md) (atomic replace), ADR [015](../adr/015-redact-before-persisting-delete-everything-per-user.md) (per-user documents) |

## What it does

Three kinds of source, all producing the same thing, a `RawDocument` (bytes plus where they
came from):

- **Files**: every file under a folder that matches the include patterns. The sample corpus
  (`samples/docs`) is a files source.
- **SQL rows**: one row of a query becomes one document. For knowledge that lives in your
  app's database: help articles, product records, FAQ rows. Any database SQLAlchemy reaches.
- **Pushed documents**: your app sends documents to `POST /v1/ingest` (up to 500 per call),
  optionally owned by one end user.

A parser then turns the bytes into a `ParsedDocument`: a title and a list of sections, each a
heading and its text (and a page number for PDFs). Sections matter: [chunking](chunking.md)
never merges two of them, and every passage carries its section heading, so answers can cite
"Refund policy > Annual plans".

A **sync** keeps the index equal to the source: new and changed documents are (re-)indexed,
unchanged ones skipped, and documents that disappeared from the source deleted, so a removed
page can never keep answering.

## How it works

The path of one sync, from the entry point to the stored chunks:

1. **Entry:** `make ingest` / `chatbot ingest` / `POST /v1/ingest {"source": "samples"}` puts
   a `sync_source` job in the queue ([background jobs](background-jobs.md)). With
   `chatbot ingest --inline` the CLI skips the queue and calls the pipeline directly.
2. **Build the source:** [`make_source(settings, name)`](../../src/chatbot/core/container.py)
   finds `ingestion.sources[name]` and asks the [registry](../architecture.md#2-the-registry-turns-names-into-parts)
   for the factory registered under its `type` (`files`, `sql`, or yours).
3. **Read:** [`IngestionPipeline.sync_source`](../../src/chatbot/ingestion/pipeline.py) iterates
   `source.iter_documents()`, remembering every document ID it sees.
4. **Parse:** for each document, `parser_for(raw.media_type).parse(raw)`
   ([`parsers/base.py`](../../src/chatbot/ingestion/parsers/base.py)) picks the parser that
   lists the media type, and returns a `ParsedDocument` whose `content_hash` is the SHA-256 of
   the bytes.
5. **Skip or index:** `ingest_parsed` compares the stored hash, chunker version and embedding
   model; if all three match, the document is `unchanged`. Otherwise it is chunked, embedded
   and stored in one transaction (`store.replace_document`).
6. **A bad document doesn't stop the sync:** a parse error, bad encoding or unreadable file is
   recorded in the report's `failed` and the sync goes on. A provider or database error stops
   it, and the job is retried; unchanged documents are skipped on the retry.
7. **Remove what's gone:** after the loop, every stored document of this source that wasn't
   seen is deleted (`store.delete_document`).
8. **Output:** an `IngestReport`, saved as the job's result.

Pushed documents take a shorter path: `ingest_documents` job → `IngestionPipeline.ingest_inline`
→ steps 4 and 5 for each document. There is no "remove what's gone" step: pushed documents
stay until you delete them.

## Inputs and outputs

**`RawDocument`** ([`core/types.py`](../../src/chatbot/core/types.py)), what a source yields:

| Field | Files source | SQL source | Pushed (`POST /v1/ingest`) |
| --- | --- | --- | --- |
| `id` | `<source>:<relative path>`, e.g. `samples:refund-policy.md` | `<source>:<id_column value>` | `<source>:<your id>` (source label defaults to `api`) |
| `uri` | `file://<relative path>` | `uri_template`, or `sql://<source>/<id>` | your `uri`, or `<source>://<id>` |
| `title` | from the document (else the file name) | `title_column` | your `title` |
| `media_type` | from the extension | `media_type` option | `text/markdown` (default), `text/plain` or `text/html` |
| `metadata` | `{"path": ...}` | `metadata_columns` | your `metadata` |
| `user_id` | none | `user_id_column` | the request's `user_id` |

Metadata is copied to every chunk, so it can be used as a [filter](../api.md#post-v1chat) at
question time (`{"filters": {"category": "billing"}}`).

**`ParsedDocument`**: `id`, `source`, `uri`, `title`, `sections` (each `heading`, `text`,
`page`), `metadata`, `content_hash`.

**`IngestReport`** (the job result): `indexed`, `unchanged`, `removed` (document IDs), `failed`
(ID → error, credentials scrubbed), `chunks` (how many were written), `usage` (embedding
tokens and cost).

**Parsers and what they keep:**

| Media type | Parser | Sections from | Title from | Notes |
| --- | --- | --- | --- | --- |
| `text/markdown`, `text/plain` (`.md`, `.markdown`, `.txt`) | `MarkdownParser` ([`text.py`](../../src/chatbot/ingestion/parsers/text.py)) | `#` headings (not inside code fences) | the first `#` heading | Plain text has no headings: one section. |
| `text/html` (`.html`, `.htm`) | `HtmlParser` ([`html.py`](../../src/chatbot/ingestion/parsers/html.py)) | `<h1>`…`<h6>` | `<title>` | Drops `script`, `style`, `nav`, `footer`, `noscript`, `template`. |
| `application/pdf` (`.pdf`) | `PdfParser` ([`pdf.py`](../../src/chatbot/ingestion/parsers/pdf.py)) | one per page, with its page number | the PDF's metadata | Scanned PDFs have no text layer and produce nothing: add OCR in your own parser. |
| DOCX (`.docx`) | `DocxParser` ([`docx.py`](../../src/chatbot/ingestion/parsers/docx.py)) | paragraphs styled `Heading …` or `Title` | the document properties | |

Every parser normalises line endings and blank lines; with no title found, the file name is
used ("refund-policy.md" → "Refund policy").

## Turn it on, change it, turn it off

**Add a folder of files:**

```yaml
ingestion:
  sources:
    - name: handbook            # lower case, digits, - and _; becomes part of every document ID
      type: files
      path: /data/handbook      # absolute, or relative to the repository root
      include: ["*.md", "*.pdf"]
```

**Add rows from a database** (the full option list is in the docstring of
[`sources/sql.py`](../../src/chatbot/ingestion/sources/sql.py)):

```yaml
    - name: articles
      type: sql
      options:
        url_env: APP_DATABASE_URL            # the env var holding the URL; default DATABASE_URL
        query: SELECT id, title, body, category FROM help_articles WHERE published
        id_column: id
        title_column: title
        text_columns: [title, body]          # or text_template: "# {title}\n\n{body}"
        metadata_columns: [category]
```

Give the query its own read-only database user. Postgres works out of the box; MySQL, SQL
Server and others need their driver added (`uv add pymysql`).

**Push documents from your app:**

```bash
curl -s localhost:8000/v1/ingest -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"source": "crm", "user_id": "customer-42",
       "documents": [{"id": "42", "title": "Order 42", "text": "Order 42 ships Friday."}]}'
```

**Sync:** `make ingest` (all sources, waits), `chatbot ingest --source handbook --wait`, or
`POST /v1/ingest {"source": "handbook"}` and poll `GET /v1/jobs/{id}`. Add `--force` (or
`"force": true`) to re-embed even unchanged documents.

**Change:** editing a document and syncing updates only that document. Changing parsers or
include patterns takes effect on the next sync.

**Remove a source:** delete it from `ingestion.sources`. Its documents stay in the index until
removed: run one last sync with `include: []` (everything counts as gone and is deleted), or
delete documents one by one with `DELETE /v1/documents/{id}`. A user's pushed documents are
also deleted by `DELETE /v1/users/{id}/memory`.

**Remove a parser or a source type** you'll never use: delete its module, its line in
`BUILTIN_MODULES` ([`core/container.py`](../../src/chatbot/core/container.py)) and its tests.
The PDF and DOCX parsers are the only ones with dependencies of their own (`pypdf`,
`python-docx`).

All settings: [configuration: `ingestion`](../configuration.md#ingestion).

## Extend it

**A new source** (a CMS, an S3 bucket, a ticket system): a class with `name` and an async
`iter_documents()` that yields `RawDocument`s, registered under a type name:

```python
@registry.register("source", "cms")
def make_cms_source(cfg, root_dir):  # cfg: the SourceConfig entry; cfg.options is yours
    return CmsSource(cfg.name, cfg.options["base_url"])
```

Then `type: cms` in `ingestion.sources`. Give each document a stable `id`: it decides what
counts as "the same document" across syncs. The full example is in
[extending: add a source](../extending.md#add-a-source).

**A new parser** (EPUB, PowerPoint, OCR for scanned PDFs): a class with `media_types` and
`parse(raw) -> ParsedDocument`; use `build(raw, sections, title)` from
[`parsers/base.py`](../../src/chatbot/ingestion/parsers/base.py) so the hash and title rules
stay the same. Register it with `registry.add("parser", "epub", EpubParser)`, and map the file
extension to its media type in `MEDIA_TYPES` ([`sources/files.py`](../../src/chatbot/ingestion/sources/files.py)).
See [extending: add a parser](../extending.md#add-a-parser).

## Test it

```bash
uv run pytest tests/unit/test_parsers_chunker.py tests/unit/test_worker_pipeline.py tests/unit/test_sql_source.py
make test-db     # adds tests/integration/test_sql_source_pg.py and the end-to-end ingest → answer test
```

| Test file | Covers |
| --- | --- |
| [`test_parsers_chunker.py`](../../tests/unit/test_parsers_chunker.py) | each parser on the sample files, titles, sections |
| [`test_worker_pipeline.py`](../../tests/unit/test_worker_pipeline.py) | sync skips unchanged and removes deleted documents; a bad file is reported, not fatal |
| [`test_sql_source.py`](../../tests/unit/test_sql_source.py), [`test_sql_source_pg.py`](../../tests/integration/test_sql_source_pg.py) | SQL options, row → document, real Postgres |
| [`test_ingest_to_answer.py`](../../tests/e2e/test_ingest_to_answer.py) | the whole path: API, job, worker, pgvector, chat |

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| Job fails with `Source 'x': folder … does not exist` | `path` is wrong, or not mounted into the container | Use an absolute path, or one relative to the repository root; with Docker, mount the folder. |
| A file is in `failed` with `No parser registered for media type` | Its extension isn't known | Add a parser, or map the extension in `MEDIA_TYPES`. |
| A PDF is indexed with 0 chunks | Scanned PDF, no text layer | OCR it first, or write a parser that does. |
| An edited document doesn't change | The sync didn't run, or the bytes are identical | Run `make ingest`; `--force` re-embeds everything. |
| A removed page still answers | It was pushed (not synced), or its source was deleted from config | `DELETE /v1/documents/{id}`; see "Remove a source" above. |
| `ingestion.sources.0.name: String should match pattern` at startup | Name has capitals or spaces | Lower case, digits, `-` and `_`, starting with a letter. |
| SQL source: `set text_columns or text_template` | Neither option given | Name the columns that hold the text. |
| `POST /v1/ingest` returns 404 `unknown source` | The name isn't in `ingestion.sources` | Use a configured name, or send `documents` instead. |

## Related

- Next stage: [chunking](chunking.md); where syncs run: [background jobs](background-jobs.md).
- Changing the embedding model or store for documents already indexed:
  [re-indexing and aliases](reindexing.md).
- Every setting: [configuration: `ingestion`](../configuration.md#ingestion); the API:
  [`POST /v1/ingest`](../api.md#post-v1ingest).
- Terms: [content hash](../glossary.md#content-hash), [chunk](../glossary.md#chunk),
  [source](../glossary.md#source), [parser](../glossary.md#parser).
