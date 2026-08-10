# Operations and safety

All development and automated tests must use a temporary data root. Never run development commands against the repository `data/` directory. Preserve pre-existing caches and compare their file hashes before and after refactor validation.

Database initialization is fail-closed. An unknown non-empty schema is an operator error; the application does not upgrade or rewrite it. Back up runtime state before changing deployed configuration.

Before a credential-dependent operation, run `secretspec check --scope SCOPE --reason "REASON"`. Use `inference`, `youtube_media`, `imap`, `brave`, `edgar`, `fred`, `portfolio_paper`, `portfolio_live`, `execution_paper`, or `execution_live`. Do not pass credential values on command lines or store them in configuration files.

Before execution, inspect the exact plan hash, expiry, trades, rejected candidates, evidence gates, and constraint results. Approval covers only that hash. A changed plan requires a new approval.

Disable execution immediately when positions, cash, open orders, fills, evidence freshness, market drift, tax state, or policy cannot be reconciled. Do not re-enable until the cause is understood and the policy version is explicit.

Autonomous live execution remains unavailable until the configured policy and all staged evaluation requirements pass. Reports and outcomes never mutate prompts, policy, or models automatically.

Supported SecretSpec SDK targets are Windows x64, glibc Linux x64 and arm64, and macOS arm64.
