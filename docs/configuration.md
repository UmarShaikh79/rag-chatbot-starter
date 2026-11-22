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

### `observability`

`log_level`, `log_format` (`json` or `text`), `log_message_text` (false: user and model text
never reach logs). Every request gets a trace ID (or reuses a safe `X-Request-Id`), returned in
the `X-Trace-Id` header and in chat responses.
