# ADR 025: Local models through Ollama's native API

Status: Accepted (milestone 4); implements the local half of ADR 009

## Decision

Two thin adapters talk to Ollama's native API with httpx (already a dependency):
`provider: ollama` for chat (`/api/chat`, streaming NDJSON, `format` with a JSON schema for
structured output) and for embeddings (`/api/embed`, batched). The `local-ollama` profile
selects them: an answer model, a cheap model and `nomic-embed-text` (768 dims) with the
`search_document:` / `search_query:` prefixes it was trained with, into its own alias
`docs_local`.

Profile `params` carry Ollama options (`num_ctx`, `temperature`), `keep_alive`, and
`think: false` for thinking models; any `<think>` block that still arrives is stripped, in
streaming too, so reasoning never reaches an answer or a citation check. Unknown params fail
at startup. A missing model fails with the `ollama pull` command to run.

## Alternatives considered

- The `openai` provider against Ollama's OpenAI-compatible `/v1`: works (and stays possible
  with `base_url`), but drops `keep_alive`, `think` and `num_ctx`, and the error messages.
- The `ollama` Python package: one more dependency for two endpoints.
- LiteLLM as a gateway: a larger dependency and another layer to configure.

## Consequences

- Model tags in `models.yaml` are a starting point read from the Ollama library on 2026-11-22;
  choose for your hardware. Local scores sit on their own scale, so calibrate the local index.
- Prefixes are part of how vectors are made but not of the index's recorded model ID;
  changing them needs `chatbot reindex` like a model change.
- CI tests the adapters against a mocked server; `tests/e2e/test_live_ollama.py` runs against
  a real one when `OLLAMA_TEST_URL` is set.
