# Undeliverable owner emails are recorded as a run artifact, not propagated

- Status: accepted
- Date: 2026-07-21
- Deciders: owner, architecture author
- Amends: ADR 0009 (decision #3)

## Context and Problem Statement

Gmail delivery was fail-closed at two points, and either one aborted a run.

At **construction time**, `production_deps()` calls `make_gmail_email_sender(config)`, which raises
`CredentialResolutionError` when `config.gmail_address` is unset or the keyring holds no app
password. With no app password provisioned, no run could be composed at all — the capital-critical
dependency check treats `send_email` as mandatory, so an unconfigured mailbox blocked the whole
pipeline, including paths that never send.

At **send time**, `EmailSendError` propagated out of both consumers: the notification node
(`pipeline/notification.py`) and the recovery node's halt / reconciled emails
(`pipeline/recovery.py`). ADR 0009 decision #3 made this deliberate — "fail closed, notify loudly."

Practice showed the propagation is the *less* informative outcome. In the recovery-HALT path the
node dies after `recovery.json` has already been written, so the owner gets neither the warning
email nor a clean halt — only a traceback whose text contains none of the reconciliation detail the
email was carrying. A dead mailbox produced a dead run *and* destroyed the message.

## Decision Drivers

- ADR 0009's driver still binds: an undelivered halt email must **surface**, not be swallowed.
- Email is a notification transport, not a capital gate. The authoritative record of what a run did
  already lives in its artifacts (`recovery.json`, `determination.json`, the execution journal).
- The failure that most needed handling is the one on the capital-risk path, where aborting loses
  the most information.
- Whatever replaces propagation must be durable and discoverable where the owner already looks.

## Decision Outcome

**1. An undeliverable email is recorded, not swallowed and not raised.**
`with_undelivered_record(send_email, working_dir)` wraps an `EmailSender` and, on `EmailSendError`
only, writes the message — UTC timestamp, failure reason, subject, and the **full body** — to
`undelivered_email_NN.txt` in the run's working directory, and emits a `logger.error` naming the
subject, reason, and artifact path. The body is written to the artifact only, not duplicated into
the serialized loguru sink. The run then continues to its terminal state.

The artifact carries no `To:` line. The decorator wraps an opaque `EmailSender` and cannot verify
that a recipient it was handed matches the one the wrapped sender actually closes over, so the field
could silently disagree with where delivery was attempted. `config.owner_recipient` already records
the addressee; an absent field beats a field that can lie.

The next index is derived from the directory contents (`_next_undelivered_email_path` globs the
existing artifacts and takes the lowest free number), not from a counter held in the closure. For
the sequential sends a run actually performs, the numbering is therefore self-enforcing: two
wrappers over the same working directory cannot clobber each other, and the caller holds no
unstated "exactly one wrapper per run" invariant. The glob-then-write is a check-to-use gap, so the
guarantee is scoped to sequential sends; concurrent sends into one working directory would need an
exclusive-create retry instead. Nothing in the graph sends concurrently.

**The recording path itself does not raise.** An `OSError` from the glob or the write is caught and
degraded to a `logger.error` carrying the **full body inline** — the deliberate inverse of the happy
path, because with no artifact written the log becomes the only surviving copy of the message.
Without this, a read-only or full working directory would abort the run from inside the recovery
node after `recovery.json` was written: exactly the defect this ADR exists to remove, relocated
from SMTP to the filesystem.

This satisfies ADR 0009's "must surface" driver by a different mechanism than propagation: the
message survives in full, next to the run's other artifacts, rather than being reduced to a
traceback.

**2. "Gmail is not configured" is modelled as a sender whose every send fails.**
`make_unconfigured_email_sender(reason)` returns an `EmailSender` raising
`EmailNotConfiguredError` on every call, and `_gmail_or_unconfigured_email_sender(config)`
substitutes it when `CredentialResolutionError` fires during composition. This collapses the two
failure points into a single fallback path, so there is exactly one place that decides what
"undelivered" means. A null object that *fails per its own contract* was chosen over a silent no-op
precisely so it flows through that same recording path and produces the same artifact.

`EmailNotConfiguredError` subclasses `EmailSendError`, and `with_undelivered_record` catches the
base type — so the fallback path is indifferent to which failure occurred, while a never-configured
mailbox and a rejected relay remain separable by type rather than by message text. The repo forbids
asserting on message text, so without the subtype the distinction would have been untestable.

Alpaca credential resolution in `production_deps()` remains eagerly fail-closed. It moves capital;
email does not.

**3. The recovery node shares the fallback.** `run_pipeline` wraps the sender once, before
`build_graph`, so the notification node and the recovery node receive the same instance — a run
producing both a recovery notice and a notification writes `undelivered_email_01.txt` and
`undelivered_email_02.txt`. The recovery path is where propagation did the most damage; exempting
it would have preserved the original defect.

**4. The `EmailSender` contract is unchanged.** `Callable[[str, str], None]` still holds; the
fallback is a decorator applied at the single composition point in `run_pipeline`, where
`working_dir` is already in scope. No consumer changed.

**5. `src/email_server/server.py` is unaffected.** The FastMCP server has no run working directory
and its caller is an MCP client that should see the error, so it keeps raising `EmailSendError`.
The isolation invariant from ADR 0009 decision #2 is untouched.

### Consequences

Good: an unprovisioned or broken mailbox no longer blocks composition or aborts a run; the
notification content survives in full and lands beside the artifacts it describes; the two failure
modes share one path and one test surface; no contract or consumer changed.

Bad / to watch: a recorded email is a *silent* delivery failure from the owner's perspective — the
artifact and the ERROR log exist, but nothing pushes the fact into view during an unattended run.
If runs become genuinely unattended, add a delivery-status field to the run's terminal echo, or a
second transport. Note that the artifact-write failure in particular is typed nowhere — it is
distinguishable only by log content, so acting on it programmatically would need a return value or
an event, not a log line. The catch is narrow (`EmailSendError` only), and the artifacts accumulate within a
run directory but are never retried; this is a dead-letter record, not a retry queue, and must not
be mistaken for one.

### Confirmation

`nox -s tests-python`. Unit tier: `make_unconfigured_email_sender` raises `EmailNotConfiguredError`,
which is also caught as `EmailSendError`; a succeeding sender delivers and writes no artifact; a
failing sender returns normally and writes `undelivered_email_01.txt` carrying subject, body, and a
`Reason:` line that distinguishes an unconfigured mailbox from a rejected relay; two failures
produce `_01` and `_02`, both through one wrapper and through two independent wrappers over the same
directory, each artifact carrying its own body; the happy-path log names the artifact and omits the
body; an unwritable working directory (a file where a directory belongs, and an absent directory)
degrades to a log carrying the full body, writes no artifact, and does not raise; a
non-`EmailSendError` exception still propagates; and `production_deps` with no `gmail_address`
yields deps whose `send_email` raises `EmailNotConfiguredError` rather than letting
`CredentialResolutionError` escape composition.

Integration tier pins the composition point itself: `run_pipeline` is driven to a recovery-HALT with
an **unwrapped** failing sender in `overrides`, so the decorator applied at
`orchestration.py` is the only thing that can produce an artifact. The run still reaches
`RecoveryDecision.HALT`, still writes `recovery.json`, and lands `undelivered_email_01.txt` carrying
the halt subject. Reverting that one wiring line turns all three red.

## Considered Options (key rejections)

- **Keep propagating (ADR 0009 as written).** Rejected: on the recovery-HALT path it destroys the
  message it was meant to deliver and converts a clean halt into a traceback.
- **Log the error only, not the message.** Rejected: the error text says delivery failed; it does
  not say *what* the owner missed. The body is the product of the notification node. (It is,
  however, the correct last resort when the artifact itself cannot be written — see decision #1.)
- **Silent no-op sender when Gmail is unconfigured.** Rejected: it would bypass the recording path,
  producing runs that appear to have notified when nothing was written anywhere.
- **Handle the fallback inside each node.** Rejected: duplicates the policy across two nodes and
  still leaves the construction-time failure unaddressed.
- **Widen `EmailSender` to carry the working directory.** Rejected: contaminates a two-argument
  transport contract for one composition-site concern that a decorator solves.
- **Pass the recipient into the decorator so the artifact can record `To:`.** Built first, then
  reversed: nothing could enforce that the supplied recipient matched the wrapped sender's, so the
  most trusted-looking line in a dead-letter record was the one most able to be wrong. A `Protocol`
  exposing `.recipient` would fix it honestly but changes a contract used at four sites for one
  field the config already holds.
- **A closure counter for the artifact index.** Built first, then reversed: it made correct
  numbering depend on an unstated "exactly one wrapper per run" invariant held entirely by the
  caller. Reading the directory costs one glob on a path that is already failing.
- **Exclusive-create (`open(path, "x")`) with retry to close the check-to-use gap.** Rejected as
  premature: no path in the graph sends concurrently, and the claim is scoped accordingly above.
  This is the fix if that ever changes.
- **A per-run retry queue or a second transport.** Rejected as premature; noted above as the
  escalation if unattended runs make silent non-delivery unacceptable.

## More Information

This adds a sanctioned swallow site, governed by ADR 0011 ("silent swallow sites narrow and log"):
the catch is narrow — the base `EmailSendError` in the wrapper, `OSError` in the recording path —
and every branch terminates in a `logger.error`, so the site conforms.

Related: ADR 0009 (email transport; this ADR amends its decision #3), ADR 0011 (swallow-site
discipline), ADR 0008 (credential resolver pattern), `src/money_pit/email_sender.py`,
`src/money_pit/pipeline/orchestration.py`, `src/money_pit/pipeline/notification.py`,
`src/money_pit/pipeline/recovery.py`.
