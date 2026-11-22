# ADR 015: Redact before persisting; delete everything per user

Status: Accepted

## Decision

PII redaction runs before any write of conversation text. Every memory row carries `user_id`. `forget_user` deletes turns, conversations, queued job payloads and documents ingested for that user, and returns counts per table. Logs exclude message text by default. Rolling summaries and long-term facts (milestone 2, ADR 016) follow the same rule: they are built from redacted turns, checked again before they are written, and deleted by `forget_user` (`conversation_summaries`, `user_facts`).

## Alternatives considered

Store raw text and redact on read.

## Why

Sensitive data should never reach Postgres, and deletion has to be complete.

## Consequences

Copies sent to external tracing (Langfuse, milestone 5) are outside the template's control.
