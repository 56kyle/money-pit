# money-pit 0.0.2

`money-pit` is a persistent, point-in-time investment-research harness for long-only US equities and ETFs. It discovers theses from configured sources and a layered universe, performs bounded research, preserves immutable claim and thesis history, and produces optimizer-derived portfolio plans.

Nothing has reached production. Release 0.0.2 uses one SQLite baseline and one A1–A6 workflow. Live execution requires an exact, unexpired plan approval by default.

## Configuration

Copy the sanitized source, strategy, and execution examples from `config/examples/` to the repository root. Complete every capital-sensitive value before running the system. The tracked `secretspec.toml` declares credentials without storing their values. Use SecretSpec to place local values in the operating-system keyring:

```bash
secretspec set OPENAI_API_KEY --reason "Configure money-pit inference"
secretspec check --scope inference --reason "Verify money-pit inference"
```

An unset `SECRETSPEC_PROFILE` selects the keyring-backed `development` profile. Set `SECRETSPEC_PROFILE=ci` only in automation that deliberately supplies the declared names through its environment. money-pit does not read `.env` files or export resolved credentials into the process environment.

The SecretSpec Python SDK supports Windows x64, glibc Linux x64 and arm64, and macOS arm64.

Existing caches are not imported or read. In particular, `data/daily_show/` remains untouched historical material.

## CLI

```text
money-pit source list|sync|backfill|ingest
money-pit run [--source ID] [--as-of TIME] [--through A1|A2|A3|A4|A5|A6]
money-pit research list|show
money-pit claims list|show|refresh|verify
money-pit theses list|show|review
money-pit portfolio snapshot|review
money-pit plan show|approve|reject|execute
money-pit execution status|disable|enable
money-pit replay RUN_ID
```

Execution enablement requires an actor, reason, policy version, and explicit confirmation. Replay never constructs a broker-write client.

## Development

Use temporary data roots for development and tests. Do not point test commands at the repository `data/` directory.

```bash
uv sync
uvx nox -s lint-python
uvx nox -s typecheck
uvx nox -s tests-python
uvx nox -s security-python
uvx nox -s build-docs
uvx nox -s build-python
uvx nox -s pre-commit
```

See `docs/architecture.md` and `docs/usage.md` for system and operating details.
