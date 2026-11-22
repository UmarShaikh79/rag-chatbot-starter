# ADR 013: Re-ingesting a document is an atomic replace

Status: Accepted

## Decision

A document is re-chunked when its content hash, chunker version or embedding model changes. One transaction upserts the document row, deletes its old chunks and inserts the new ones. Chunk IDs derive from document ID, chunker version and position. A source sync also deletes documents that disappeared from the source.

## Alternatives considered

Upserting chunks individually; leaving orphans for a cleanup job.

## Why

Stale or orphaned chunks would keep answering with outdated text.

## Consequences

Tests edit a document, re-ingest, and assert the old text cannot be retrieved; and assert a failed replace keeps the old version.
