# ADR 011: Retrieval thresholds are calibrated, never constants

Status: Accepted

## Decision

`min_score` and `min_hits` always apply together. Calibrated values are stored with the index and win over config (`retrieval.thresholds_from: index`). Milestone 1 ships provisional values and logs a warning while an index is uncalibrated; milestone 3 adds `make calibrate`, which sweeps the golden set per embedding model and reranker.

## Alternatives considered

A single constant cosine threshold.

## Why

Cosine scores differ by model and normalisation, so 0.35 means nothing on its own. The offline profile shows this: its hashing embedder needs 0.08.

## Consequences

Enabling a reranker requires recalibration, because its scores are on another scale.
