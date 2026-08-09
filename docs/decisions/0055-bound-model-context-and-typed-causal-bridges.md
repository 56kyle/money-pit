# Bound model context and require typed causal bridges

## Context and problem statement

Research and synthesis can accumulate more durable evidence than a model request can safely inspect. Overlapping evidence chunks can also duplicate claims. Separately, treating any nonempty explanation as a causal bridge lets model prose bypass deterministic temporal gates.

## Decision drivers

- Keep the complete provider-visible inference envelope within an exact character budget.
- Preserve durable evidence identity and source authorization outside the model context.
- Prevent overlapping chunks from creating duplicate observations.
- Keep temporal, regime, proxy, and update exceptions deterministic and auditable.

## Considered options

- Truncate serialized JSON after rendering.
- Let each prompt select arbitrary context and explain temporal exceptions in prose.
- Project complete atomic records deterministically and validate typed bridge relationships.

## Decision outcome

Chosen option: deterministic atomic-record projection and typed directional bridges.

Evidence aliases record whether they own a chunk (`core=true`) or provide neighboring context. A1 accepts a claim only when its earliest cited alias is core. A2 greedily covers the complete layered universe and claim context across atomic chunks. A3 and A4 project complete typed records, with resolution subjects and candidates kept indivisible. All stages record included and omitted identities and fail locally when one atomic unit cannot fit.

The agent factory, rather than orchestration, owns the hidden fixed envelope calculation. It counts the exact system prompt, canonical output schema, model metadata, user-message serialization, and configured response reserve before provider invocation. Projection and invocation use the same canonical user-message renderer. Evidence chunking receives the complete request acceptance predicate, so it does not approximate the final request from evidence-only JSON. The boundary exposes only the remaining allowance and typed fit predicate to projection code and returns a typed component breakdown when the complete envelope is too large.

A4 reads one keyset-bounded unresolved-observation page before it performs any per-subject full-text search. It records the continuation cursor and any prompt-projection omissions. Deferred subjects remain unresolved for a later run; the harness never loads or searches the complete unresolved population merely to truncate it afterward.

Signal contributions may propose only a typed bridge kind, source subject, target subject, and rationale. The harness checks the direction against configured relationships. Non-overlapping intervals require an accepted update relationship. Free text alone never changes compatibility.

### Consequences

- Models may see an explicit bounded subset while durable artifacts retain the selection and alias map.
- Configuration must declare bridge relationships before they can affect synthesis.
- Context projection and complete-envelope failures occur before provider or persistence side effects.

## Validation

Tests cover transcript and frame aliases, overlapping chunk ownership, exact rendered budgets, atomic-record overflow, repeated-query exclusion, nonadjacent horizons, update bridges, regime mismatch, and unauthorized free-text bridges.
