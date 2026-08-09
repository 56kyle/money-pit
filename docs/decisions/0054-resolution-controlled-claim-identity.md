# Resolution-controlled claim identity and configured freshness

## Context and problem statement

Source interpretation previously assigned canonical claim keys from normalized text before adversarial resolution. Later `same`, `contradicts`, and `updates` decisions were persisted but did not change projection membership. This made accepted resolution inert, left no genuinely unresolved observations, and allowed an agent-facing field to author durable identity. Claim freshness was also a hard-coded statement-kind default rather than strategy policy.

## Decision drivers

- Claim identity must follow accepted resolution decisions.
- Agent output must not directly author durable canonical keys.
- Resolution input must be bounded and point-in-time safe.
- Same-run A4 decisions and verifications must be usable by A5 without backdating.
- Freshness must vary explicitly by economic category and horizon.

## Considered options

- Keep provisional normalized-text keys and mutate observations after resolution.
- Store an agent-proposed key on each resolution.
- Keep observations unresolved and derive append-only membership from accepted relations.

## Decision outcome

Claim observations and agent-visible resolution decisions contain no canonical key. The repository derives a key from normalized accepted statement text. For `same`, `contradicts`, and `updates`, the subject joins the resolved object membership; for `distinct`, the subject receives its own deterministic statement key. An object used as an accepted comparison anchor receives its deterministic membership when needed. Stored derived keys are checked again during replay.

The repository exposes bounded FTS candidates, exact-ID reads, and projection of the historical cutoff unioned with exact same-run observation, resolution, and verification IDs. `updates` supersedes the prior object in the projection, while `contradicts` preserves both observations and marks the projection disputed.

Claim freshness is a complete versioned strategy matrix keyed by `ClaimCategory` and `HorizonClass`. No production defaults exist. The policy version is carried by every canonical projection and portfolio decision snapshot.

### Consequences

- Unresolved observations remain visible until an accepted resolution establishes membership.
- Resolution decisions are append-only and materially affect projections.
- FTS candidate retrieval is bounded, stable, and cutoff-aware.
- Strategy configuration must provide every category/horizon freshness rule.
- Existing callers must inject a validated freshness policy into `ClaimRepository`.

## Validation

Tests cover unresolved filtering, bounded FTS candidates, every resolution relation, stable deterministic membership, and exact same-run delta projection with concurrent records excluded.
