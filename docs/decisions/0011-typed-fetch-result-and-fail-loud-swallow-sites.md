# Deterministic fetches return a typed result; silent swallow sites narrow and log

- Status: accepted
- Date: 2026-07-06
- Deciders: owner, python-dev

## Context and Problem Statement

The deterministic research fetches — `_DirectDeterministicTools.fetch_fred_series` and
`fetch_ticker_price` in `pipeline/orchestration.py` — returned `float | None`. That signature
conflates two outcomes a capital-allocating pipeline must never merge: a series or ticker that
legitimately has **no observation**, and a fetch that **errored** (network outage, unparseable
response, missing credential). Both collapsed to `None`, so an outage was indistinguishable from
absence: the consumer stamped the same "Data unavailable." answer, derived the same `LOW`
confidence, and left no loud signal that the datum was unknown rather than genuinely empty.

Compounding this, every fetch wrapped its body in `except Exception: return None`. A genuine code
bug (`TypeError`, `AttributeError`) was swallowed identically to an expected `requests` failure,
so a broken fetch degraded silently to "no data" and flowed downstream into sizing and the audit
record.

The same swallow-and-hide shape recurred at four other sites: `brave_search` / `edgar_search`
(open-ended retrieval), `determination._read_journal_outcome` (a corrupt execution journal — a
capital-critical artifact — silently read as an absent outcome), and `alpaca_portfolio._resolve_sector`
(best-effort enrichment). None distinguished an expected empty from a swallowed bug, and none logged.

Governing principle (operating ethos, "Error handling"): failure modes are part of a function's
contract, not a silent property of its implementation; expected-empty and code-broke must be
distinct, and genuine bugs must propagate.

## Decision Drivers

- An outage must be **loud and distinct** from legitimate absence, both in logs and in the `Answer`.
- Genuine bugs (`TypeError`/`AttributeError`) must **propagate**, never be caught by a data fetch.
- The node-level soft-fail contract stays intact: a fetch failure degrades one answer, it does not
  crash the retrieval node.
- The result type must be importable by both the protocol (`agents/`) and the consumer (`pipeline/`)
  without inverting the dependency direction (agents is lower-level than pipeline).
- No magic values; failure surfaced in the one-line docstring per the ethos.

## Decision Outcome

**1. A closed three-variant union, `FetchResult`, in `schemas/fetch_result.py`.** The result is the
sum type `FetchValue | NoData | FetchError`, each a frozen pydantic model
(`ConfigDict(frozen=True, extra="forbid")`, matching the schema layer). `FetchValue` carries the
`float`; `NoData` is the legitimately-empty variant (no fields); `FetchError` carries a `reason: str`.
Modelling the outcomes as distinct types rather than one model with a discriminator enum makes the
invalid states (`kind=VALUE` with no value) unrepresentable — the Rust-`enum` shape. It lives in
`schemas/` because both the protocol in `agents/research_tools.py` and the consumer in
`pipeline/retrieval.py` already depend on `schemas/`, so no new coupling or cycle is introduced;
`schemas/` is the shared value-type layer even though this particular type is never serialized.

**2. The fetch protocol and every implementer return `FetchResult`.** `DeterministicResearchTools`,
`_DirectDeterministicTools`, and `_Phase4DeterministicTools` (the stub, which returns `NoData()`) all
adopt the union. `mcp.clients` does **not** implement these methods, so no ripple there.

**3. Each fetch narrows its catch to the expected classes and logs loudly.** FRED catches
`requests.RequestException` and `ValueError` (JSON decode / `float()` parse), guards `response.json()`
against a non-dict rather than risking an `AttributeError`, and logs at `WARNING`. A missing FRED API
key returns `FetchError` (not `NoData`): we could not obtain the datum, and it is not legitimately
absent — the honest, loud classification. The ticker path catches `requests.RequestException` and
`OSError` (the yfinance network/OS surface). Any other exception is treated as a bug and propagates.

**4. The consumer distinguishes all three variants in the `Answer`.** `retrieval._deterministic_answer`
pattern-matches: `FetchValue` stamps the honest source token and real `data_retrieved`; `NoData`
yields "Data unavailable."; `FetchError` logs at `ERROR` (with the `question_id` the fetch layer
lacks) and emits a distinct "Data fetch error: {reason}" answer with a matching `limitations`. Both
non-value variants derive `LOW` confidence from empty `sources_used`, so an outage never masquerades
as retrieved data.

**5. The remaining swallow sites narrow and log, keeping their return shape.** `brave_search` /
`edgar_search` catch `requests.RequestException` (+ `OSError` for edgar), log at `WARNING`, and keep
their `list[str]` return; the dead `try` around pure dict traversal is removed so a bug there
propagates. `determination._read_journal_outcome` keeps its narrow `ValidationError` catch and its
`None` return (the finalizer already handles an absent outcome) but now logs at `ERROR` — a corrupt
journal is capital-critical and must not be silent. `alpaca_portfolio._resolve_sector` catches the
yfinance network/OS/`KeyError`/`ValueError` surface, logs at `DEBUG` (best-effort enrichment), and
keeps the `unknown` fallback; it is deliberately kept slightly broader than the other paths because
it runs inside the capital-critical portfolio snapshot, where a yfinance quirk must not crash the
whole fetch.

**6. The `signal_source` field migrated from `str` to `str | None`** on `Question` (`schemas/questions.py`)
and `Answer` (`schemas/answers.py`), replacing the `"none"` string sentinel with a typed absence. The
no-overlap portfolio-gap placeholder in `pipeline/questions.py` now emits `signal_source=None` directly,
and the consumers guard on `is None` before the `INDICATOR_PREFIX` startswith check
(`pipeline/retrieval.py`, `pipeline/analysis.py`) rather than reasoning about a magic string. This is the
same "make ambiguous states unrepresentable" theme as the `FetchResult` union: `None` is unambiguously
"no signal," where the string `"none"` was indistinguishable from a malformed real source token. The A2
draft still carries `signal_source: str` (`schemas/question_draft.py`); only the run-facing `Question`/`Answer`
contract and its no-signal producer changed.

### Consequences

Good: an outage is now loud (logged) and structurally distinct from absence, at both the fetch and
answer layers; bugs propagate instead of degrading to "no data"; sizing and audit key off honest
provenance. The union is a small, closed, exhaustively-matchable contract that a test can assert
against by variant type rather than by message text.

Bad / to watch: the missing-FRED-key case now surfaces a `FetchError` on every macro-regime question
when the key is unconfigured (common offline), which is louder than the prior silent `None`. This is
intended, but Wave 2's config migration should make key absence a startup-time concern rather than a
per-question one. The `signal_source` `str → str | None` migration (Outcome 6) removes the `"none"`
string sentinel; consumers that once compared against that string now branch on `is None`.

### Confirmation

python-test-writer pins: a bug-shaped fetch input propagates (no swallow); an expected `requests`
failure returns `FetchError` and logs; an empty upstream returns `NoData`; the consumer produces
three distinct answers/log levels by variant; the corrupt-journal read logs at ERROR and returns
`None`. The existing `test_retrieval` unit tests that asserted the old `float | None` /
`dict | None` shapes need re-pinning to the union.

## Considered Options (key rejections)

- **One model with a `kind` enum + optional `value`/`reason`.** Rejected: permits invalid states
  (`VALUE` with no value); the distinct-type union makes them unrepresentable.
- **Frozen dataclasses instead of pydantic models.** Reasonable (no serialization here), but the
  `schemas/` layer is uniformly pydantic-frozen; consistency won.
- **Missing FRED key → `NoData`.** Rejected: it implies the series genuinely has no observation; the
  datum is unknown, so `FetchError` is the honest classification.
- **Keep `float | None`, just log in the consumer.** Rejected: the consumer cannot recover the
  error-vs-absent distinction once it has been collapsed to `None` at the fetch boundary.

## More Information

Wave 1a of the observation-pass remediation (`please-read-through-the-soft-wolf.md`). Reconciles with
the documented agent-failure contract (A2/A3 soft-empty is by design — the fix is a log line, not a
raise; the orchestration data-fetches stop catching bugs). Provenance note: the portfolio-gap price
token was audited and found already honest — `_PORTFOLIO_GAP_SOURCE = YFINANCE_MCP` matches the
actual `fetch_ticker_price` (yfinance) source and the `pipeline_contracts.md` routing table
(`portfolio_gap → yfinance_mcp`, medium confidence), so no relabel was needed. Related:
`src/money_pit/schemas/fetch_result.py`, `src/money_pit/agents/research_tools.py`,
`src/money_pit/pipeline/orchestration.py`, `src/money_pit/pipeline/retrieval.py`,
`src/money_pit/pipeline/determination.py`, `src/money_pit/alpaca_portfolio.py`. The `signal_source`
migration (Outcome 6) spans `src/money_pit/schemas/questions.py`, `src/money_pit/schemas/answers.py`,
`src/money_pit/pipeline/questions.py`, `src/money_pit/pipeline/retrieval.py`, and
`src/money_pit/pipeline/analysis.py`.
