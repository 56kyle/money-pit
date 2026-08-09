# Troubleshooting

## Database fingerprint rejected

The selected database is non-empty and does not match the 0.0.2 baseline. Stop and verify the configured data root. Do not rename, migrate, or delete an unknown database automatically.

## Candidate cannot create exposure

Inspect the thesis and plan rejection reasons. New exposure requires a supported tradable instrument, current market/risk/liquidity coverage, normalized scenarios, invalidation rules, and supported authorized factual anchors with sufficient provenance. Expired or contradictory material remains visible but cannot grant capital authority.

## Plan execution denied

Confirm the kill switch is enabled, the plan is unexpired, and approval covers its exact hash. The gateway also rechecks positions, cash, open orders, market drift, evidence freshness, tax state, policy, and recovery claims. A failed check is a stop condition, not an override prompt.

## Replay differs from a current run

Replay intentionally excludes evidence, prices, configuration, and model output recorded after the run cutoff. It never constructs a broker-write client.
