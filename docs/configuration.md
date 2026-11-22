# Configuration

Configuration is loaded in three layers, later ones winning:

1. `config/app.yaml`
2. `config/profiles/<CHATBOT_PROFILE>.yaml` (default profile: `dev`)
3. Environment variables `CHATBOT__<SECTION>__<KEY>`, parsed as YAML values
   (`CHATBOT__RETRIEVAL__TOP_K=5`, `CHATBOT__GUARDRAILS__INPUT__PII_REDACTION=false`)

`config/models.yaml` holds the model catalog. Unknown keys fail startup with their path, so a
typo never silently falls back to a default. `uv run chatbot check-config` prints the merged
result. Secrets are never read from YAML; config only names the environment variables that hold
them (`OPENAI_API_KEY`, `CHATBOT_API_KEYS`, `DATABASE_URL`).

## Profiles

| Profile | Use |
| --- | --- |
| `dev` | Local development with OpenAI; readable text logs |
| `prod` | JSON logs, message text never logged |
| `offline` | No API key: hashing embedder + extractive fake LLM, own index alias, own thresholds |

## Sections

### `models` and `config/models.yaml`

`models.answer`, `models.cheap` and `models.embed` name entries in `models.yaml`. Each LLM entry
has `provider` (`openai`, `fake`), `model`, `api_key_env`, optional `base_url` (any
OpenAI-compatible endpoint), `params` passed to the API, and `price_per_mtok` (`input`,
`output`; leave `null` to omit cost). Embedding entries also have `dims`.

The OpenAI model IDs were taken from OpenAI's models page on 2026-11-21. Check them when you
clone; only `models.yaml` needs to change.

### `vector_store`

| Key | Default | Notes |
| --- | --- | --- |
| `index` | `docs` | Alias. The physical index name adds the embedding model and dims (ADR 005). |
| `hnsw.m`, `hnsw.ef_construction` | 16, 64 | Applied when the index is created. |
| `hnsw.ef_search` | 100 | Per query. Higher: better recall, slower. |
| `hnsw.iterative_scan` | `relaxed_order` | Keeps filtered queries from returning fewer than `top_k` rows (ADR 012). |
| `hnsw.max_scan_tuples` | 20000 | Upper bound for iterative scans. |
| `exact_scan_threshold` | 2000 | Filters matching at most this many chunks use an exact scan. |

### `ingestion`

`sources` is a list of `{name, type, path, include, options}`. `type: files` walks `path`
(relative to the repo root) for the `include` globs. Document IDs are `<source>:<relative path>`.

`chunker`: `size_tokens` (512), `overlap_tokens` (64), `tokenizer` (`approx` counts 4 characters
per token and needs no download; `tiktoken` is exact but needs the `tiktoken` package). Changing
any of these changes the chunker version, so the next sync re-chunks every document.

`worker`: `concurrency`, `max_attempts` (retries with exponential backoff), `poll_interval_s`,
`lock_timeout_s` (a job locked longer than this is assumed abandoned and claimed again).

### `retrieval`

| Key | Default | Notes |
| --- | --- | --- |
| `strategy` | `dense` | Registry name of the retriever. |
| `top_k` | 8 | Chunks retrieved per question. |
| `min_score` | 0.35 | Provisional (ADR 011). Strong hits score at least this. |
| `min_hits` | 1 | Retrieval is weak when fewer than this many hits are strong. |
| `thresholds_from` | `index` | Calibrated values stored on the index win; `config` forces these values. |

The provisional 0.35 is a guess for `text-embedding-3-small`. Until the calibration command
lands (milestone 3), check a few known-answerable and unanswerable questions after ingesting
your own corpus and adjust. The API logs a warning once per index while it is uncalibrated.

### `answer`

`prompt` / `clarify_prompt` (`answer/v1`, `clarify/v1`), `max_context_chunks` (6),
`citations` (`required`: an answer without a valid `[n]` citation becomes a clarifying
question), `on_weak_retrieval` (`clarify` or `refuse`), `stream_mode` (`draft_then_final` or
`buffered`, see [api.md](api.md)).

### `memory`

`history_turns` (10) turns are loaded into the prompt. `redact_before_persist` (true) runs PII
redaction on every turn before it is written, even if input redaction is off.

### `guardrails`

`input.max_chars`, `input.pii_redaction`, `input.injection_check`, `context.injection_check`
(drops retrieved chunks that look like instructions), `output.pii_redaction`, and
`stream_holdback_chars` (64): how far the streaming redactor stays behind the model so PII is
never split across two events.

The built-in checks are regex heuristics: they catch well-formed emails, phone numbers, card
numbers (Luhn-checked), IBANs, US SSNs and IPv4 addresses, and blunt injection attempts. They do
not catch names or street addresses. See [extending.md](extending.md) to add Presidio or a
model-based check.

### `auth`

`mode: api_key` (default) or `none`. `CHATBOT_API_KEYS` holds comma-separated SHA-256 hashes;
`chatbot hash-key` creates a key and its hash. Clients send `X-API-Key: <key>` or
`Authorization: Bearer <key>`. `/healthz` is always public.

### `observability`

`log_level`, `log_format` (`json` or `text`), `log_message_text` (false: user and model text
never reach logs). Every request gets a trace ID (or reuses a safe `X-Request-Id`), returned in
the `X-Trace-Id` header and in chat responses.
