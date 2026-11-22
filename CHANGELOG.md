# Changelog

This is a template you copy and own (ADR 001). Each entry lists what changed and, when it
matters, how to bring the change into a copy you have already modified.

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
