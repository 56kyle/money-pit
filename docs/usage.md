# Operations

Create local source, strategy, and execution configuration from `config/examples/`. The tracked `secretspec.toml` is the credential schema. It contains no credential values.

Install the SecretSpec CLI and let it prompt for each credential you need. Do not put a value on the command line:

```bash
secretspec set OPENAI_API_KEY --reason "Configure money-pit inference"
secretspec set ALPACA_PAPER_API_KEY --reason "Configure paper portfolio access"
secretspec set ALPACA_PAPER_SECRET_KEY --reason "Configure paper portfolio access"
secretspec check --scope portfolio_paper --reason "Verify paper portfolio access"
```

The `development` profile stores values in the operating-system keyring and is selected when `SECRETSPEC_PROFILE` is unset. Automation must select the `ci` profile with `SECRETSPEC_PROFILE=ci`; that profile reads the declared names from the process environment. A blank selector is invalid, and there is no fallback between the profiles. Never commit local configuration or credential values.

Credential scopes match runtime capabilities. A credential marked `required` is required only when money-pit requests a scope that contains it. Disabled or uninvoked providers and adapters do not request their scopes. Source listing and replay resolve no credentials. Paper and live Alpaca scopes use different names, so one account cannot satisfy the other account's workflow.

Synchronize or ingest sources before updating intelligence:

```bash
money-pit source sync SOURCE_ID
money-pit source ingest SOURCE_ID https://youtu.be/VIDEO_ID
money-pit intelligence status --source SOURCE_ID
money-pit intelligence audit --source SOURCE_ID
money-pit intelligence reviews --source SOURCE_ID
money-pit intelligence update --source SOURCE_ID
money-pit intelligence show RUN_ID
money-pit intelligence promote-claim CANONICAL_KEY --reason "operator rationale"
money-pit portfolio review
money-pit plan show PLAN_ID
money-pit plan approve PLAN_ID --actor OPERATOR
money-pit plan execute PLAN_ID
```

Run `money-pit source check` before the first sync. It parses the registry without providers, credentials, or database changes. `source list` is also read-only. Use `source status SOURCE_ID` to inspect cursors.

Global options precede the command. `money-pit --json intelligence status` writes one JSON value to stdout. Progress remains on stderr. Add `--debug` to include a traceback on stderr.

YouTube sync and backfill use the `youtube_discovery` credential scope. Direct ingestion accepts one canonical `https://www.youtube.com/watch?v=VIDEO_ID` or `https://youtu.be/VIDEO_ID` URL and does not require a YouTube Data API key. It retains the configured source's provenance and trust policy and does not read or update sync or backfill cursors.

An update processes a bounded batch and can finish with queued work remaining. `--iterations N` requests up to `N` separately durable runs. It stops early only after zero durable lifecycle transitions. Use `--through interpretation`, `--through discovery`, `--through research`, or `--through synthesis` to select the last stage.

A source-specific update processes work caused by that source. An update without `--source` advances the global queue fairly. Research evidence verifies its assigned candidate and does not automatically become new discovery input.

`money-pit intelligence status`, `money-pit intelligence audit`, `money-pit intelligence reviews`, `money-pit intelligence lineage`, and `money-pit intelligence show` only read durable state. They do not resolve credentials or construct inference and research providers. The audit distinguishes safe resumable work from blocked invariant violations.

Status reports `needs_review`, `unavailable`, `non_investable`, and `superseded` counts separately in JSON. Human output replaces underscores with spaces. An insufficient-evidence assessment is non-investable history. It does not create runnable work or change the next update action.

Each candidate proposal retains one exact semantic variant. Equal variants join the same canonical hypothesis group automatically. A possible match that is not exact creates a review. While a review is open, the affected work is unavailable and unrelated work can continue. Inspect and resolve a review with:

A proposal without an instrument, instrument reference, or layered universe reference remains unclassified and unavailable. A shared theme does not grant capital-reference authority or automatic equivalence.

```bash
money-pit intelligence reviews --source SOURCE_ID
money-pit intelligence lineage hypothesis-review:REVIEW_HASH
money-pit intelligence resolve REVIEW_ID --decision same --actor OPERATOR --reason "Why these proposals represent one hypothesis"
```

Use `--decision distinct` when the proposals differ materially. A `same` decision is rejected when the capital reference, direction, or horizon is incompatible. Resolution is an append-only operator action. It does not run a provider or read a credential.

`money-pit portfolio review` starts A5 from the latest eligible durable intelligence and current portfolio state. It does not rerun interpretation, discovery, research, or synthesis. Insufficient-evidence and unresolved hypotheses cannot create portfolio exposure.

`intelligence promote-claim` is the explicit, provider-free boundary for sending a materially changed canonical claim back through discovery. The reason is recorded as operator provenance. A3 verification evidence never recursively creates discovery work.

Use `money-pit execution disable --actor OPERATOR --reason REASON` as the kill switch. Re-enabling requires the actor, reason, exact policy version, and `--confirmed`.

Use `money-pit replay RUN_ID` for historical reconstruction. Replay is read-only and cannot run A6.

Use `money-pit doctor` to inspect local configuration and database schema identity. Doctor does not resolve secrets, construct providers, create storage, or migrate storage.

Schedulers should synchronize each source and then invoke one bounded intelligence update. A scheduler can repeat updates while status reports queued work, but one invocation must not drain the complete global backlog.
