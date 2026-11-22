# ADR 006: Weak retrieval asks, never invents

Status: Accepted

## Decision

When retrieval is weak (see ADR 011) the turn returns `kind: clarify` with a clarifying question, or a fixed refusal if `answer.on_weak_retrieval: refuse`. Answers must cite chunks; an answer with no valid citation, or the model's `INSUFFICIENT_CONTEXT` reply, also becomes a clarifying question.

## Alternatives considered

Answering anyway with a disclaimer.

## Why

Trust, and fewer hallucinations.

## Consequences

Thresholds need tuning per corpus and embedding model.
