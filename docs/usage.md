# Operations

Create local configuration from `config/examples/` and provide secrets through `.env` or the configured keyring. Do not commit local source, strategy, execution, or credential files.

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
