# ADR 007: Safety on by default

Status: Accepted

## Decision

Prompt-injection checks on user input and on retrieved chunks, and PII redaction on input, output and anything persisted, are on by default. Milestone 1 ships heuristic checks (regex rules).

## Alternatives considered

Off by default.

## Why

Safe out of the box.

## Consequences

Adds a little latency and some false positives. Presidio or a model-based checker can be plugged in behind the `Guard` protocol (proposal: heuristics + Presidio by default, LLM check optional).
