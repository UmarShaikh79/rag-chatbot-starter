# ADR 007: Safety on by default

Status: Accepted

## Decision

Prompt-injection checks on user input and on retrieved chunks, and PII redaction on input, output and anything persisted, are on by default. Milestone 1 ships heuristic checks (regex rules); milestone 2 makes them detector chains with optional Presidio and model-based checks (ADR 018).

## Alternatives considered

Off by default.

## Why

Safe out of the box.

## Consequences

Adds a little latency and some false positives. Presidio and a model-based injection classifier are available as opt-in detectors (ADR 018).
