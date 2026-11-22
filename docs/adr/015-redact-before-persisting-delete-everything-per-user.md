# ADR 015: Redact before persisting; delete everything per user

Status: Accepted (milestone 1 covers turns, conversations, jobs, owned documents)

## Decision

PII redaction runs before any write of conversation text. Every memory row carries `user_id`. `forget_user` deletes turns, conversations, queued job payloads and documents ingested for that user, and returns counts per table. Logs exclude message text by default. Milestone 2 adds rolling summaries and long-term facts, which follow the same rule.

## Alternatives considered

Store raw text and redact on read.

## Why

Sensitive data should never reach Postgres, and deletion has to be complete.

## Consequences

Copies sent to external tracing (Langfuse, milestone 5) are outside the template's control.
