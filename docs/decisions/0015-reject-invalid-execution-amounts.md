# Reject non-finite and below-minimum notional amounts before the order boundary

- Status: accepted
- Date: 2026-07-12
- Deciders: owner, python-dev

## Context and Problem Statement

`build_execution_params` (`compute/execution_params.py`) is the single boundary that turns a
post-processor `dollar_amount: float` into the `notional` value that moves real capital. ADR 0014
reconciled the emitted payload to the pinned `place_stock_order` schema by typing `notional` (and
`qty`) as **string-or-null** and emitting `notional` as a cents-formatted string
(`f"{dollar_amount:.2f}"`).

That string typing removed a safety property that used to be implicit. When `notional` was a JSON
number, `jsonschema.validate` rejected a non-finite value structurally — `NaN`/`Infinity` are not
valid JSON numbers. Now that the schema field is a **string**, that check is gone:
`f"{float('inf'):.2f}"` is `"inf"` and `f"{float('nan'):.2f}"` is `"nan"` — both are valid strings
under the schema, so `jsonschema.validate` passes them. A `NaN`, `inf`, zero, negative, or sub-cent
`dollar_amount` would therefore format into a well-typed-but-nonsensical order string and clear the
one schema gate on the capital path, only to be rejected (at best) by Alpaca **after** the payload
left the choke point — or, at worst, interpreted unpredictably.

A `dollar_amount` this degenerate signals an upstream sizing bug (a divide-by-zero in Kelly, a
missing snapshot value coerced to `NaN`, a rounding-to-zero). The money boundary must refuse it
loudly at the boundary, not pass it downstream. Governing principle: **fail closed where capital is
at stake, and reject an invalid state as early as it can be detected.**

## Decision Drivers

- An invalid capital amount must never reach the order payload — this is the money choke point.
- ADR 0014's string typing means `jsonschema` no longer rejects non-finite/absurd amounts; the
  numeric-validation guarantee that typing used to provide must be reinstated explicitly.
- Reject before any I/O (before `load_order_schema`), so the cheapest, earliest failure wins and no
  schema read happens on a doomed call.
- Failure is part of the function's contract: a typed error the caller can catch, not a bare
  `ValueError` or a silently-formatted bad string (the Rust-style explicit-failure bar).
- No new magic number: the minimum is derived from the cents-formatting decision already made in
  ADR 0014, not an independently-chosen literal.

## Considered Options

- **No guard — rely on the schema / Alpaca to reject the amount.**
- **Guard with a bare `assert`.**
- **Raise a typed `InvalidExecutionAmountError` before schema load.**

## Decision Outcome

Chosen: **a typed `InvalidExecutionAmountError`, raised before the schema is loaded**, for any
`dollar_amount` that is not finite or is below the minimum representable notional.

```python
_NOTIONAL_DECIMAL_PLACES: int = 2
_MIN_NOTIONAL_DOLLARS: float = 10 ** -_NOTIONAL_DECIMAL_PLACES   # one cent

if not math.isfinite(dollar_amount) or dollar_amount < _MIN_NOTIONAL_DOLLARS:
    raise InvalidExecutionAmountError(...)
schema = load_order_schema(schema_path)
```

- `math.isfinite` rejects `NaN` and `±inf` in one check.
- The floor is `_MIN_NOTIONAL_DOLLARS = 10 ** -_NOTIONAL_DECIMAL_PLACES` — one cent — derived
  directly from ADR 0014's `_NOTIONAL_DECIMAL_PLACES`, so the smallest accepted order is exactly the
  smallest amount the cents-formatting can faithfully represent. A sub-cent notional is meaningless
  (it would format to `"0.00"`), and zero / negative amounts are rejected by the same lower bound.
- The guard sits **before** `load_order_schema`, so an invalid amount fails without touching the
  filesystem and independently of whether the schema is pinned, missing, or malformed.

### Consequences

Good: the capital boundary refuses a degenerate amount loudly and early, with a typed error naming
the amount; the numeric-sanity guarantee that ADR 0014's string typing removed is restored
explicitly; the minimum is a single named constant tied to the existing formatting decision, not a
new magic number. An upstream sizing bug surfaces here as a clear failure rather than as a strange
order or a late broker rejection.

Bad / to watch: the floor is a hard one-cent minimum. If a future path legitimately needs
fractional-cent internal accounting, this bound and ADR 0014's cents formatting move together — both
are localized to the two named constants at this one boundary, so that revisit stays single-site.
The guard covers the `notional` path only; a future share-sized (`qty`) path will need its own
amount/precision guard at its own boundary.

### Confirmation

Failure-mode tests in `tests/unit_tests/compute/test_execution_params.py` pin the guard: a `NaN`,
`inf`, zero, negative, and sub-cent `dollar_amount` each raise `InvalidExecutionAmountError`, and a
test asserts the amount check fires **before** the schema load (an invalid amount raises
`InvalidExecutionAmountError` even when pointed at a missing schema path, proving the ordering). The
happy path confirms `750.00` → `"750.00"` and that the emitted payload validates against the real
pinned schema.

## Considered Options (key rejections)

- **No guard — let the schema or Alpaca reject it.** Rejected: after ADR 0014 the schema field is a
  string, so `jsonschema.validate` accepts `"nan"` / `"inf"` / `"0.00"`; the invalid payload would
  clear the only local gate and depend on the broker rejecting it — a check that happens after the
  value has already left the money choke point, which the fail-closed posture forbids.
- **`assert` the amount.** Rejected: assertions are stripped under `python -O`, so the guard on the
  capital path would silently vanish in an optimized deployment. A capital-safety check must not
  depend on assertion mode.

## More Information

Directly motivated by ADR 0014 (order payload reconciled to the pinned schema — the string typing
that removed `jsonschema`'s implicit numeric rejection) and shares the fail-closed bar of ADR 0003 /
ADR 0007. Related: `src/money_pit/compute/execution_params.py`,
`src/money_pit/schemas/action_steps.py`, `docs/pipeline_contracts.md` §5.
