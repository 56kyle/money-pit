# Credentials stay `SecretStr` to the point of use, and upstream exception text never leaves the local log

- Status: accepted (amended 2026-07-22 — redaction rejected, superseded by structural closure of the sink)
- Date: 2026-07-21
- Deciders: owner, python-dev, python-reviewer

## Context and Problem Statement

`Config.fred_api_key` and `Config.brave_api_key` are `SecretStr` (`config.py:87-88`), but
`run_pipeline` called `.get_secret_value()` on both at composition time and handed plain `str` to
`_DirectDeterministicTools` and `_DirectOpenEndedTools`. Those objects live for the whole run, so
the protection `SecretStr` exists to give — no accidental exposure through `repr()`, logging, or a
traceback that captures frame locals — was discarded at the moment the pipeline composed.

Moving the unwrap to the point of use fixed that, and immediately exposed a second, worse problem
that the composition-time unwrap had been masking. The FRED key is a **query parameter** (FRED API
v1 offers no header auth), and `requests` embeds the full request URL — query string included — in
its connection-layer exception text:

```
HTTPSConnectionPool(...): Max retries exceeded with url:
/fred/series/observations?series_id=CPILFESL&api_key=<THE REAL KEY>&file_type=json
```

`fetch_fred_series` interpolated that exception into two sinks, and one of them is not terminal:
`FetchError.reason` flows into `retrieval.py:132`, which builds `f"Data fetch error: {reason}"`
into `Answer.answer` — persisted as a run artifact, sent to the LLM provider, and reachable by the
owner-email path.

So the pre-existing exposure was passive (needs a debugger or a memory dump); the exposure a
naive "hold `SecretStr` longer" change leaves behind is **active** — the operator's key written to
disk and shipped to a third party on the single most likely failure, a network blip.

## Decision Drivers

- `SecretStr` should mean something at runtime, not just in the config type.
- A credential must never reach a durable sink: a run artifact, an outbound request body, or an
  email.
- Diagnostics must survive — an operator debugging a failed fetch still needs to know why. They do
  not have to survive *in the propagating value*; the local log is a legitimate place to keep them.
- The fix must not depend on remembering to apply it at each new call site. Stated plainly: **no option
  considered here meets this driver**, including the one adopted. It is recorded because it is what
  discriminates the options — see the Consequences section for how far the adopted option gets.

## Decision Outcome

**1. Credential material stays `SecretStr` until the expression that transmits it.**
`_DirectDeterministicTools._fred_api_key` and `_DirectOpenEndedTools._brave_api_key` are
`SecretStr | None`; `run_pipeline` passes `config.fred_api_key` / `config.brave_api_key` straight
through. `.get_secret_value()` is called inline in the `requests` params dict and the
`X-Subscription-Token` header respectively.

Scope claim, stated precisely: this removes the plaintext from a **run-lifetime attribute**. It does
not make the plaintext transient in the absolute sense — `requests` assembles it into a URL and
retains it on the `PreparedRequest`, so a traceback through `requests` still holds it. The honest
statement is "no longer held for the lifetime of the run," not "exists only as a transient
expression."

**2. No upstream exception text reaches `FetchError.reason`. Detail goes to the local log only.**
Every failure handler in `_DirectDeterministicTools` logs the exception in full via
`logger.warning`, and returns a `FetchError` whose `reason` names the subject (series id, ticker)
and something the handler knows independently of the caught exception — the failure class
(`type(error).__name__`), or, where the `except` catches a single type and so makes that name a
constant, a datum from the response body such as the observation `float()` rejected — and carries no
exception text at all. The operator
keeps the complete diagnostic where it is safe — on the local machine — and the propagating value
carries only what is safe to persist and transmit.

This applies to the whole fetch layer, not to FRED alone: `fetch_ticker_price` gets the same
treatment. yfinance carries no credential, but the invariant is worth more as a property of the
layer than as a FRED special case, and yfinance error text is equally untrusted for other reasons
(upstream URLs, vendor identifiers, arbitrary third-party strings).

**2a. Redaction was tried and is rejected.** A prior revision of this ADR adopted
`_error_text_with_secret_redacted(error, secret)` — `str(error).replace(secret_value, "<redacted>")`.
That helper and its placeholder constant have been deleted. It is unsound:

- It is string-munging over untrusted upstream text, and its correctness depends on the plaintext
  appearing *verbatim*. `requests` URL-encodes query parameters, so a key containing any character
  that percent-encodes never matches, and the redactor returns the leak unchanged while reading as
  though it had handled it.
- It is opt-in per call site, and opt-in in the worst direction: its guarantee was only ever "someone
  remembered to call the helper," so the default-shaped handler — the one written by someone who knew
  nothing about this ADR — leaks.
- It generalises to nothing. It knows about one secret in one module, so every new secret and every
  new sink is a fresh place to get it right.

The replacement is not a better redactor. It is not processing the untrusted text at all.

**3. The key is bound to a local before the `None` guard.** `fetch_fred_series` reads
`self._fred_api_key` once into `fred_api_key` and checks that. The class is mutable, so narrowing on
the attribute would not survive the intervening `try` block. This survives the amendment for
type-narrowing reasons, though its original motivation (guaranteeing the redactor had a key) is gone.

**4. Brave needs no change, and now needs no exemption.** `brave_search` logs and returns `[]` on
failure, so its error text reaches `logger` only — never a `FetchError.reason`, and so never
`Answer.answer`. Under redaction this was an argued exemption; under decision #2 it is simply the
same rule, since the sink that mattered does not exist on that path.

**5. `raise_for_status()` is now safe to add, and no longer blocked on anything.**
`fetch_fred_series` does not call `raise_for_status()` today, so an invalid key parses as `NoData`
rather than `FetchError` — a known defect deferred to the queued refactor of this module. Adding it
raises `requests.HTTPError`, a `RequestException`, whose `str()` is
`400 Client Error: Bad Request for url: https://...api_key=<KEY>...`. It lands in the same handler,
which now discards that text unconditionally. Under redaction this ordering was a prerequisite with
a correctness argument attached; under decision #2 the handler is closed by construction for every
member of the `except` tuple, present and future.

### Consequences

Good: no credential reaches a run artifact, the LLM provider, or an owner email through the FRED
failure path — and the guarantee holds for exception types nobody has enumerated, because it does
not depend on inspecting the text. The `raise_for_status()` fix is safe to make in any order.

What this does **not** buy is decision driver #4. Nothing structurally prevents someone adding a new
handler that interpolates `{error}` into a `FetchError.reason`; the rule is still convention, enforced
by review and by the tests below, not by the type system. What changed is the *direction* of the
residual exposure. Under redaction, a handler written without knowledge of this ADR leaked by default,
and staying safe required actively adding a redactor call. Under decision #2, a handler written the
obvious way — name the subject, name the failure class — is safe by default, and leaking requires
actively reaching for the exception's text. Smaller surface, inverted default; not a closed one. A
genuinely structural fix (a `reason` type that cannot be constructed from an exception, or a
credential type whose plaintext never reaches `requests` as a string) was not attempted here.

Cost, stated plainly: `FetchError.reason` is now less informative than it was. An operator reading a
run artifact sees `FRED fetch failed for series CPILFESL: ConnectionError` and must go to the local
log for the URL, the retry count, and the underlying socket error. This is the trade accepted
deliberately — the artifact is the untrusted, widely-distributed copy; the log is the trusted, local
one. Detail is relocated, not discarded.

To watch: **this closes the sink, not the source.** The plaintext is still assembled into a URL
inside `requests`, so anything else that stringifies a request or response in that call stack — a
debug-level `requests`/`urllib3` logger, an APM integration, a future traceback handler that dumps
frame locals — reopens the exposure independently of this fix. The local log deliberately still
carries the full exception text, so a log shipped off-box is a leak; the log is trusted *because it
is local*, and that assumption is load-bearing. The durable version is keeping the credential out of
the query string entirely, which the FRED API does not offer. That residual risk is accepted
knowingly.

### Confirmation

Decision #1 is pinned offline by `tests/unit_tests/pipeline/test_direct_deterministic_tools.py`,
asserting the plaintext appears in neither `repr()` nor `vars()` of the instance.

Decision #2 is pinned on `fetch_fred_series` by asserting that a `requests.ConnectionError` carrying
the real key in its URL produces a `FetchError.reason` that holds the series id and the exception's
type name, and that it contains neither the plaintext key nor `"Max retries exceeded with url"`, a
distinctive fragment of the exception text. Note what that is and is not: a check that one chosen
fragment is absent, not a proof that no substring of the exception text survives — the universal claim
is not assertable, since short substrings of any message occur incidentally in a compliant reason. The
fragment is chosen to be one a `{error}`-built reason necessarily carries, so the old contract fails it.
A control test proves the seam still emits the key, so the "plaintext absent" assertions cannot go
vacuous, and a companion asserts the exception fragment does reach the *log*, so the diagnostic is
verifiably relocated rather than lost. Whether the log also holds the plaintext key is deliberately
unpinned: that exposure is accepted residual risk (see "To watch"), and asserting it would make a later
tightening of the local log fail a test that reads as defending the leak. The tests written against the
removed redactor (placeholder present, plaintext absent in `reason`) are superseded and rewritten.

Nothing is left unpinned pending `raise_for_status()`: the handler's behaviour is independent of which
exception type it caught, so one member of the `except` tuple witnesses the invariant for all of them.

One gap is open and named: **`fetch_ticker_price` has no tests at all.** Decision #2 extends the
invariant to it by assertion in this document only, so a future edit interpolating `{error}` into its
`FetchError.reason` would go unnoticed by the suite. The consequence is bounded — yfinance carries no
credential on that path — but the layer-wide claim in decision #2 is, today, pinned on one of its two
members.

## Considered Options (key rejections)

- **Leave the composition-time unwrap.** Rejected: it makes `SecretStr` decorative, and it was
  masking the query-string leak rather than preventing it.
- **Drop the exception text from `FetchError.reason` entirely.** Initially rejected on the grounds
  that it removes the leak by removing the diagnostic — **this is the option now adopted** (decision
  #2). The original rejection was wrong because it assumed the diagnostic had to travel with the
  propagating value. It does not: the full exception is retained in the local log, so the operator
  loses nothing they had, and only the copy that reaches disk-as-artifact, the LLM, and email is
  reduced.
- **Redact the secret's plaintext out of the message (`str.replace`).** Adopted in the first
  revision, now rejected — see decision #2a: silently defeated by percent-encoding, opt-in per call
  site, and generalises to nothing.
- **Regex-strip `api_key=[^&]*` from the message.** Rejected: keyed to one parameter name, silently
  incomplete. Superseded anyway — no message-rewriting approach is in play.
- **Redact centrally by installing a logging filter.** Rejected: it would cover `logger` but not
  `FetchError.reason`, which is the sink that actually reaches disk and the LLM. Under decision #2
  the log is the side deliberately left un-redacted, so this is doubly moot.

## More Information

**Filename divergence.** This file is named `0032-secretstr-held-to-point-of-use-and-redacted-in-errors.md`,
but the 2026-07-22 amendment *rejects* redaction (decision #2a); the title above is the accurate one. The
file is deliberately not renamed — inbound links from code comments, other ADRs, and commit messages point
at this path. Read the `-redacted-in-errors` suffix as a historical artefact of the first revision, not as
a description of the decision.

Related: ADR 0010 (pydantic-settings migration, which introduced `SecretStr` on these fields),
ADR 0011 (typed fetch results and swallow-site discipline — `FetchError.reason` is the channel this
ADR constrains), `src/money_pit/pipeline/orchestration.py`, `src/money_pit/pipeline/retrieval.py`,
`tests/integration_tests/fred_live/`.
