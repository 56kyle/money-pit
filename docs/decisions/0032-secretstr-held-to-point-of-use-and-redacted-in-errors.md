# Credentials stay `SecretStr` to the point of use, and are redacted from error text

- Status: accepted
- Date: 2026-07-21
- Deciders: owner, python-dev, python-reviewer

## Context and Problem Statement

`Config.fred_api_key` and `Config.brave_api_key` are `SecretStr` (`config.py:87-88`), but
`run_pipeline` called `.get_secret_value()` on both at composition time and handed plain `str` to
`_DirectDeterministicTools` and `_DirectOpenEndedTools`. Those objects live for the whole run, so
the protection `SecretStr` exists to give — no accidental exposure through `repr()`, logging, or a
traceback that captures frame locals — was discarded at the moment the pipeline composed.

Moving the unwrap to the point of use fixed that, and immediately exposed a second, worse problem
that the composition-time unwrap had been masking. The FRED key is a **query parameter**, and
`requests` embeds the full request URL — query string included — in its connection-layer exception
text:

```
HTTPSConnectionPool(...): Max retries exceeded with url:
/fred/series/observations?series_id=CPILFESL&api_key=<THE REAL KEY>&file_type=json
```

`fetch_fred_series` interpolated that exception into two sinks, and one of them is not terminal:
`FetchError.reason` flows into `retrieval.py:125-133`, which builds `f"Data fetch error: {reason}"`
into `Answer.answer` — persisted as a run artifact, sent to the LLM provider, and reachable by the
owner-email path.

So the pre-existing exposure was passive (needs a debugger or a memory dump); the exposure a
naive "hold `SecretStr` longer" change leaves behind is **active** — the operator's key written to
disk and shipped to a third party on the single most likely failure, a network blip.

## Decision Drivers

- `SecretStr` should mean something at runtime, not just in the config type.
- A credential must never reach a durable sink: a log file, a run artifact, an outbound request
  body, or an email.
- Diagnostics must survive redaction — an operator debugging a failed fetch still needs to know why.
- The fix must not depend on remembering to apply it at each new call site.

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

**2. Exception text that could embed a credential is redacted before it reaches any sink.**
`_error_text_with_secret_redacted(error, secret)` returns `str(error)` with every occurrence of the
secret's plaintext replaced by `_REDACTED_SECRET_PLACEHOLDER`. Both sinks in the
`fetch_fred_series` failure handler consume the redacted string.

Redaction is `str.replace` on the secret's value, deliberately **not** a regex over
`api_key=[^&]*`. A pattern keyed to one parameter name silently misses the credential the moment it
appears in another position, another parameter, or a differently-shaped upstream message; replacing
the known plaintext is position- and name-independent.

An empty secret returns the text unchanged — `"".replace` would splice the placeholder between
every character and destroy the diagnostic. An empty key is a config defect, not a leak, so it fails
toward a readable error.

**3. The key is bound to a local before the `None` guard.** `fetch_fred_series` reads
`self._fred_api_key` once into `fred_api_key`, checks that, and uses the local for both the request
and the redaction. The class is mutable, so narrowing on the attribute would not survive the
intervening `try` block; the guarantee that redaction always has a key is now structural rather than
inferred.

**4. Brave is deliberately not redacted.** Its token is an `X-Subscription-Token` **header**, which
does not appear in `requests` exception text, and `brave_search`'s only interpolated URL params are
`q` and `count`. It is also structurally safer: `brave_search` returns `[]` on failure, so its error
text reaches `logger` only — never a `FetchError.reason`, and so never `Answer.answer`. If Brave ever
moves its token to a query parameter, decision #2 applies to it immediately.

**5. Redaction is a prerequisite for adding `raise_for_status()`, not independent of it.**
`fetch_fred_series` does not call `raise_for_status()` today, so an invalid key parses as `NoData`
rather than `FetchError` — a known defect deferred to the queued refactor of this module. Adding it
raises `requests.HTTPError`, a `RequestException`, whose `str()` is
`400 Client Error: Bad Request for url: https://...api_key=<KEY>...`. It lands in this same handler.
**Fixing the status check before the redaction would have amplified the leak**, in precisely the
scenario where the key is most likely wrong-but-real. The redaction covers that case unchanged.

### Consequences

Good: no credential reaches a log, a run artifact, the LLM provider, or an owner email through the
FRED failure path; diagnostics survive intact; the redactor takes the exception rather than a
pre-stringified message, so no call site holds an un-redacted `str(error)` in a local; the future
`raise_for_status()` fix is now safe to make.

Bad / to watch: **this closes the sink, not the source.** The plaintext is still assembled into a
URL inside `requests`, so anything else that stringifies a request or response in that call stack —
a debug-level `requests`/`urllib3` logger, an APM integration, a future traceback handler that dumps
frame locals — reopens the exposure independently of this fix. The durable version is keeping the
credential out of the query string entirely, which the FRED API does not offer. That residual risk
is accepted knowingly. Redaction is also opt-in per call site: a new sink that interpolates a raw
exception in this module reintroduces the leak, and only the helper's name guards against it.

### Confirmation

`nox -s tests-python` stays green. Decisions #1 and #2 are pinned offline by
`tests/unit_tests/pipeline/test_direct_deterministic_tools.py`: a `requests.ConnectionError` carrying
the real key in its URL is asserted to reach both sinks — the log record and `FetchError.reason` —
with the placeholder present and the plaintext absent, alongside a control test proving the seam
still emits the key so those assertions cannot go vacuous. Decision #1's run-lifetime claim is pinned
separately by asserting the plaintext appears in neither `repr()` nor `vars()` of the instance.

What remains unpinned is decision #5. Until `raise_for_status()` lands there is no arm of this handler
that both carries the credential and is reachable offline other than the connection error, so "every
member of the `except (RequestException, ValueError)` tuple leaves through the redactor" is asserted
for one member only; the `ValueError` arm is covered for its return value, not for redaction, because a
JSON-decode message never contains the key. The test that pins `HTTPError` redaction is owed by the
change that adds the status check.

## Considered Options (key rejections)

- **Leave the composition-time unwrap.** Rejected: it makes `SecretStr` decorative, and it was
  masking the query-string leak rather than preventing it.
- **Drop the exception from `FetchError.reason` entirely.** Rejected: it removes the leak by
  removing the diagnostic, leaving an operator with "FRED fetch failed" and no cause.
- **Regex-strip `api_key=[^&]*` from the message.** Rejected under decision #2 — keyed to one
  parameter name, silently incomplete.
- **Redact centrally by installing a logging filter.** Rejected: it would cover `logger` but not
  `FetchError.reason`, which is the sink that actually reaches disk and the LLM.

## More Information

Related: ADR 0010 (pydantic-settings migration, which introduced `SecretStr` on these fields),
ADR 0011 (typed fetch results and swallow-site discipline — `FetchError.reason` is the channel this
ADR constrains), `src/money_pit/pipeline/orchestration.py`, `src/money_pit/pipeline/retrieval.py`,
`tests/integration_tests/fred_live/`.
