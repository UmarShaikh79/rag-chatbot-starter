# ADR 010: Evals as a CI gate

Status: Accepted; implemented in milestone 3 (ADR 022)

## Decision

A golden JSONL dataset with retrieval hit rate, faithfulness and abstention metrics; pull requests fail below thresholds.

## Alternatives considered

Manual spot checks; hosted eval tools only.

## Why

Model or prompt changes are measured.

## Consequences

Needs a real API key in CI or a cached-response mode. Milestone 1 ships the dataset and a retrieval hit-rate test.
