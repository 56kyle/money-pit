# Load configuration by stage responsibility

## Status

Accepted

## Context and Problem Statement

Research-only commands previously loaded capital strategy and execution authority. This made A1-A4 depend on broker policy and could construct capabilities that those stages must never receive.

## Decision Drivers

- Fail closed without widening stage authority.
- Do not invent hashes for configuration that a stage did not load.
- Keep point-in-time replay bound to the exact applicable configuration projections.

## Considered Options

- Load all documents for every command.
- Use sentinel hashes for unavailable documents.
- Parse typed intelligence, capital, and execution projections according to the terminal stage.

## Decision Outcome

Use typed stage scopes. A1-A4 load the source and intelligence projections, A5 additionally loads the capital strategy, and A6 additionally loads execution policy. Durable run records use optional stage-validated bindings, and A5 decision snapshots omit execution configuration until A6 revalidates the current explicit execution policy.

### Consequences

- Research commands work without broker or execution configuration.
- Missing capital values fail only when A5 is requested; missing execution values fail only at A6.
- Configuration models and run records expose absence explicitly instead of hashing a placeholder.

## Validation

CLI help/import smokes, stage-specific configuration tests, run-record validation, replay binding tests, Ruff, and basedpyright cover the boundary.
