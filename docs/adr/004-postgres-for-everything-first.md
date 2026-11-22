# ADR 004: Postgres for everything first

Status: Accepted

## Decision

Postgres holds documents, vectors (pgvector), conversations and the job queue (`SELECT ... FOR UPDATE SKIP LOCKED`).

## Alternatives considered

Redis + Celery; a separate vector database.

## Why

One service to run, back up and secure.

## Consequences

A Redis queue or cache can be added later; very high ingest volume may need it.
