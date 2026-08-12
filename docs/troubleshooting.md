# Troubleshooting

## SecretSpec cannot resolve a scope

Run `secretspec check --scope SCOPE --reason "Diagnose money-pit credentials"`. Confirm that local use selects the `development` keyring profile; an unset `SECRETSPEC_PROFILE` selects it automatically. For environment-backed automation, set `SECRETSPEC_PROFILE=ci` explicitly and supply every name declared for the requested scope. You do not need names from unrequested scopes. money-pit does not load `.env` files and does not fall back between providers. A blank profile selector is invalid.

## Database fingerprint rejected

The selected database is non-empty and matches neither the 0.0.3 schema nor the exact retained 0.0.2 predecessor. An exact 0.0.2 database migrates automatically in one transaction. Unknown releases, metadata drift, and catalog drift are rejected without mutation. Stop and verify the configured data root; do not rename, manually migrate, or delete an unknown database automatically.

## Candidate cannot create exposure

Inspect the thesis and plan rejection reasons. New exposure requires a supported tradable instrument, current market/risk/liquidity coverage, normalized scenarios, invalidation rules, and supported authorized factual anchors with sufficient provenance. Expired or contradictory material remains visible but cannot grant capital authority.

## Plan execution denied

Confirm the kill switch is enabled, the plan is unexpired, and approval covers its exact hash. The gateway also rechecks positions, cash, open orders, market drift, evidence freshness, tax state, policy, and recovery claims. A failed check is a stop condition, not an override prompt.

## Replay differs from a current run

Replay intentionally excludes evidence, prices, configuration, and model output recorded after the run cutoff. It never constructs a broker-write client.
