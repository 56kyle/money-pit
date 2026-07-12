# Order payload reconciled to the pinned Alpaca schema: string qty/notional, cents-formatted notional

- Status: accepted
- Date: 2026-07-12
- Deciders: owner, architecture author

## Context and Problem Statement

ADR 0007 pinned the order-schema gate to a committed artifact and defined the terminus of that work:
when the real OpenAPI-generated `place_stock_order` inputSchema was pinned from the live Alpaca MCP
server, the sentinel-driven fail-closed machinery would be retired. That pinning event has now happened
— `src/money_pit/mcp/alpaca_order_schema.json` is the real tool schema.

The real schema is materially different from the stub the code was written against:

- Its key is **`qty`**, not `quantity`.
- `qty`, `notional`, and every price field are typed `{"anyOf": [{"type": "string"}, {"type": "null"}]}`
  — **string or null**, never numbers.
- It carries `"additionalProperties": false` and `"required": ["symbol", "side"]`.

The emitted order payload is now **invalid** against the real schema, confirmed by the paper-trade e2e
failing with `10000.0 is not valid under any of the given schemas`. `ExecutionParameters` emitted
`notional`/`quantity` as **floats** under the key **`quantity`**: wrong key, wrong type. Because this is
the payload that moves real capital, an invalid payload is a fail-closed condition, not a cosmetic one.

Governing principle (`ExecutionParameters`'s own docstring): **keys must match the official inputSchema
literally.** The model had drifted from its own stated contract.

## Decision Drivers

- The emitted payload must validate against the real pinned schema — this is the money choke point.
- `ExecutionParameters` must mean what its docstring says: field names and types mirror the schema
  literally, so the model holds the exact values sent to Alpaca with no downstream remapping.
- The `quantity → qty` transitional shim (ADR 0007) exists only until this reconciliation; its terminus
  is now met and it must be deleted rather than left as a permanent silent remap.
- A single source of truth: the payload the post-processor validates must be byte-for-byte the payload
  the write client submits.

## Considered Options

- **Coerce types/keys inside `to_order_payload` while leaving fields as floats named `quantity`.**
- **Rename to `qty` and type as `str | None`, holding the exact submitted strings in the model.**
- **Type `notional` as `Decimal` and stringify at emission.**

## Decision Outcome

Chosen: **rename `quantity → qty`, type both `qty` and `notional` as `str | None`, and hold the exact
submitted strings in the model.** This is the only option that makes the model literally mirror the
schema — the value in the field is the value on the wire, so there is no emission-time transform that
could diverge from what was validated.

- `ExecutionParameters.notional: str | None` and `ExecutionParameters.qty: str | None` (was
  `notional: float | None`, `quantity: float | None`).
- `to_order_payload` emits `symbol`, `side`, `type`, `time_in_force`, `client_order_id`, and — when not
  None — `notional` / `qty`, all as the stored strings. No `"quantity"` key exists anywhere.
- The string boundary conversion happens in `build_execution_params`, which receives
  `dollar_amount: float` and formats it once at that boundary.

### Notional formatting: fixed 2-decimal (cents) string

`notional` is a dollar amount, formatted as `f"{dollar_amount:.2f}"` (`10000.0` → `"10000.00"`) via a
named module constant `_NOTIONAL_DECIMAL_PLACES = 2` in `compute/execution_params.py`.

Rationale: a sub-cent notional order is meaningless — the smallest unit of a dollar-denominated order is
one cent — so cents precision is the correct, complete representation. Fixed 2-decimal formatting also
gives a stable, canonical string (no float-repr artifacts like `"10000.000000001"` or scientific
notation), which is what a string-typed schema field wants. `qty` (share count) is a separate concern
and is not produced by the current notional-order path (`qty=None`); a future share-sized path will make
its own precision decision at that boundary.

This is the reviewer-flagged decision in this change. It is deliberately localized to one named constant
at one boundary so a future revisit (e.g. moving to `Decimal` if fractional-cent internal accounting
ever appears upstream) is a single-site change.

### Deleting the ADR-0007 shim

`mcp/clients._order_arguments` — the `quantity → qty` remap — is deleted, and its caller
(`AlpacaWriteDeps.__call__`) now submits `params.to_order_payload()` directly. The shim's stated
terminus ("rename `ExecutionParameters.quantity → qty` during the deferred pin-order-schema
reconciliation, after which this remap is deleted") is met by this change. This closes that stepping
stone: the write client no longer transforms the payload, so what is validated is exactly what is
submitted.

### Consequences

Good: the emitted payload validates against the real schema and the paper-trade execute path is
unblocked; `ExecutionParameters` again means what its docstring claims; the write path is transform-free
so validation and submission cannot diverge; the ADR-0007 shim's terminus is honored rather than
becoming permanent.

Bad / to watch: markdown renderers in `pipeline/analysis.py` that formatted `notional` as a float
(`f"${notional:.2f}"`) now render the preformatted string (`f"${notional}"`); this is correct given the
value is already cents-formatted, but the presentation is now sourced from the same string that goes to
Alpaca rather than reformatted independently. Separately, this reconciliation is downstream of the
schema-pinning event that ADR 0007's strict-xfail tripwire and per-test sentinel opt-ins were designed
to force; retiring that machinery is ADR 0007's terminus and is left to the test/tripwire pass
(python-test-writer), not this payload change.

### Confirmation

`build_execution_params(...)` for a notional order returns params whose `to_order_payload()` validates
against the real pinned schema — verified: `10000.0` emits
`{"symbol": "SPY", "side": "buy", "type": "market", "time_in_force": "day",
"client_order_id": "…", "notional": "10000.00"}` with `notional` a `"NNNN.NN"` string, key `notional`,
no `quantity` key and no float value, and `jsonschema.validate` (run inside `build_execution_params`)
raising nothing. Failure-mode and construction-site tests are pinned by python-test-writer.

## More Information

Closes the terminus of ADR 0007 (unpinned order schema fails closed) — that ADR's shim and pin-time
tripwire were explicitly scoped to end at this reconciliation. Related:
`src/money_pit/schemas/action_steps.py`, `src/money_pit/compute/execution_params.py`,
`src/money_pit/mcp/clients.py`, `src/money_pit/pipeline/analysis.py`,
`src/money_pit/mcp/alpaca_order_schema.json`.
