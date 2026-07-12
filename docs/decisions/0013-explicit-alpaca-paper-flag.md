# Explicit required `alpaca_paper` config field, superseding service-name-suffix inference

- Status: accepted
- Date: 2026-07-12
- Deciders: owner, python-dev

## Context and Problem Statement

`money_pit/config.py::_paper_from_service` derived the Alpaca paper-vs-live routing decision
from a suffix (`-paper` / `-live`) on the keyring **service name** (`config.alpaca_service`).
That suffix is a project-invented convention, not an Alpaca concept: Alpaca distinguishes paper
from live purely by separate credentials and separate base URLs. Routing capital on a
hand-maintained label means the label can silently disagree with the actual credential type — a
`-paper`-suffixed service name holding live credentials, or vice versa. The naming convention is
also undocumented and unenforced, so a service registered without the suffix raised at resolution
time even when the operator knew exactly which mode they wanted.

The prior behavior did fail *safe* in one narrow sense — a name-vs-key mismatch would fail at
Alpaca auth rather than route an order into the wrong environment — but capital routing should not
depend on the spelling of a keyring service name at all.

## Decision Drivers

- The paper/live decision routes real capital; it must be explicit and operator-declared, not
  inferred from an arbitrary label.
- Faithful to the existing "refuse to guess / fail closed" posture: the run must not proceed in
  any trading mode unless the environment is explicitly declared.
- Reuse the existing typed fail-closed (`CredentialResolutionError`) and fail-loud
  (`ValidationError`) paths in `load_config`; add no new failure surface.

## Decision Outcome

**Add a required `alpaca_paper: bool` field to `Config`**, placed with the other `alpaca_*`
fields. Because `Config` is a `BaseSettings` with `env_prefix="MONEY_PIT__"`, it maps to
`MONEY_PIT__ALPACA_PAPER` automatically. The field is **required with no default**:

```python
alpaca_paper: bool
```

`resolve_alpaca_credentials` sets `paper=config.alpaca_paper` directly. `_paper_from_service`,
`_ALPACA_PAPER_SUFFIX`, and `_ALPACA_LIVE_SUFFIX` are deleted as dead code. The
`CredentialResolutionError` docstring, which referenced service-name paper/live ambiguity, is
corrected — that failure mode no longer exists.

Making the field required (rather than defaulting to paper) means an unset
`MONEY_PIT__ALPACA_PAPER` surfaces through the existing `_missing_required_env_vars` path in
`load_config`, raising `CredentialResolutionError` naming `MONEY_PIT__ALPACA_PAPER`. Going live
requires an explicit `MONEY_PIT__ALPACA_PAPER=false`; no trading mode is ever entered unchosen.

### Consequences

Good: the routing decision is explicit, operator-declared, and decoupled from the keyring service
name; the service name is now a free-form label. Fail-closed on an unset flag and fail-loud on a
mistyped one both reuse existing typed paths.

Bad / to watch: this is a new required environment variable. Every deployment `.env` and every
test that constructs `Config(...)` or sets `MONEY_PIT__ALPACA_*` must now supply `alpaca_paper` /
`MONEY_PIT__ALPACA_PAPER`, or `load_config` fails closed. python-test-writer updates the pinning
tests; the `paper/live routing unchanged` confirmation from ADR 0010 is superseded here.

### Confirmation

`python -c` smoke confirmed: valid paper (`alpaca_paper=True`) and live (`alpaca_paper=False`)
construction; an unset `MONEY_PIT__ALPACA_PAPER` raising `CredentialResolutionError` naming
`MONEY_PIT__ALPACA_PAPER`; a mistyped `MONEY_PIT__ALPACA_PAPER=notabool` propagating a raw
`pydantic.ValidationError` (not swallowed into `CredentialResolutionError`). Grep of `src/`
confirms no remaining references to the three deleted symbols.

## Considered Options (key rejections)

- **Infer from the API key ID prefix (e.g. `PK` paper / `AK` live).** Rejected: the prefix
  convention could not be confirmed in Alpaca's current documentation, and capital routing must not
  be gated on an unverified, undocumented key format — it would trade one implicit inference for
  another.
- **A `alpaca_paper: bool = True` field defaulting to paper.** Rejected in favor of
  required-explicit. A default would let a run proceed in a trading mode nobody chose; the
  fail-closed posture requires that live vs paper be an explicit declaration every time, with going
  live demanding an explicit `false`.
- **Keep the service-name suffix inference.** Rejected: it is the core finding — capital routing on
  a hand-maintained keyring label that can silently disagree with the credential type.

## More Information

Supersedes the suffix-inference routing described implicitly by ADR 0010's "paper/live routing
unchanged" confirmation line. Related: `src/money_pit/config.py`,
`docs/decisions/0010-config-pydantic-settings-migration.md`.
