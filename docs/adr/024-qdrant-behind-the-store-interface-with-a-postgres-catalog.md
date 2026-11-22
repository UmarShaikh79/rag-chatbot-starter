# ADR 024: Qdrant behind the store interface, with a Postgres catalog

Status: Accepted (milestone 4); extends ADR 004

## Decision

Qdrant is the second built-in vector store (`vector_store.backend: qdrant`, `uv sync --extra
qdrant`), implementing the same `VectorStore` protocol as pgvector. Responsibilities split:

- **Qdrant** holds the vectors: one collection per physical index, cosine distance, one point
  per chunk (point ID derived from the chunk ID) with text, position, document ID, user ID and
  metadata as payload, and keyword payload indexes on `document_id`, `user_id` and
  `metadata.source` (filterable HNSW).
- **Postgres** keeps the catalog for every store (`chatbot.stores.catalog`): index rows with
  model, dims, chunker version, backend and status; aliases; document hashes and parsed text;
  calibrations. Postgres already runs for memory and jobs.

A document replace is one `batch_update_points` call (delete the document's points, then
upsert the new ones), followed by the Postgres row. `forget_user` deletes the user's points
from every Qdrant index before it deletes the rows (new optional `UserDocumentEraser`
capability). An alias that points at an index in another store is refused with a message
naming `chatbot reindex`, which can move an index from pgvector to Qdrant or back.

The client is the official `qdrant-client` (async), so tests can run Qdrant's in-process local
mode; CI also runs the suite against a real `qdrant/qdrant` service.

## Alternatives considered

- Keep everything in Qdrant (collection metadata and aliases): two catalogs to keep in step,
  and calibrations, forget_user and re-indexing would each need a second implementation.
- Plain REST with httpx (as for Cohere): no local mode for tests; the client is small enough.
- BM25 in Qdrant via sparse vectors: a different scoring model with its own calibration;
  left as an extension. With Qdrant, use `retrieval.strategy: dense` (the bm25 retriever
  explains this when misconfigured).

## Consequences

- Qdrant has no multi-operation transactions: a search during a replace can miss the document
  or see part of the new version, never a mix of old and new (ADR 013 holds in that sense).
- Two systems to back up; Postgres is still the source of truth for what is indexed, so a lost
  Qdrant volume is recovered with `chatbot reindex` (sources plus stored text).
- Filter semantics differ slightly from pgvector's JSON containment: a scalar filter also
  matches a list that contains it.
