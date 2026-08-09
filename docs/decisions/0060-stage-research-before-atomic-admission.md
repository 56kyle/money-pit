# Stage research before atomic A3 admission

## Context and problem statement

A3 performs external searches and fetches, processes evidence, interprets claims, and updates bounded research sessions. These operations cannot share one transaction with provider I/O. Treating each durable write as completed A3 state could expose a partial round after interruption, while writing only a final summary would make acquired evidence and spent budgets unrecoverable.

## Decision drivers

- Preserve provider queries, fetches, evidence, failures, and budget consumption as they occur.
- Expose A3 to downstream stages only when every session and task is terminal.
- Bind the artifact to exact durable descendants without trusting an in-memory summary.
- Permit recovery after interruption without repeating provider or model calls.
- Keep replay capability-free and based only on admitted artifacts.

## Considered options

- Hold every research write until the end of A3.
- Treat each provider or interpretation write as independently completed A3 output.
- Stage operational records durably, then atomically admit their exact repository reconstruction with the A3 artifact.

## Decision outcome

Chosen option: stage operational records and atomically admit a repository-reconstructed stage.

Research sessions own materialized tasks, results, fetches, failures, interpretation attempts, and stop events. Provider and interpretation operations persist those records as staged facts. After every selected session is terminal, the research repository reconstructs a `ResearchStageAdmission` from relational ownership. The admission contains raw typed identifiers and produces namespace-qualified artifact bindings.

One SQLite transaction validates the complete descendant set, completed planned-task mappings, successful-fetch provenance, terminal interpretation attempts, observation identities, and stop events. It then appends the A3 stage artifact and immutable admission marker. Graph state is updated only after that transaction succeeds. The derived artifact file is installed afterward and can be reconciled from the database without rerunning research.

### Consequences

- An interrupted A3 can leave staged, inspectable records but cannot appear as a completed stage.
- Recovery can reconstruct and admit terminal staged work without repeating provider calls.
- A3 artifact outputs include operational audit records as well as evidence and observations.
- Repository validation is intentionally stricter than agent or orchestration summaries.

## Validation

Tests cover terminal-session reconstruction, exact planned-task and execution-task ownership, failed and successful fetch descendants, interpretation observations, stop events, atomic rollback, immutable admission collisions, artifact reconciliation, and replay from the admitted A3 artifact.
