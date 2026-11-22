# ADR 009: Hosted and local defaults

Status: Accepted

## Decision

OpenAI through the profile names `answer`, `cheap` and `text-embedding-3-small`. Ollama with `nomic-embed-text` for local runs (milestone 4). An offline profile (hashing embedder + extractive fake LLM) runs everything without a key.

## Alternatives considered

Anthropic, Gemini, LiteLLM as a gateway.

## Why

Most common default, and the one chosen for this project.

## Consequences

Provider adapters are thin, so others can be added in one file each.
