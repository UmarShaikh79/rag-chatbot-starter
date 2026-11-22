# ADR 003: Plain Python orchestration

Status: Accepted

## Decision

The default orchestrator (`orchestration/simple.py`) is plain async Python. LangGraph arrives later as an optional adapter behind the same `Orchestrator` protocol.

## Alternatives considered

LangGraph or LlamaIndex at the core.

## Why

Readable, debuggable, no framework churn in the hot path.

## Consequences

Agentic and human-in-the-loop flows come through the LangGraph adapter (milestone 5).
