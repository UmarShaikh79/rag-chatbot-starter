# ADR 008: Memory keyed by caller user ID

Status: Accepted

## Decision

The calling application passes `user_id`; conversations and turns are stored per conversation and owner. `DELETE /v1/users/{id}/memory` deletes everything for a user.

## Alternatives considered

Built-in user accounts; multi-tenancy.

## Why

Works for a small app or a large one without owning identity.

## Consequences

The calling app is responsible for user ID integrity; JWT `sub` can supply it once JWT auth lands (milestone 2).
