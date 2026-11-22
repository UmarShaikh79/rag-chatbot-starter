# Changelog

This is a template you copy and own (ADR 001). Each entry lists what changed and, when it
matters, how to bring the change into a copy you have already modified.

## 0.3.0: milestone 3

Retrieval quality and evals.

- Retrieval pipeline built from config (`retrieval.*`, ADR 019): `dense`, `bm25` (Okapi BM25
  in Postgres over a `tsvector` column, no extension, ADR 020) and `hybrid` (reciprocal rank
  fusion with optional weights); HyDE (`hyde/v1`) for chosen retrievers; query rewriting of
  follow-ups (`rewrite_query/v1`) used only for retrieval; rerank with Cohere (HTTP, no SDK),
  a local cross-encoder (`uv sync --extra rerank`) or a word-overlap reranker for tests.
- Every hit keeps its per-step scores (`ScoredChunk.scores`); weak-retrieval thresholds apply to
  a gate score (`retrieval.gate`), never to a fused rank.
- Calibration per index and scorer (ADR 021): `chatbot calibrate [--apply] [--check]` sweeps
  `min_score` with `min_hits` on the golden set; `GET /v1/sources` shows the active scorer and
  its thresholds.
- Evals (ADR 022): `chatbot eval` with hit@k, recall, MRR, answer rate, required phrases,
  citation precision, faithfulness and abstention; `--gate` against `evals/gate.yaml` and a
  committed baseline; CI `evals` job (offline) and a nightly `evals-live` workflow.
- Optional groundedness check on answers (`guardrails.output.groundedness_check`,
  `judge_groundedness/v1` or a lexical judge) with clarify, refuse or warn on failure.
- Golden set grows to 34 questions (10 unanswerable). Cohere reranker profile defaults to
  `rerank-v5.0-fast`.
- ADRs 019-022.

Upgrading a copy of 0.2.0: run `chatbot migrate` (adds `chunks.tsv`, `chunks.doc_len`,
`vector_indexes.text_config` and `retrieval_calibrations`, backfills existing chunks and moves
old thresholds to scorer `dense|dense`). `IndexInfo` lost `min_score`/`min_hits` (use
`get_calibration`). Custom retriever factories are now called with a `settings=` keyword:
add `**kwargs`. A custom vector store needs `keyword_search` for BM25 and
`get_calibration`/`save_calibration` for stored thresholds; without them those features are
unavailable and config thresholds apply.

## 0.2.0: milestone 2

Safety and memory.

- PII: detector chain (`guardrails.pii.detectors`) with the built-in `regex` detector and an
  optional Microsoft Presidio detector (`uv sync --extra presidio`) for names and places; an
  allow list; one configured redactor shared by guards, streaming, persistence and memory.
- Injection: heuristics now run on normalised text (NFKC, zero-width characters removed,
  leetspeak and spaced letters undone, base64 decoded) with new rules for role tags and headers,
  developer mode, link-based exfiltration and several languages; optional `llm` classifier
  (`guard_injection/v1`) with a fail-open or fail-closed setting.
- Output: prompt-leak check; output guards can now block, which ends a stream with `refusal`.
- Memory: rolling conversation summaries and long-term per-user facts (ADR 016), refreshed by a
  `memory_refresh` job or inline; facts are re-checked for PII and injection before storage;
  `GET /v1/users/{id}/memory` and `DELETE /v1/users/{id}/memory/facts/{fact_id}`;
  `forget_user` also deletes summaries and facts.
- Prompts: `answer/v2` (adds a memory section; now the default), `summarize/v1`,
  `extract_facts/v1`, `guard_injection/v1`.
- Auth: `auth.mode` gains `jwt` and `api_key_or_jwt` (ADR 017): OIDC discovery, JWKS caching
  and rotation, `iss`/`aud`/`exp` required, scopes, an admin scope; end users act only as
  their token's subject. `chatbot dev-token` signs local HS256 tokens.
- ADRs 016-018.

Upgrading a copy of 0.1.0: run `chatbot migrate` (adds `conversation_summaries` and
`user_facts`); if you customised the `MemoryStore`, add the new methods from
`core/interfaces.py`; if you replaced `prompts/answer/v1.yaml`, add a `{memory}` section or set
`answer.prompt` back to it; run the worker (`chatbot worker`) or set `memory.run_in: inline`.

## 0.1.0: milestone 1

First release: end-to-end RAG backend.

- Config loader (YAML + profiles + `CHATBOT__` env overrides) that rejects unknown keys.
- Protocols and a name registry for every swappable part; in-memory fakes for tests.
- OpenAI chat and embedding adapters; offline hashing embedder and extractive fake LLM.
- Ingestion of Markdown, text, HTML, PDF and DOCX files and of inline documents over the API;
  structure-aware recursive chunking; content-hash dedup; atomic per-document replace; removal
  of documents deleted at the source.
- Postgres job queue with retries, backoff, dedup keys and abandoned-job recovery; worker.
- pgvector store with per-index partial HNSW indexes, iterative scans for filtered search and an
  exact-scan path for selective filters; index records its embedding model.
- Dense retrieval with `min_score` + `min_hits` weak-retrieval detection; clarifying questions.
- Versioned prompts with strict rendering; inline `[n]` citations aligned to sources.
- Heuristic injection checks and PII redaction (input, retrieved context, output, storage);
  streaming redactor with a rolling buffer.
- SSE streaming with `draft_then_final` and `buffered` modes and one terminal event.
- Conversation memory per caller `user_id`; `DELETE /v1/users/{id}/memory`.
- API key auth, JSON logs with trace IDs, token and cost accounting.
- Docker Compose stack, GitHub Actions CI, sample corpus, 20 golden questions, ADRs 001-015.
