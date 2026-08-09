# Operations and safety

All development and automated tests must use a temporary data root. Never run development commands against the repository `data/` directory. Preserve pre-existing caches and compare their file hashes before and after refactor validation.

Database initialization is fail-closed. An unknown non-empty schema is an operator error; the application does not upgrade or rewrite it. Back up runtime state before changing deployed configuration.

Before execution, inspect the exact plan hash, expiry, trades, rejected candidates, evidence gates, and constraint results. Approval covers only that hash. A changed plan requires a new approval.

Disable execution immediately when positions, cash, open orders, fills, evidence freshness, market drift, tax state, or policy cannot be reconciled. Do not re-enable until the cause is understood and the policy version is explicit.

Autonomous live execution remains unavailable until the configured policy and all staged evaluation requirements pass. Reports and outcomes never mutate prompts, policy, or models automatically.
