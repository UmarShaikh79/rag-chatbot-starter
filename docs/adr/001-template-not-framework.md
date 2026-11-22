# ADR 001: Template, not framework

Status: Accepted

## Decision

The project is a clone-and-own repository with small modules behind `typing.Protocol` interfaces and a name registry.

## Alternatives considered

An installable package or a plugin framework.

## Why

Users edit code freely and never wait on an upstream release; there is no version lock-in.

## Consequences

Upgrades are manual. Keep CHANGELOG.md with migration notes for anyone who wants to pull later changes.
