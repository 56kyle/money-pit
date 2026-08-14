# Troubleshooting

## Doctor reports missing storage

`./data/intelligence.sqlite3` does not exist. Doctor does not create it. Confirm the current directory and configuration before a state-changing command.

## A command fails without enough detail

Run the command again with global `--debug`, for example `money-pit --debug intelligence status`. Debug mode adds the traceback to stderr.

## SecretSpec cannot resolve a scope

Run `secretspec check --scope SCOPE --reason "Diagnose money-pit credentials"`. Confirm that local use selects the `development` keyring profile; an unset `SECRETSPEC_PROFILE` selects it automatically. For environment-backed automation, set `SECRETSPEC_PROFILE=ci` explicitly and supply every name declared for the requested scope. You do not need names from unrequested scopes. money-pit does not load `.env` files and does not fall back between providers. A blank profile selector is invalid.

## Intelligence update reports queued work

This result is normal. One invocation processes a bounded batch and does not drain the complete backlog. Run `money-pit intelligence status [--source SOURCE_ID]` to inspect pending work, then run another matching update if you want to continue. Status makes no model or research-provider calls.

## Intelligence update stops after a provider failure

Inspect the update with `money-pit intelligence show RUN_ID`. Completed work remains durable. Correct the provider or credential problem, then run another update. The next update resumes unfinished work and does not repeat completed model calls whose work identity and version bindings are unchanged.

## Database fingerprint rejected

The selected database is non-empty and matches neither the 0.0.5 schema nor the exact retained 0.0.4 predecessor. An exact 0.0.4 database migrates automatically in one transaction. Unknown releases, metadata drift, and catalog drift are rejected without mutation. Stop and verify the configured data root; do not rename, manually migrate, or delete an unknown database automatically.

Before retrying a failed intelligence update, run `money-pit intelligence audit`. `resumable` means paid work is intact and a later run can continue it. `blocked` means a durable invariant failed; do not run a provider-backed update until the affected IDs are diagnosed.

## Candidate cannot create exposure

Inspect the thesis and plan rejection reasons. New exposure requires a supported tradable instrument, current market/risk/liquidity coverage, normalized scenarios, invalidation rules, and supported authorized factual anchors with sufficient provenance. Expired or contradictory material remains visible but cannot grant capital authority.

## Plan execution denied

Confirm the kill switch is enabled, the plan is unexpired, and approval covers its exact hash. The gateway also rechecks positions, cash, open orders, market drift, evidence freshness, tax state, policy, and recovery claims. A failed check is a stop condition, not an override prompt.

## Replay differs from a current run

Replay intentionally excludes evidence, prices, configuration, and model output recorded after the run cutoff. It never constructs a broker-write client.
