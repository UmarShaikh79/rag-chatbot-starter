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
| `prod` | JSON logs, message text never logged (the Kubernetes example uses it) |
| `offline` | No API key: hashing embedder + extractive fake LLM, own index alias, own thresholds |
| `local-ollama` | Local models through Ollama (chat and `nomic-embed-text`), own index alias `docs_local` |

## Sections

### `models` and `config/models.yaml`

`models.answer`, `models.cheap` and `models.embed` name entries in `models.yaml`. Each LLM entry
has `provider` (`openai`, `ollama`, `fake`), `model`, `api_key_env`, optional `base_url` (any
OpenAI-compatible endpoint, or the Ollama server), `params` passed to the API, and
`price_per_mtok` (`input`, `output`; leave `null` to omit cost). Embedding entries also have
`dims` and `params`.

Ollama (ADR 025): the base URL is the profile's `base_url`, else `OLLAMA_BASE_URL`, else
`http://localhost:11434`; `api_key_env` adds a bearer token (hosted Ollama or a proxy). Chat
`params`: `options` (passed to Ollama, e.g. `num_ctx`, `temperature`), `keep_alive`, `think`
(`false` for thinking models), `timeout_s`. Embedding `params`: `document_prefix`,
`query_prefix`, `keep_alive`, `truncate`, `timeout_s`. Unknown params fail at startup. The
Ollama tags were read from the Ollama library on 2026-11-22; choose ones that fit your hardware. Reranker entries
(`rerankers:`) have `provider` (`cohere`, `cross_encoder`, `lexical`), `model`, `api_key_env`,
`base_url` and `params`; the Cohere IDs were read from Cohere's rerank docs on 2026-11-22.

The OpenAI model IDs were taken from OpenAI's models page on 2026-11-21. Check them when you
clone; only `models.yaml` needs to change.

### `vector_store`

| Key | Default | Notes |
| --- | --- | --- |
| `backend` | `pgvector` | `pgvector` or `qdrant` (ADR 024). Changing it needs `chatbot reindex`. |
| `index` | `docs` | Alias. The physical index name adds the embedding model and dims (ADR 005). |
| `hnsw.m`, `hnsw.ef_construction` | 16, 64 | Applied when the index is created. |
| `hnsw.ef_search` | 100 | Per query. Higher: better recall, slower. |
| `hnsw.iterative_scan` | `relaxed_order` | Keeps filtered queries from returning fewer than `top_k` rows (ADR 012). |
| `hnsw.max_scan_tuples` | 20000 | Upper bound for iterative scans. |
| `exact_scan_threshold` | 2000 | Filters matching at most this many chunks use an exact scan. |
| `text_search_config` | `english` | Postgres text search config for BM25, recorded with each new index (ADR 020). |
| `qdrant.url_env` / `qdrant.url` | `QDRANT_URL` / `http://localhost:6333` | Server address; the env var wins. |
| `qdrant.api_key_env` | `QDRANT_API_KEY` | For Qdrant Cloud or a secured server; ignored when unset. |
| `qdrant.location` | `null` | `":memory:"` runs Qdrant in-process (tests only). |
| `qdrant.hnsw.m` / `qdrant.hnsw.ef_construct` | 16 / 100 | Applied when a collection is created. |
| `qdrant.hnsw_ef` | 128 | Search beam width per query. |
| `qdrant.on_disk` | `false` | Keep vectors on disk (memmap) when they outgrow RAM. |
| `qdrant.upsert_batch`, `prefer_grpc`, `timeout_s` | 256, `false`, 30 | Client settings. |

Whatever the backend, Postgres keeps the catalog: index rows, aliases, document hashes and
parsed text, calibrations. Changing the embedder, its prefixes, the chunker or the backend
means a new physical index: see [operations/reindexing.md](operations/reindexing.md). Tuning
pgvector in production: [operations/pgvector.md](operations/pgvector.md).

### `ingestion`

`sources` is a list of `{name, type, path, include, options}`. `type: files` walks `path`
(relative to the repo root) for the `include` globs. Document IDs are `<source>:<relative path>`.

`type: sql` turns each row of a query into a document, from any database SQLAlchemy reaches
(Postgres works as installed; add a driver such as `pymysql` for others). `options`:

| Key | Default | Notes |
| --- | --- | --- |
| `url_env` | `DATABASE_URL` | Env var with the database URL. Use a read-only user. |
| `query` | required | Runs in full on each sync; rows that disappear are removed from the index. |
| `id_column` | required | Document ID becomes `<source>:<value>`. |
| `text_columns` / `text_template` | one required | Columns joined by blank lines, or a format string like `"# {title}\n\n{body}"`. |
| `title_column`, `uri_template` | `null` | Title and link shown in citations. |
| `metadata_columns` | `[]` | Copied to chunk metadata, usable as retrieval filters. |
| `user_id_column` | `null` | Rows owned by an end user; `forget_user` deletes their documents. |
| `media_type` | `text/markdown` | Or `text/plain`, `text/html`. |
| `batch_size` | 500 | Rows fetched per round trip. |

`chunker`: `size_tokens` (512), `overlap_tokens` (64), `tokenizer` (`approx` counts 4 characters
per token and needs no download; `tiktoken` is exact but needs the `tiktoken` package). Changing
any of these changes the chunker version, so the next sync re-chunks every document.

`worker`: `concurrency`, `max_attempts` (retries with exponential backoff), `poll_interval_s`,
`lock_timeout_s` (a job locked longer than this is assumed abandoned and claimed again).

### `retrieval`

| Key | Default | Notes |
| --- | --- | --- |
| `strategy` | `dense` | `dense`, `bm25`, `hybrid`, or a retriever you register. |
| `top_k` | 8 | Chunks retrieved per question (after fusion and rerank). |
| `bm25.k1` / `bm25.b` | 1.2 / 0.75 | Okapi BM25 parameters (ADR 020). |
| `hybrid.retrievers` | `[dense, bm25]` | Fused with reciprocal rank fusion (ADR 019). |
| `hybrid.candidates` | 20 | Hits each retriever returns before fusion. |
| `hybrid.rrf_k` / `hybrid.weights` | 60 / `{}` | RRF constant; per-retriever weights (default 1.0). |
| `hyde.enabled` | false | Embed "question + hypothetical answer" from the cheap model (`hyde/v1`). |
| `hyde.apply_to` | `[dense]` | Which retrievers get the hypothetical text. |
| `query_rewrite.enabled` | false | Rewrite follow-ups into standalone queries (`rewrite_query/v1`). Only retrieval sees the rewrite. |
| `query_rewrite.only_with_history` | true | Skip the call on a conversation's first message. |
| `rerank.enabled` | false | Re-score `rerank.candidates` (20) hits with `rerank.model`. |
| `rerank.model` | `cohere:rerank` | A name under `rerankers` in `config/models.yaml`: `cohere:rerank` (`COHERE_API_KEY`), `local:bge-reranker-base` (`uv sync --extra rerank`), `lexical:overlap` (tests). |
| `gate` | `auto` | The score `min_score` applies to: `rerank` when reranking, else `dense`, else `bm25`. Never the fused RRF score. |
| `min_score` / `min_hits` | 0.35 / 1 | Provisional, always applied together (ADR 011). |
| `thresholds_from` | `index` | Calibrated values stored for this index and pipeline win; `config` forces these values. |

Calibrated thresholds are stored per index and **scorer** (the gate plus the pipeline, e.g.
`dense|hybrid(dense,bm25)`; ADR 021). Change the strategy, HyDE or the reranker and the API
logs that thresholds are provisional until you run `chatbot calibrate --apply` again.
`GET /v1/sources` shows the active scorer and whether it is calibrated.

### `answer`

`prompt` / `clarify_prompt` (`answer/v1`, `clarify/v1`), `max_context_chunks` (6),
`citations` (`required`: an answer without a valid `[n]` citation becomes a clarifying
question), `on_weak_retrieval` (`clarify` or `refuse`), `stream_mode` (`draft_then_final` or
`buffered`, see [api.md](api.md)).

### `memory`

`history_turns` (10) turns are loaded into the prompt. `redact_before_persist` (true) runs PII
redaction on every turn before it is written, even if input redaction is off.

Summaries and long-term facts (ADR 016):

| Key | Default | Notes |
| --- | --- | --- |
| `run_in` | `worker` | `worker`: a `memory_refresh` job after each turn (run `chatbot worker`). `inline`: before the response returns. |
| `summary.enabled` | true | Fold turns older than the history window into a rolling summary. |
| `summary.refresh_after_turns` | 4 | Fold once this many turns have left the window. |
| `summary.max_chars` | 1500 | Summary length cap. |
| `summary.prompt` | `summarize/v1` | Runs on the `cheap` model. |
| `long_term.enabled` | true | Extract facts about the user. Only for requests with a `user_id`. |
| `long_term.max_facts_per_user` | 50 | Least recently seen facts are dropped beyond this. |
| `long_term.facts_in_prompt` | 20 | Most recent facts added to the answer prompt. |
| `long_term.max_fact_chars` | 200 | Longer facts are discarded. |
| `long_term.prompt` | `extract_facts/v1` | Runs on the `cheap` model. |

Summaries and facts reach the model through the `memory` variable of `answer/v2`. If you keep
`answer/v1`, memory is still stored but not shown to the model.

### `guardrails`

`input.max_chars`, `input.pii_redaction`, `input.injection_check`, `context.injection_check`
(drops retrieved chunks that look like instructions; also screens long-term facts),
`output.pii_redaction`, `output.prompt_leak_check` (refuses an answer that repeats ten or more
consecutive words of the system prompt), and `stream_holdback_chars` (64): how far the streaming
redactor stays behind the model so PII is never split across two events. Output checks run on
the complete answer in both stream modes.

Groundedness (off by default; one more model call per answer): `output.groundedness_check`
judges the answer against the sources it cited. `groundedness.judge` is `llm`
(`judge_groundedness/v1` on `model_role`, default cheap) or `lexical` (word overlap, offline);
`min_score` (0.7) is the share of claims that must be supported; `on_fail` is `clarify`
(default: ask instead), `refuse` or `warn` (adds `groundedness:low` to `warnings`); `on_error`
is `allow` or `block` when the judge call fails. The same judge scores faithfulness in evals.

Detectors (ADR 018):

| Key | Default | Notes |
| --- | --- | --- |
| `pii.detectors` | `[regex]` | `regex`: emails, phones, cards (Luhn), IBANs, US SSNs, IPv4. `presidio`: adds names, places and more. Spans from all detectors are merged. |
| `pii.allow` | `[]` | Exact strings never redacted, e.g. your public support address. |
| `pii.presidio.*` | `en`, `en_core_web_sm`, 0.5 | `language`, `spacy_model`, `entities`, `score_threshold`. |
| `injection.detectors` | `[heuristic]` | `heuristic`: rules on normalised text (zero-width, leetspeak, spaced letters, base64 decoded). `llm`: a classifier prompt on a model. |
| `injection.llm.model_role` | `cheap` | Which model profile classifies. |
| `injection.llm.stages` | `[input]` | Add `context` to check retrieved chunks too (one call per chunk). |
| `injection.llm.on_error` | `allow` | `block` refuses the message when the classifier call fails. |

To use Presidio:

```bash
uv sync --extra presidio
uv run python -m spacy download en_core_web_sm   # or uv pip install the model wheel
CHATBOT__GUARDRAILS__PII__DETECTORS='[regex, presidio]' uv run chatbot serve
```

In Docker, build with `--build-arg UV_EXTRAS="--extra presidio"` and
`--build-arg SPACY_MODEL_URL=<model wheel URL from github.com/explosion/spacy-models>`.
Presidio also redacts names in answers quoted from your documents; add names that are fine to
show (your product, your staff) to `pii.allow`.

### `auth`

`mode`: `api_key` (default), `jwt`, `api_key_or_jwt` or `none`. `/healthz` is always public.

API keys: `CHATBOT_API_KEYS` holds comma-separated SHA-256 hashes; `chatbot hash-key` creates a
key and its hash. Clients send `X-API-Key: <key>` or `Authorization: Bearer <key>`. A key is a
trusted backend: it may pass any `user_id`.

JWT/OIDC (ADR 017): clients send the access token from your identity provider as
`Authorization: Bearer <jwt>`.

| Key | Default | Notes |
| --- | --- | --- |
| `jwt.issuer` | required | Must equal the token's `iss`. Also the base for OIDC discovery. |
| `jwt.audience` | required | Must appear in the token's `aud`. |
| `jwt.jwks_url` | discovered | Set it when your provider has no discovery document. |
| `jwt.algorithms` | `[RS256, ES256]` | `none` is never accepted. `HS256` needs `hs256_secret_env`. |
| `jwt.hs256_secret_env` | null | Env var holding a shared secret (32+ characters), for local development. |
| `jwt.user_id_claim` | `sub` | Becomes the chat `user_id`. A different `user_id` in the body is a 403. |
| `jwt.required_scopes` | `[]` | Scopes every token must carry (`scope` or `scp` claim). |
| `jwt.admin_scope` | `chatbot:admin` | Lets a token act for any user and reach ingestion, documents and jobs. |
| `jwt.leeway_s` | 30 | Clock skew allowed on `exp`, `nbf`, `iat`. |
| `jwt.jwks_cache_s` | 3600 | Key cache lifetime. An unknown `kid` triggers one early refetch. |

Example for Auth0 (other providers differ only in the issuer URL):

```yaml
auth:
  mode: api_key_or_jwt
  jwt:
    issuer: https://your-tenant.eu.auth0.com/
    audience: https://chatbot.example.com
```

Without an identity provider, try it locally with an HS256 secret:

```bash
export DEV_JWT_SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(48))")
export CHATBOT__AUTH__MODE=jwt CHATBOT__AUTH__JWT__ISSUER=http://localhost \
       CHATBOT__AUTH__JWT__AUDIENCE=chatbot CHATBOT__AUTH__JWT__ALGORITHMS='[HS256]' \
       CHATBOT__AUTH__JWT__HS256_SECRET_ENV=DEV_JWT_SECRET
TOKEN=$(uv run chatbot dev-token --sub alice)
```

### `evals`

| Key | Default | Notes |
| --- | --- | --- |
| `dataset` | `evals/datasets/golden.jsonl` | One question per line; see `evals/README.md`. |
| `gate` | `evals/gate.yaml` | Floors per profile, checked by `chatbot eval --gate`. |
| `baselines_dir` | `evals/baselines` | `<profile>.json`, written by `--update-baseline`. |
| `hit_at` | `[1, 3]` | Extra hit@k columns besides hit@top_k. |
| `faithfulness_judge` | `llm` | `llm`, `lexical` (offline profile) or `off`. |
| `calibration.target_answer_recall` | 0.9 | Share of answerable questions calibration must keep. |
| `calibration.max_min_hits` | 3 | `min_hits` values tried. |
| `calibration.min_unanswerable` | 5 | Warn when the dataset has fewer unanswerable questions. |

### `observability`

`log_level`, `log_format` (`json` or `text`), `log_message_text` (false: user and model text
never reach logs). Every request gets a trace ID, returned in the `X-Trace-Id` header and in
chat responses: the OpenTelemetry trace ID when tracing is on, otherwise a safe `X-Request-Id`
or a new one. The full guide is [observability.md](observability.md).

| Key | Default | Notes |
| --- | --- | --- |
| `otel.enabled` | `false` | Traces and metrics over OTLP/HTTP (ADR 026). Needs `uv sync --extra otel`. Destination: `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS`. |
| `otel.service_name` | `null` | Default `app.name`; `OTEL_SERVICE_NAME` wins. |
| `otel.traces`, `otel.metrics` | `true`, `true` | Either can be turned off. |
| `otel.metrics_interval_s` | 30 | How often metrics are pushed. |
| `otel.sample_ratio` | 1.0 | Share of new traces kept; a sampled parent always wins. |
| `otel.capture_text` | `false` | Redacted prompts, answers and chunk IDs on spans. |
| `otel.console` | `false` | Also print spans to stdout. |
| `langfuse.enabled` | `false` | Langfuse over its OTLP endpoint (ADR 027). Needs `uv sync --extra langfuse`. |
| `langfuse.base_url_env` / `base_url` | `LANGFUSE_BASE_URL` / `https://cloud.langfuse.com` | The env var wins. US cloud: `https://us.cloud.langfuse.com`. |
| `langfuse.public_key_env`, `secret_key_env` | `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | Missing keys fail at startup. |
| `langfuse.capture_text` | `true` | Redacted text goes to Langfuse; `forget_user` cannot delete it there. |
| `langfuse.send_user_id` | `true` | The caller's `user_id` as the Langfuse user. |
| `usage.persist` | `true` | A `usage_records` row per chat turn, job and `ingest --inline` (ADR 028). Needs Postgres. |

### `orchestrator`

`simple` (default): the plain-Python turn in `orchestration/simple.py` (ADR 003). `langgraph`:
the same steps as a LangGraph graph (ADR 029), for flows that need tools, loops or human
approval; needs `uv sync --extra langgraph`. Any name registered with
`@registry.register("orchestrator", "...")` works too.
