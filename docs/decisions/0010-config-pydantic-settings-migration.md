# Config on pydantic-settings, with explicit .env loading and the email server folded in

- Status: accepted
- Date: 2026-07-06
- Deciders: owner, python-dev

## Context and Problem Statement

`money_pit/config.py` carried every default twice: once as a pydantic field default on `Config`
(a plain `BaseModel`), and again as the literal fallback in a hand-rolled `os.environ.get(name, literal)`
block inside `load_config`. The two could drift silently — a change to `kelly_fraction`'s default in one
place did nothing unless mirrored in the other. `Config` was not frozen (a value-shaped settings object
that downstream code holds for the run should not mutate). Missing required credentials raised a bare
`ValueError` rather than the module's own `CredentialResolutionError`, so callers could only distinguish
the failure by message text. `load_config` was memoized with `@lru_cache`, which turns a path-parameterized
loader into a process-global singleton and forces tests to reach for `load_config.cache_clear()`. The
FRED and Brave API keys sat in the model as bare `str`, so a secret could land in a log line or repr.

Separately, `src/email_server/server.py` (ADR 0009) re-declared its own `EmailSendError`, its own
`_DEFAULT_SMTP_HOST` / `_DEFAULT_SMTP_PORT`, and read `MONEY_PIT__*` env directly — a second, drifting
copy of the app's config surface, justified at the time by a hard "no import from money_pit" isolation
invariant.

## Decision Drivers

- Single source of truth for every default (kill the double-default drift risk).
- Value-shaped config should be frozen; failures should be typed, not stringly-distinguished.
- Secrets should not be printable by accident.
- Preserve the `load_config(path=...)` seam that lets a caller point at an alternate `.env`.
- Stop duplicating the SMTP defaults and the `EmailSendError` type across the email-server boundary.

## Decision Outcome

**1. `Config` becomes a frozen `BaseSettings`.** `model_config = SettingsConfigDict(env_prefix="MONEY_PIT__",
frozen=True)`. All fields are flat, so `env_prefix` alone reproduces the existing `MONEY_PIT__<FIELD>`
mapping — no nested delimiter. The field default is now the *only* source of each default; the hand-rolled
`os.environ.get(..., literal)` block is deleted. Defaults that are shared constants keep referencing them
(`gmail_service` → `GMAIL_KEYRING_SERVICE`, `owner_recipient` → `DEFAULT_OWNER_RECIPIENT`, SMTP →
`DEFAULT_SMTP_HOST` / `DEFAULT_SMTP_PORT`).

**2. `.env` loads explicitly via `python-dotenv`, not `SettingsConfigDict(env_file=...)`.** pydantic-settings
does not auto-load `.env`, and its `env_file` option is fixed at class-definition time — it cannot honor a
per-call `path` argument. `load_config(path=DEFAULT_CONFIG_PATH)` therefore does an explicit `load_dotenv(path)`
boundary step *before* instantiating `Config()`, preserving the parameterized-path seam that tests and the
CLI rely on.

**3. No caching.** `@lru_cache` is dropped; `load_config` returns a fresh frozen `Config` each call. The two
real consumers (`orchestration.build_pipeline`, `__main__.pin_order_schema`) already call it exactly once, so
there is no hot-path cost. Only test conftests relied on `cache_clear()`.

**4. Missing required vars raise `CredentialResolutionError`.** A missing required field makes `BaseSettings()`
raise `ValidationError` at instantiation; `load_config` catches it, extracts the `missing`-typed field
locations, and re-raises `CredentialResolutionError` naming the offending `MONEY_PIT__*` vars. A
`ValidationError` with no missing fields (e.g. a bad `smtp_port` type) is *not* swallowed — it propagates, so a
genuine mis-type surfaces loudly rather than masquerading as a credential problem.

**5. Env-native secrets become `SecretStr`.** `fred_api_key` / `brave_api_key` are `SecretStr | None`, so they
do not print in a repr or log. Their one consumer (`orchestration`'s `_Direct*Tools` construction) unwraps
with `.get_secret_value()` at the point of use, keeping the `_Direct*Tools` signatures as plain `str | None`.
Keyring-resolved secrets (Alpaca secret key, Gmail app password) stay *out* of the model entirely — the
`resolve_alpaca_credentials` / `resolve_gmail_app_password` functions resolve them against a passed-in `Config`,
unchanged.

**6. The email server folds into the package, consuming the shared SMTP defaults and the single
`EmailSendError`.** `email_server/server.py` now imports `DEFAULT_SMTP_HOST` / `DEFAULT_SMTP_PORT` from
`money_pit.constants` and `EmailSendError` from `money_pit.email_sender`, deleting its own duplicates. This
supersedes ADR 0009's isolation invariant (decision #2 and the "no import from money_pit" rule): the owner has
ruled the server needn't be an import-isolated standalone.

The server's **credential sourcing stays env-based** (`MONEY_PIT__GMAIL_ADDRESS` /
`MONEY_PIT__GMAIL_APP_PASSWORD` via `_require_env`), and it does **not** route through `load_config()` or the
keyring `resolve_gmail_app_password` path. Two deployment-contract reasons:
- `load_config()` now requires `alpaca_service` / `alpaca_username`. Routing the email server through it would
  couple a pure email process to the trading credentials it has no business needing.
- The keyring path requires a keyring backend on the server's deployment host; the server is documented (ADR
  0009) as a deployable artifact for external callers, where env-var credentials are the portable contract.

So the fold-in is scoped to *structural* single-sourcing (defaults + the exception type), not credential
sourcing. `EmailServerConfigError` remains local — it is the server's own env-gate error, not duplicated in
money_pit.

### Consequences

Good: one source per default; frozen value object; typed missing-credential failure that a test can assert by
type; secrets non-printable; the SMTP defaults and `EmailSendError` have one home. Path-parameterized loading
survives the migration.

Bad / to watch: the ADR 0009 import-isolation test and its docstring claim ("`email_server.server` imports
nothing from `money_pit`") are now false and must be retired by python-test-writer. `Config()` no longer
requires kwargs at call sites — tests that construct `Config(alpaca_service=..., ...)` still work, but they now
also read the ambient `MONEY_PIT__*` environment, so a leaked env var could bleed into a test; the
`integration_env` teardown fix (Wave 6) matters more now. The email server still duplicates its *credential
sourcing* logic rather than sharing it; that is a deliberate deployment-contract choice, revisitable if the
server is ever confined to the pipeline host.

### Confirmation

Import + `load_config` round-trip smoke (temp `.env`) confirms: frozen (mutation raises `ValidationError`),
single-sourced defaults, `llm_model` == `claude-sonnet-5`, `fred_api_key` is `SecretStr`, missing required vars
raise `CredentialResolutionError` naming both, and `email_server.server.EmailSendError is
money_pit.email_sender.EmailSendError`. python-test-writer pins: required-var-missing fails closed, env-override
boundary, default-model value, paper/live routing unchanged.

## Considered Options (key rejections)

- **Keep `BaseModel` + the hand-rolled env block.** Rejected: the double-default drift risk is the core finding.
- **`SettingsConfigDict(env_file=DEFAULT_CONFIG_PATH)` instead of explicit `load_dotenv`.** Rejected: fixes the
  `.env` path at class-definition time, breaking the per-call `path` override.
- **Keep `@lru_cache`.** Rejected: caching a path-parameterized I/O loader creates a process-global singleton
  and the `cache_clear()` test dance; the real consumers call once anyway.
- **Route the email server through `load_config()` / keyring.** Rejected: couples the email process to Alpaca
  credentials and to a keyring backend on its host — a deployment-contract break (see decision #6).

## More Information

Supersedes the isolation invariant in ADR 0009 (`docs/decisions/0009-email-transport-smtplib-fastmcp.md`).
Related: `docs/package_structure.md` (email_server isolation note, now stale), ADR 0011 (typed fetch results),
`src/money_pit/config.py`, `src/money_pit/constants.py`, `src/email_server/server.py`,
`src/money_pit/pipeline/orchestration.py`.
