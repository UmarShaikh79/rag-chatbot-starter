# ADR 023: Re-index beside the live index, then switch

Status: Accepted (milestone 4); implements the re-index half of ADR 005

## Decision

Anything that changes the vectors (embedding model or its prefixes, chunker settings, the
vector store, HNSW build settings) is applied by building a **new physical index** while the
old one keeps serving, then moving the alias in one transaction:

1. `chatbot reindex`, run with the new configuration, creates an empty index in status
   `building`, reachable only through the staging alias `<alias>_next`. A previous unfinished
   build with the same settings is resumed; `--restart` starts over.
2. It re-reads every configured source into it, and rebuilds every other document of the live
   index (pushed through `POST /v1/ingest`) from the parsed text the catalog now keeps
   (`documents.sections`, migration 0004).
3. It runs the same pass a second time to pick up what changed meanwhile. Unchanged documents
   are skipped by content hash, so this pass is cheap.
4. It marks the index `ready`, unless a live document could not be rebuilt (rows stored before
   migration 0004 have no text): then it stops with the list, and `--allow-missing` or
   re-pushing those documents (to `<alias>_next`) resolves it.
5. Before switching, calibrate and evaluate the staging alias
   (`CHATBOT__VECTOR_STORE__INDEX=docs_next chatbot calibrate --apply`, `chatbot eval --gate`);
   calibrations belong to the index (ADR 021), so they carry over.
6. `chatbot index activate` points the alias at the new index and remembers the old one;
   `chatbot index rollback` swaps them back; `chatbot index drop <name>` deletes an index no
   alias points at.

Retrievers keep refusing to search an index whose embedding model differs from the configured
one (ADR 005); the error now names `chatbot reindex`.

## Alternatives considered

- Re-embed in place: mixes two models' vectors during the run and has no way back.
- Use Qdrant's native collection aliases: only covers one store; the Postgres catalog gives the
  same alias semantics for pgvector and Qdrant and lets an index move between them.
- Keep a full copy of every raw document: the parsed sections are enough to re-chunk and cost
  far less than PDFs and DOCX files.
- Switch automatically when the build finishes: the default stops before switching so the new
  index can be calibrated and gated; `--activate` exists for small deployments.

## Consequences

- Storage doubles while both indexes exist, and the parsed text is stored once more beside the
  chunks. Drop the old index once the new one has proved itself.
- The alias flip is atomic, but processes still running the old configuration will get a 409
  ("index built with another model") until they restart with the new one. Activate as part of
  the deploy that rolls out the new configuration.
- Changes to the live index after the second pass starts (a narrow window) are not copied;
  rerunning `chatbot reindex` before activating picks them up.
- Documents ingested before migration 0004 that no source can re-read must be pushed again.
