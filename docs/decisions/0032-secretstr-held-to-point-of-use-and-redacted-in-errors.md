# Credentials stay `SecretStr` to the point of use, and upstream exception text never leaves the local log

- Status: accepted (amended 2026-07-22 — redaction rejected, superseded by structural closure of the sink;
  amended 2026-07-22 — decision #1 widened from the FRED/Brave keys to every resolved credential;
  amended 2026-07-23 — decision #5's `raise_for_status()` landed and is now pinned)
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

**1. Credential material stays `SecretStr` until the expression that transmits it.** This is a rule
about **every resolved credential in the system**, not about the two keys that first motivated it. No
resolver returns plaintext, no object holds plaintext for the lifetime of a run, and
`.get_secret_value()` appears only inside the expression that hands the value to the transport.

Covered today:

| Credential | Held as | Unwrapped inline at |
| --- | --- | --- |
| FRED key | `_DirectDeterministicTools._fred_api_key: SecretStr \| None` | the `requests` params dict |
| Brave key | `_DirectOpenEndedTools._brave_api_key: SecretStr \| None` | the `X-Subscription-Token` header |
| Alpaca secret key | `AlpacaCredentials.secret_key: SecretStr` | `_write_env`'s `ALPACA_SECRET_KEY` value, and the `TradingClient(...)` construction in `alpaca_orders` and `alpaca_portfolio` |
| Gmail app password | the `make_gmail_email_sender` closure's `app_password: SecretStr` | the `server.login(...)` call |

The Alpaca secret key is the reason this widening was not optional. It is the capital-moving
credential, it was a plain `str` on a `@dataclass(frozen=True)` with the default `repr`, and that
dataclass is held for the whole run inside `AlpacaWriteDeps` — itself a default-`repr` dataclass
reachable from `PipelineOverrides.place_order`, the portfolio fetcher, the fill observer, and
`live_manifest`. Any `repr()` of the overrides, or any traceback that captured frame locals anywhere
on that path, printed it. The Gmail app password had the same shape one layer in: resolved as `str`
and closed over for the process lifetime.

Two deliberate exclusions, both scope boundaries rather than oversights:

- **`AlpacaCredentials.api_key` stays `str`.** Under this project's keyring convention it is the
  Alpaca API *key id* — the keyring *username*, the identifier under which the secret is stored. It is
  an identifier, not secret material, and it is already interpolated into `CredentialResolutionError`
  messages as the thing an operator needs in order to fix a missing-secret failure. Masking it would
  make those messages useless and would misrepresent what `SecretStr` means on the other fields.
- **`src/email_server/server.py` stays as-is.** It is a separate process reading
  `MONEY_PIT__GMAIL_APP_PASSWORD` from its own environment at its own entry point. It does not go
  through `resolve_gmail_app_password`, and pulling it into this rule would be a change to a different
  deployment unit's boundary handling.

`CredentialResolutionError` messages were re-checked under the new types: all four interpolate only
service names and usernames (`alpaca_service`, `alpaca_username`, `gmail_service`, `gmail_address`),
never the resolved secret. No change was needed, and per decision #2a nothing is redacted.

Scope claim, stated precisely: this removes the plaintext from a **run-lifetime attribute**. It does
not make the plaintext transient in the absolute sense. `requests` assembles the FRED key into a URL
and retains it on the `PreparedRequest`; `alpaca-py`'s `TradingClient` stores the secret it is
constructed with; `_write_env` produces a plaintext `dict[str, str]` that is handed to a subprocess
environment; `smtplib` holds what it was passed for the duration of the AUTH exchange. Each resolver
also binds one plaintext local between `keyring.get_password` and the `SecretStr(...)` wrap, which is
unavoidable while `keyring` returns `str`. The honest statement is "no longer held for the lifetime of
the run," not "exists only as a transient expression."

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

**The rule is not fetch-layer-only: it covers the email path too.** `make_gmail_email_sender`'s send
seam has the same shape — `smtplib` exception text interpolated into `EmailSendError`, which
`with_undelivered_record` writes verbatim into the `Reason:` field of an undelivered-email artifact in
the run's working directory. That is a durable sink reachable by the same argument as
`FetchError.reason`, on a path decision #1 now covers (the Gmail app password). So `send_email` logs
the full exception via `logger.warning` and raises an `EmailSendError` carrying only the subject, the
recipient, and `type(error).__name__`. This is not a known leak today — `smtplib` does not echo the
AUTH payload into `SMTPAuthenticationError` — but the invariant is worth more as a property of every
untrusted-text-into-durable-artifact sink than as a fetch-layer special case, and it would be
inconsistent to close one and leave the other open. As with the fetch layer, detail is relocated to
the local log, not discarded.

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
`fetch_fred_series` did not call `raise_for_status()`, so an invalid key parsed as `NoData` rather
than `FetchError` — a known defect that violated both this module's own docstring ("`FetchError` on
an upstream or config failure") and the `FetchResult` contract (`schemas/fetch_result.py`), which
forbids conflating a failure to retrieve with a legitimately-empty response. Adding it raises
`requests.HTTPError`, a `RequestException`, whose `str()` is
`400 Client Error: Bad Request for url: https://...api_key=<KEY>...`. It lands in the same handler,
which now discards that text unconditionally. Under redaction this ordering was a prerequisite with
a correctness argument attached; under decision #2 the handler is closed by construction for every
member of the `except` tuple, present and future.

**Landed 2026-07-23.** The single line `response.raise_for_status()` now sits in `fetch_fred_series`
after `requests.get(...)` and before `response.json()`, so a bad HTTP status fails closed as a
redacted `FetchError` rather than a silent `NoData`. This was executing an already-recorded decision,
not a new one, so no separate ADR was cut. See the Confirmation section for the offline test that
pins the 400 → `FetchError` arm.

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

Decision #1 is pinned offline, one test per credential-holding object, each asserting the plaintext is
absent from the run-lifetime state of the thing that holds it:

- `tests/unit_tests/pipeline/test_direct_deterministic_tools.py` — `repr()` and `vars()` of the
  `_DirectDeterministicTools` instance.
- `tests/unit_tests/test_config.py` — `repr()` and `vars()` of the `AlpacaCredentials` returned by
  `resolve_alpaca_credentials`, having first asserted `get_secret_value()` round-trips the keyring
  value so the absence assertions cannot pass on an empty credential.
- `tests/unit_tests/mcp/test_clients.py` — `repr()` and `vars()` of `AlpacaWriteDeps`, which is the
  object that actually survives the run and is reachable from `PipelineOverrides`.
- `tests/unit_tests/test_email_sender.py` — the closure cells of the returned `EmailSender`, and
  deliberately *not* its `repr()` or `vars()`. A function's `repr` never shows closed-over values and
  its `__dict__` is empty, so an assertion against either would pass unconditionally — vacuous inside a
  security test, and worse than no assertion because it reads as coverage. `__closure__` is where a
  plaintext copy would actually survive. A companion control test,
  `test_make_gmail_email_sender_transmits_the_app_password_plaintext`, asserts the plaintext still
  reaches `SMTP.login`, so the absence assertion cannot go vacuous either.

Pinned but not by an absence assertion: `_write_env`'s output is asserted to *equal* the secret (it
must — it is the transmitting expression). Note precisely what that assertion is against: a test-local
sentinel constant, `_SENTINEL_SECRET_KEY`, constructed into the `AlpacaCredentials` fixture by the test
itself — never the plaintext of a credential resolved from a keyring.

Not pinned: the `TradingClient` constructions in `alpaca_orders` / `alpaca_portfolio`, which are
unreachable offline, so their inline unwrap is asserted in this document only.

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

Decision #2 on the email path is pinned in `tests/unit_tests/test_email_sender.py` by the same
three-part shape: a fake SMTP whose `send_message` raises with a distinctive text fragment, then an
assertion that the undelivered-email artifact's `Reason:` line names `SMTPException` and that the
fragment is absent from the artifact, plus a companion asserting the fragment does reach the local
`logger` — so the diagnostic is verifiably relocated, not lost.

Nothing is left unpinned pending `raise_for_status()`: the handler's behaviour is independent of which
exception type it caught, so one member of the `except` tuple witnesses the invariant for all of them.
Decision #5 now has its own positive witness — `test_fetch_fred_series_with_error_status_returns_fetch_error`
drives a real 400 `Response` through production's `raise_for_status()` and asserts the resulting
`FetchError.reason` names the series id and `HTTPError` while carrying neither the plaintext key nor the
`for url:` fragment, with a seam control proving the raised `HTTPError` really embeds the key.

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
ADR constrains), ADR 0008 (the typed, fail-closed `resolve_alpaca_credentials` whose return type
decision #1 now constrains), ADR 0013 (`AlpacaCredentials.paper`, untouched by this amendment —
paper/live routing behaviour is identical), `src/money_pit/pipeline/orchestration.py`,
`src/money_pit/pipeline/retrieval.py`, `tests/integration_tests/fred_live/`.
