# ADR 014: Streaming with output guards

Status: Accepted

## Decision

`answer.stream_mode: draft_then_final` (default) streams `delta` events through a PII redactor with a rolling buffer, then sends exactly one terminal event: `final` (drafts equal the canonical answer), `replace` (a guard or citation check changed it; the client must swap its draft), `refusal`, or `error`. `buffered` sends only the terminal event. The streamed draft is never the final answer.

## Alternatives considered

Stream raw tokens; or never stream.

## Why

Fast first tokens without ever presenting unchecked text as final.

## Consequences

Clients must handle `replace`. See docs/api.md for the contract and a client sketch.
