# Troubleshooting

## Doctor reports missing storage

`./data/intelligence.sqlite3` does not exist. Doctor does not create it. Confirm the current directory and configuration before a state-changing command.

## A command fails without enough detail

Add `--debug` anywhere before a literal `--` separator, for example `money-pit intelligence status --debug`. Debug mode adds the originating traceback to stderr. Normal error output still includes the failure kind, a sanitized message, durable recovery findings when available, and a concrete next command.

## SecretSpec cannot resolve a scope

Run `secretspec check --scope SCOPE --reason "Diagnose money-pit credentials"`. Confirm that local use selects the `development` keyring profile; an unset `SECRETSPEC_PROFILE` selects it automatically. For environment-backed automation, set `SECRETSPEC_PROFILE=ci` explicitly and supply every name declared for the requested scope. You do not need names from unrequested scopes. money-pit does not load `.env` files and does not fall back between providers. A blank profile selector is invalid.

## Intelligence update reports queued work

This result is normal. One invocation processes a bounded batch and does not drain the complete backlog. Run `money-pit intelligence status [--source SOURCE_ID]` to inspect pending work, then run another matching update if you want to continue. Status makes no model or research-provider calls.

## Intelligence update stops after a provider failure

Inspect the update with `money-pit intelligence show RUN_ID`. Completed work remains durable. Correct the provider or credential problem, then run another update. The next update resumes unfinished work and does not repeat completed model calls whose work identity and version bindings are unchanged.

## Status reports needs-review work

1. Run `money-pit intelligence reviews [--source SOURCE_ID]`.
2. Copy the returned `hypothesis-review:...` ID.
3. Run `money-pit intelligence lineage HYPOTHESIS_REVIEW_ID`.
4. Inspect the proposals, discovery origins, research cases, and synthesis state.
5. Resolve the review only when you can justify `same` or `distinct`.

An open review makes only its related work unavailable.

## Status reports superseded work

Superseded rows are retained audit history and are not runnable. You do not have to remove these rows.

1. Run `money-pit intelligence lineage NAMESPACE:ID` to inspect the active successor.
2. If the lineage has no valid successor, run `money-pit intelligence audit`.
3. If the audit reports `blocked`, do not run a provider-backed update.

## Research closes with insufficient evidence

This result is a deterministic stop, not a provider failure. The system records the eligibility assessment and does not spend an A4 call. The hypothesis remains non-investable until new material evidence, a decisive contradiction, or an explicitly due review changes its state.

## Database fingerprint rejected

The selected database is non-empty and matches neither the 0.0.6 schema nor the exact retained 0.0.5 predecessor. An exact 0.0.5 database migrates automatically in one transaction. Unknown releases, metadata drift, and catalog drift are rejected without mutation. Stop and verify the configured data root; do not rename, manually migrate, or delete an unknown database automatically.

Before retrying a failed intelligence update, run `money-pit intelligence audit`. `resumable` means paid work is intact and a later run can continue it. `blocked` means a durable invariant failed; do not run a provider-backed update until the affected IDs are diagnosed.

## Candidate cannot create exposure

Inspect the thesis and plan rejection reasons. New exposure requires a supported tradable instrument, current market/risk/liquidity coverage, normalized scenarios, invalidation rules, and supported authorized factual anchors with sufficient provenance. Expired or contradictory material remains visible but cannot grant capital authority.

## Plan execution denied

Confirm the kill switch is enabled, the plan is unexpired, and approval covers its exact hash. The gateway also rechecks positions, cash, open orders, market drift, evidence freshness, tax state, policy, and recovery claims. A failed check is a stop condition, not an override prompt.

## Replay differs from a current run

Replay intentionally excludes evidence, prices, configuration, and model output recorded after the run cutoff. It never constructs a broker-write client.
