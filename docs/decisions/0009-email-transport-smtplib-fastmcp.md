# Email transport: direct smtplib in the pipeline, an independent FastMCP server alongside

- Status: accepted
- Date: 2026-07-06
- Deciders: owner, architecture author

## Context and Problem Statement

The notification sub-agent sends the owner an email on any halt/escalation path through the
`EmailSender = Callable[[str, str], None]` seam (`(subject, body)`). Phase 7 must supply a real sender.
Two design facts are in tension:

- `architecture.md` §8 offers latitude: "communication — build (small) via FastMCP, **or** use
  `smtplib` directly."
- `package_structure.md` states a hard isolation invariant: `src/email_server/` is a separate
  deployable MCP server with **no import relationship to `money_pit`**.

So there are two potential consumers of "send an email over Gmail": the pipeline's in-process
`EmailSender`, and the standalone FastMCP `send_email(to, subject, body)` server. A shared sender module
would have to live somewhere — and if it lived in `money_pit`, the email server importing it would
break the isolation invariant.

## Decision Drivers

- Preserve the email-server isolation invariant (no `money_pit` import).
- Don't make the pipeline process depend on the FastMCP server being up to send a halt email.
- Keep the failure mode honest and testable — an undelivered halt email must surface, not be swallowed.
- Avoid a heavy dependency; the owner address is Gmail.

## Decision Outcome

**1. The pipeline's `EmailSender` calls smtplib directly.** `money_pit/email_sender.py` exposes
`make_gmail_email_sender(config)` returning a `send_email(subject, body)` that delivers over Gmail SMTP
with STARTTLS to `config.owner_recipient`. The pipeline does **not** talk to the FastMCP server — a
halt email never depends on a second process being alive. The module is named `email_sender.py`, not
`email.py`, to avoid shadowing the stdlib `email` package that `smtplib` itself imports.

**2. The FastMCP server owns an independent smtplib sender.** `src/email_server/server.py` exposes
`send_email(to, subject, body)` backed by its own smtplib logic, reading its config from environment
variables, importing nothing from `money_pit`. The ~10 lines of SMTP boilerplate are **deliberately
duplicated** across the boundary rather than shared through a cross-package import — isolation is worth
more than de-duplicating a small, stable snippet. The server is a separate deployable artifact for
external callers, not a pipeline dependency.

**3. SMTP failures are wrapped in a typed `EmailSendError`.** The sender catches
`smtplib.SMTPException`/`OSError` and raises `EmailSendError`. The notification node does not catch it,
so a failed halt-email propagates loudly (fail closed, notify loudly) — and the failure-mode test can
assert on a stable type rather than a raw library exception's message.

> Amended by ADR 0031: the typed `EmailSendError` stands, but it no longer propagates out of the
> pipeline. `run_pipeline` wraps the sender so an undeliverable message is recorded in full as an
> `undelivered_email_NN.txt` run artifact and the run continues.

**4. Credentials via the same keyring pattern.** The Gmail app password resolves through
`resolve_gmail_app_password(config)` (keyring under `config.gmail_service` / `config.gmail_address`),
raising `CredentialResolutionError` when the address or stored secret is absent — consistent with the
Alpaca resolver (ADR 0008).

### Consequences

Good: the isolation invariant holds structurally (verified by an import test); the pipeline's halt path
has no cross-process dependency; the failure mode is a typed, testable boundary; no heavy email
dependency (stdlib smtplib + FastMCP, already transitively present, declared explicitly for the
server).

Bad / to watch: the SMTP boilerplate exists in two places, so a change to send semantics (e.g. adding
DKIM headers or a retry policy) must be made in both `money_pit/email_sender.py` and
`email_server/server.py`. This is the accepted cost of isolation; if the logic grows non-trivial,
revisit whether the email server should become the single sender the pipeline calls over MCP.

### Confirmation

`nox -s tests-python`: `send_email` success and failure are exercised with a hand-written fake SMTP
(no `unittest.mock`); the failure raises `EmailSendError`; `make_gmail_email_sender` with no
`gmail_address` raises `CredentialResolutionError`; an import test asserts `email_server.server`
imports nothing from `money_pit`.

## Considered Options (key rejections)

- **Pipeline sends over MCP to the FastMCP server.** Rejected: makes a halt email depend on a second
  process being up — the worst time to add a dependency is the failure path — and adds an MCP client to
  the pipeline for no gain over direct smtplib.
- **A shared sender module in `money_pit` imported by the email server.** Rejected: breaks the
  isolation invariant outright.
- **Let the raw `smtplib.SMTPException` propagate.** Rejected: the failure-mode test would assert on
  library message text (the repo forbids message-text assertions); a typed wrapper is the stable
  contract.
- **A transactional email API (SendGrid/Resend).** Rejected for v0: an extra account, API key, and
  dependency for a single-recipient owner-notification path that Gmail SMTP covers.

## More Information

Related: `docs/architecture.md` §8/§6.9, `docs/package_structure.md` (email_server isolation),
ADR 0008 (credential resolver pattern), `src/money_pit/email_sender.py`,
`src/email_server/server.py`, `src/money_pit/pipeline/notification.py`.
