# Operations and safety

All development and automated tests must use a temporary data root. Never run development commands against the repository `data/` directory. Preserve pre-existing caches and compare their file hashes before and after refactor validation.

Database initialization is fail-closed. An unknown non-empty schema is an operator error; the application does not upgrade or rewrite it. Back up runtime state before changing deployed configuration.

Synchronize sources before running `money-pit intelligence update`. Each update advances one bounded batch. Use `money-pit intelligence status` to decide whether another update is necessary. Do not schedule concurrent updates against the same data root. Status and run inspection make no provider calls and need no credentials.

Run commands from the project root. Relative configuration paths resolve there, and durable state is under `./data`. `money-pit doctor` inspects schema identity in read-only mode.

Before a credential-dependent operation, run `secretspec check --scope SCOPE --reason "REASON"`. Use `inference`, `youtube_discovery`, `imap`, `brave`, `edgar`, `fred`, `portfolio_paper`, `portfolio_live`, `execution_paper`, or `execution_live`. Supply every credential in the requested scope. Credentials in scopes that the operation does not request are not prerequisites. Direct YouTube URL ingestion does not request `youtube_discovery`; its media processing requests `inference` only when frame interpretation needs it. Do not pass credential values on command lines or store them in configuration files.

Before execution, inspect the exact plan hash, expiry, trades, rejected candidates, evidence gates, and constraint results. Approval covers only that hash. A changed plan requires a new approval.

Human portfolio output masks account IDs and shows the broker environment. JSON output contains the complete typed record.

Disable execution immediately when positions, cash, open orders, fills, evidence freshness, market drift, tax state, or policy cannot be reconciled. Do not re-enable until the cause is understood and the policy version is explicit.

Run `money-pit portfolio review` only after the required intelligence updates finish. Portfolio review starts at A5 and does not rerun intelligence work. Autonomous live execution remains unavailable until the configured policy and all staged evaluation requirements pass. Reports and outcomes never mutate prompts, policy, or models automatically.

Supported SecretSpec SDK targets are Windows x64, glibc Linux x64 and arm64, and macOS arm64.
