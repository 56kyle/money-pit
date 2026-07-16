# SELL/TRIM exit sizing — quantity exits and Kelly-target trims

- Status: accepted
- Date: 2026-07-16
- Deciders: owner, python-dev

## Context and Problem Statement

`design_decisions.md` §2/§4 specify exits: **SELL** = full exit, closed by the snapshot's held **quantity**
(to avoid fractional dust); **TRIM** = reduce by **notional** (dollars to remove), toward the lower Kelly
target implied by the weakened thesis. §4 was marked RESOLVED but flagged the quantity path as "not yet
built".

The post-processor routed SELL/TRIM through the **entry** sizer (`size_position`): they received the EV gate
+ Kelly and an entry-sized notional bearing **no relationship to the held position**, and
`build_execution_params` hardcoded `notional=<dollars>, qty=None` for every action. Two concrete defects:

- **A SELL whose thesis EV fell below `ev_gate` was silently dropped** — the model said "exit" and nothing
  happened. A capital-safety bug (you stay in a position you decided to leave).
- A SELL/TRIM naming a **non-held** ticker sailed through unguarded to an eventual broker rejection.

The rest of the stack already supported a `qty` order (the `ExecutionParameters.qty` field,
`to_order_payload()`, the pinned `alpaca_order_schema.json`, the executor, the OrderPlacer). Only two points
imposed notional-only behavior: `build_execution_params` and the exit branch of `_materialize_action_steps`.

## Decision Drivers

- Faithfulness to the §2/§4 spec, which is explicit about SELL-by-quantity and TRIM-toward-Kelly-target.
- Capital safety: an exit decision must actually exit; the EV gate is an **entry** go/no-go and must not
  suppress exits.
- Fail-soft on a bad exit thesis (non-held ticker): drop it, don't HALT the run.

## Decision Outcome

1. **SELL = full exit by held quantity.** The exit branch looks the instrument up in a case-insensitive
   held-position index and emits a **quantity** order (`build_quantity_execution_params`, `qty` = the held
   `Position.quantity`, `notional=None`, `side="sell"`). No EV gate, no Kelly. This fixes the silent-drop
   bug — a weak-thesis SELL now exits.
2. **TRIM = Kelly-target notional reduction.** `trim_notional = max(0, held.current_value − kelly_target)`,
   where `kelly_target = kelly_target_dollars(...)` is the weakened thesis's Kelly weight × TAV (haircuts +
   `max_position_weight` cap) with **no EV gate and no entry headroom clamps** (both are entry-only). A
   weaker thesis → lower target → larger trim; a position already at/below target trims nothing (dropped
   below `MIN_NOTIONAL_DOLLARS`). Emitted on the notional path.
3. **Held-position guard.** A SELL/TRIM whose instrument is not held is dropped with a `logger.warning`, not
   a HALT — one bad exit thesis must not kill the run.
4. **Exits are budget-neutral.** SELL/TRIM never read or accrue the BUY/ADD running sector/cash/overlap
   tallies (they reduce, not increase, exposure).

### Supporting changes
- `kelly_target_dollars` extracted from `size_position` (the un-gated, un-clamped `w · TAV`), reused by both
  the entry sizer (which adds the EV gate + clamps on top) and the TRIM path. BUY/ADD sizing is unchanged.
- `build_quantity_execution_params` added for the `qty` path (finite, `> 0` guard; precision-preserving,
  no-scientific-notation quantity string). `build_execution_params` (notional) now serves BUY/ADD **and**
  TRIM.
- An `ExecutionParameters` `model_validator` now requires **exactly one** of `notional`/`qty` — fail-closed
  hardening now that both order shapes are live.

### Consequences

Good: exits now behave as specified and as a trader expects — SELL fully closes by share count (no dust, no
EV-gate suppression), TRIM reduces toward the weakened Kelly size. A nonsensical non-held exit is dropped
loudly rather than sent to the broker. The `qty` path was already accepted end-to-end, so no downstream
change was needed. `design_decisions.md` §4's "not yet built" quantity path is now built.

Bad / to watch: the TRIM target uses the same Kelly machinery as entries, so a TRIM whose weakened thesis has
non-positive EV yields a `0` target and trims the whole position (effectively a full exit via the notional
path) — acceptable (reducing a collapsed thesis) but worth noting it can approximate a SELL. Quantity is
echoed from the snapshot at up to 9 decimals; a broker-side fractional-share precision limit is not
re-checked here (the snapshot quantity originated from Alpaca, so it is already valid). The `qty`/`notional`
mutual-exclusivity lives in our validator and the Alpaca server, not in the pinned JSON schema (which only
documents it in prose).

### Still open
- Nothing specific to exits remains. The broader atomic-group compensation and N>1 corroboration items are
  tracked elsewhere and are unaffected.

## Considered Options (key rejections)

- **Fixed-fraction TRIM** (remove a configurable fraction of the position). Rejected: ignores the scenario
  table and §2's "toward the lower Kelly target" — every trim would remove the same proportion regardless of
  how much the thesis weakened.
- **Keep exits on the entry sizer.** Rejected: it is the source of both defects (entry-sized notional; EV
  gate suppressing exits).
- **HALT on a non-held exit thesis.** Rejected: too aggressive; a single mis-named exit should drop, not
  kill the run.

## More Information

Resolves `design_decisions.md` §4 (SELL/TRIM translation). Implements `compute/sizing.py::kelly_target_dollars`,
`compute/execution_params.py::build_quantity_execution_params` (+ `_format_quantity`), the
`ExecutionParameters` exactly-one-of validator (`schemas/action_steps.py`), and the exit branch of
`pipeline/analysis.py::_materialize_action_steps`. Builds on ADR 0026 (the BUY/ADD running-tally
enforcement, unchanged here) and ADR 0014/0015 (the pinned order schema + invalid-amount guard).
