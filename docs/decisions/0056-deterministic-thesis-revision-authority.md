# Deterministic thesis revision authority

## Context and problem statement

An agent-authored thesis revision previously supplied canonical claim keys, thesis identity, and lifecycle status. Those values control durable intelligence and portfolio eligibility, so accepting them directly allowed invented support, identity changes, or an unsupported transition to active or invalidated.

## Decision drivers

- Keep financial interpretation and scenario authorship with the synthesis agent.
- Derive durable identity and claim membership only from accepted repository records.
- Make lifecycle transitions replayable, point-in-time-safe, and fail closed.
- Prevent terminal theses from being silently revived.

## Considered options

- Retain agent-authored identity and status with validation after persistence.
- Let the agent cite canonical keys projected into its prompt.
- Accept observation references and lifecycle evidence, then derive all authoritative fields deterministically.

## Decision outcome

Chosen option: accept observation references and derive authoritative revision fields.

A revision proposal promotes exactly one visible candidate or revises exactly one latest visible revision. It cites supporting and contradicting observation IDs, never canonical claim keys. The claim repository resolves those observations through the accepted resolution batch. New thesis identity derives only from the promoted candidate. Existing revisions inherit thesis identity and extend the exact latest revision.

Deterministic code derives status from admissible verification, expiry, and exact prior invalidation rules. A batch may contain at most one revision for a thesis. Invalidated and closed theses are terminal; a later investment hypothesis requires a new candidate.

### Consequences

- Agents still author scenarios, causal interpretation, confidence, and invalidation rules.
- Missing resolution or verification leaves a new revision in candidate state instead of creating exposure.
- Existing thesis updates must identify the exact prior revision they extend.
- Repository admission can revalidate every identity and lifecycle transition atomically.

## Validation

Tests reject fabricated canonical keys, arbitrary thesis identities, duplicate same-batch revisions, unsupported activation or invalidation, stale prior revision targets, and revival of terminal theses.
