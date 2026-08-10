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

Credential scopes match runtime capabilities. Source listing and replay resolve no credentials. Paper and live Alpaca scopes use different names, so one account cannot satisfy the other account's workflow.

Synchronize sources before running the harness:

```bash
money-pit source sync SOURCE_ID
money-pit run --source SOURCE_ID --through A5
money-pit portfolio review
money-pit plan show PLAN_ID
money-pit plan approve PLAN_ID --actor OPERATOR
money-pit plan execute PLAN_ID
```

Use `money-pit execution disable --actor OPERATOR --reason REASON` as the kill switch. Re-enabling requires the actor, reason, exact policy version, and `--confirmed`.

Use `money-pit replay RUN_ID` for historical reconstruction. Replay is read-only and cannot run A6.

Schedulers should perform source synchronization followed by `money-pit run`. There is no channel-specific ledger or latest-episode command.
