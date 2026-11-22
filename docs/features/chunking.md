# Chunking

How a parsed document is split into the passages ("[chunks](../glossary.md#chunk)") that are
embedded, searched and shown to the model. The second stage of
[ingestion](../glossary.md#ingestion), between [sources and parsers](sources-and-parsers.md)
and embedding.

Back to the [docs home](../README.md) · See also: [architecture, step 5](../architecture.md#5-one-document-from-file-to-vector)

## At a glance

| | |
| --- | --- |
| **What it does** | Splits each section of a document into passages of at most ~512 tokens, with 64 tokens of overlap, each starting with a "Title > Heading" breadcrumb. |
| **Turned on by** | Always on; `ingestion.chunker` in [`config/app.yaml`](../../config/app.yaml) |
| **Main files** | [`ingestion/chunkers/recursive.py`](../../src/chatbot/ingestion/chunkers/recursive.py) (`RecursiveChunker`), the `Chunker` Protocol in [`core/interfaces.py`](../../src/chatbot/core/interfaces.py) |
| **Depends on** | A `ParsedDocument` from a [parser](sources-and-parsers.md) |
| **Used by** | [`IngestionPipeline.ingest_parsed`](../../src/chatbot/ingestion/pipeline.py); its output goes to the embedder and the vector store |
| **Decisions** | ADR [013](../adr/013-re-ingesting-a-document-is-an-atomic-replace.md) (stable chunk IDs, atomic replace) |

## What it does

A model can only read so much at once, and a search works better on focused passages than on
whole documents. Chunking cuts each document into passages that are:

- **Small enough**: at most `size_tokens` (512) [tokens](#how-tokens-are-counted), so several fit
  in the answer prompt.
- **Whole where possible**: split at paragraph breaks first, and only at smaller units when a
  paragraph is too long, so a passage rarely stops mid-sentence.
- **Never across sections**: text under two different headings never shares a passage.
- **Overlapping**: the last ~64 tokens of one passage start the next, so a fact on the
  boundary is found from either side.
- **Self-describing**: the first line is a breadcrumb, `Refund policy > Annual plans`, so the
  passage makes sense alone, and its embedding knows the topic.

## How it works

[`RecursiveChunker.split(doc)`](../../src/chatbot/ingestion/chunkers/recursive.py), for each
section of the document:

1. **Breadcrumb and budget:** the breadcrumb is `Title` or `Title > Heading`. Its tokens (and a
   newline) are taken from the budget: `budget = size_tokens - tokens(breadcrumb) - 1` (at
   least 16).
2. **Split into pieces that fit** (`_pieces`): if the section fits the budget it is one piece;
   otherwise it is split on the first separator that applies, recursively:
   blank lines (paragraphs) → single newlines → sentence ends (`.`, `!`, `?`) → spaces. A
   single "word" still longer than the budget is cut by characters.
3. **Pack pieces into passages** (`_pack`): pieces are added to the current passage while it
   fits. When the next one doesn't, the passage is closed, and the new one starts with the
   last pieces of the previous passage that fit within `overlap_tokens`.
4. **Make the chunk:** text = breadcrumb + newline + body; metadata = `title`, `heading`,
   `page` (PDFs) and the document's own metadata; `id` = the first 32 hex characters of
   SHA-256 of `document id | chunker version | position`.

The chunk ID depends only on the document, the chunker settings and the position, so
re-chunking the same document with the same settings gives the same IDs.

### How tokens are counted

`tokenizer: approx` (the default) estimates `ceil(characters / 4)`: no download, good enough
for English. `tokenizer: tiktoken` counts with OpenAI's `cl100k_base` encoding; install it with
`uv add tiktoken`. For languages with long words or no spaces, `approx` can be well off; use
`tiktoken`, or smaller sizes.

## Inputs and outputs

**In:** a `ParsedDocument` (`id`, `title`, `sections` of `heading` / `text` / `page`,
`metadata`, `content_hash`).

**Out:** a list of `Chunk` ([`core/types.py`](../../src/chatbot/core/types.py)):

| Field | Example |
| --- | --- |
| `id` | `5f3a…` (32 hex characters, stable) |
| `document_id` | `samples:refund-policy.md` |
| `position` | `0`, `1`, … in document order |
| `text` | `Refund policy > Annual plans\nAnnual Plus and Team subscriptions can be refunded in full within 30 days of purchase…` |
| `metadata` | `{"title": "Refund policy", "heading": "Annual plans", "path": "refund-policy.md"}` |
| `content_hash` | SHA-256 of `text` |

The chunker also exposes `version`, for example `recursive-v1:512:64:approx`. It is stored with
every document and every index.

## Turn it on, change it, turn it off

Chunking is always on. Its settings ([configuration: `ingestion`](../configuration.md#ingestion)):

| Setting | Default | Notes |
| --- | --- | --- |
| `ingestion.chunker.type` | `recursive` | The registered chunker to use. |
| `ingestion.chunker.size_tokens` | `512` | Upper bound per passage, breadcrumb included. At least 32. |
| `ingestion.chunker.overlap_tokens` | `64` | Must be smaller than `size_tokens`. |
| `ingestion.chunker.tokenizer` | `approx` | `approx` or `tiktoken`. |

**Choosing a size:** smaller passages are more precise to search but carry less context;
larger ones give the model more to work with but blur the search. 300 to 800 tokens suits most
help-center and documentation text. Measure with `make eval` rather than guessing.

**Changing the settings** changes the chunker version. Nothing breaks: each document is
re-chunked the next time it is synced (its stored version no longer matches), and until then
the old passages keep answering. To re-chunk everything at once, run `chatbot ingest --force`,
or build a fresh index beside the live one with `chatbot reindex` and switch when ready
([re-indexing](reindexing.md)). Recalibrate afterwards: passage sizes change scores
(`make calibrate-apply`).

**Turning it off** isn't possible (documents must be split to be searched), but you can make
it trivial: a very large `size_tokens` keeps each section as one passage.

## Extend it

A chunker is anything with a `version` string and `split(doc) -> list[Chunk]` (the `Chunker`
Protocol). Register a factory that takes the `ingestion.chunker` settings:

```python
from chatbot.core.registry import registry


@registry.register("chunker", "by_heading")
def make_by_heading(cfg):  # cfg: the ChunkerSection settings
    return ByHeadingChunker(max_tokens=cfg.size_tokens)
```

Then `ingestion.chunker.type: by_heading`. Rules worth keeping from the built-in one:

- Include everything that changes the output in `version` (name and settings), so a change
  re-chunks documents.
- Derive chunk IDs from document ID, version and position, so they are stable.
- Copy the document's `metadata` to each chunk, or metadata filters stop working.

Typical reasons to write one: semantic splitting (cut where meaning shifts), code-aware
splitting, or keeping tables whole.

## Test it

```bash
uv run pytest tests/unit/test_parsers_chunker.py
```

[`test_parsers_chunker.py`](../../tests/unit/test_parsers_chunker.py) checks that chunks respect
size and overlap, that IDs are deterministic and change with the version, that sections are
never merged, and that a giant word is hard-split.

## Common problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `overlap_tokens must be smaller than size_tokens` at startup | Overlap ≥ size | Lower the overlap (10–20% of the size is typical). |
| `ModuleNotFoundError: tiktoken` during ingest | `tokenizer: tiktoken` without the package | `uv add tiktoken`, or go back to `approx`. |
| Answers miss context that is in the document | Passages too small, or the fact sits across a section boundary | Raise `size_tokens`; check the parser found the right headings. |
| Retrieval finds the right document but the wrong passage | Passages too large to be specific | Lower `size_tokens`. |
| After changing settings, some answers use old passages | Documents are re-chunked only when synced | `chatbot ingest --force`, or `chatbot reindex`. |

## Related

- Before: [sources and parsers](sources-and-parsers.md) (where sections come from).
- After: embedding and storage, then retrieval: [architecture](../architecture.md#5-one-document-from-file-to-vector).
- Re-chunking everything safely: [re-indexing and aliases](reindexing.md).
- Terms: [chunk](../glossary.md#chunk), [chunker version](../glossary.md#chunker-version),
  [embedding](../glossary.md#embedding).
