# Execution observes real order fills; the EXECUTED_INCOMPLETE outcome and notify-on-incomplete

- Status: accepted
- Date: 2026-07-13
- Deciders: owner, python-dev

## Context and Problem Statement

ADR 0003 made the execution node **submission-level** as an explicit, bounded stub: the injected
`place_order` returned only a broker order id, so a successful call proved *"submitted,"* not
*"filled,"* and `EXECUTED_CLEAN` was redefined as "all independent legs submitted without exception,"
with `filled_qty`/`filled_avg_price`/`realized_notional` held `None`. That ADR named its own terminus:
"fills observable → `phase=FILLED`, `filled_*` populated, `EXECUTED_CLEAN` regains its filled meaning."
This change reaches that terminus for the independent-order path.

A program that moves real capital must not report "we sent the order" as success — it must observe the
actual fill. Two problems had to be solved together:

1. **Observation.** There was no way to read an order's real status/fill; the Alpaca client could only
   `place_stock_order`.
2. **The outcome gap.** Once `EXECUTED_CLEAN` means "filled," the `ExecutionOutcome` enum had no member
   for an order that was **submitted but never cleanly fills** — still open at poll timeout (e.g. a
   market DAY order placed while the market is closed sits `accepted`), or a **terminal partial**
   (`partially_filled` → `done_for_day`). Folding those into `EXECUTED_CLEAN` reports unearned success
   (the exact thing ADR 0003 forbids); folding into `EXECUTION_FAILED` asserts a broker rejection that
   did not happen; `outcome=None` is reserved for crashed/incomplete journals (ADR 0003).

Atomic-group compensation stays the deferred `AtomicGroupNotSupportedError` stub (its design inputs —
grouping criteria, leg ordering, compensation cost bound — are architecture §15 #9/#10/#11, still open
pending a real interdependent thesis). This ADR covers the **independent-order** path only, which is the
only path the current N=1 signal reality produces.

## Decision Drivers

- Never report unearned success: a "submitted" order is not a filled order.
- Reads go via alpaca-py, writes via MCP (ADR 0008) — order status is a **read**.
- Fail closed: if a fill cannot be observed, record reality (open/unobserved), never a fabricated fill.
- No magic numbers: poll timing is a named `Config` surface.
- Human-in-the-loop on an abnormal capital state, without alert fatigue on normal recorded failures.

## Decision Outcome

1. **Observe fills via alpaca-py.** `make_alpaca_fill_observer` (`alpaca_orders.py`) reads an order's
   real status by `client_order_id` through the alpaca-py `TradingClient.get_order_by_client_id` — the
   reads-via-SDK boundary of ADR 0008 — and returns a **typed `FillObservation`** (raw status, mapped
   `ExecutionPhase`, `filled_qty`/`filled_avg_price`/`realized_notional`). The alpaca-status→phase
   mapping (`compute/fills.map_order_status`) is a pure, exhaustively-tested function; the raw alpaca
   `Order` never crosses into the pipeline node.

2. **Poll to terminal or timeout, fail closed.** The execution node submits, journals the SUBMITTED
   entry incrementally (crash-survivable), then polls (`_poll_fill`, clock-injected) until a terminal
   status or `execution_fill_poll_timeout_seconds`. A 404 (order not yet indexed) and transient read
   errors are treated as **retryable in-flight**; if the order never reaches terminal within the window
   the node records the last-observed non-terminal state, or an explicit "unobserved" marker — never a
   fabricated fill.

3. **`EXECUTED_CLEAN` reverts to "all legs filled"; add `EXECUTED_INCOMPLETE`.** The new outcome means
   "submitted, but one or more legs are not cleanly filled (open at timeout, or a terminal partial)."
   Outcome precedence (`compute/fills.derive_execution_outcome`): **incomplete > failed (rejected) >
   clean** — an open/partial leg leaves the portfolio in a state nobody chose and outranks a clean
   rejection.

4. **Notify the owner on `EXECUTED_INCOMPLETE`.** A new post-execution route
   (`execution_outcome_router`) sends an incomplete run through the notification node (subject
   "Execution Incomplete", body summarizing the journal legs) before the finalizer. A cleanly
   `EXECUTION_FAILED` run (rejected, nothing moved) does **not** email — §7a re-plans from reality on the
   next run, and emailing every rejected independent leg is alert fatigue. `map_execution_outcome`
   already routes `EXECUTED_INCOMPLETE` to `failure` in `determination.json` (only `EXECUTED_CLEAN` /
   `PARTIAL_COMPENSATED` are success), so the audit record is truthful regardless of the email.

### Consequences

Good: execution is honest — the journal carries real fills, `EXECUTED_CLEAN` again means filled, and
the two "not cleanly filled" realities (open-at-timeout, terminal partial) are surfaced to the owner
rather than mis-reported. The observe path is a real boundary tested offline (typed fakes + injected
clock) and validated live against the paper account.

Bad / to watch: reverting `EXECUTED_CLEAN` to "filled" means a run executed **outside market hours**
lands in `EXECUTED_INCOMPLETE` and emails, rather than reporting clean — ADR 0003 had defined
`EXECUTED_CLEAN` as merely "submitted" partly so off-hours dry-runs could reach a clean terminal. This is
the intended honest behavior; the eventual off-hours fill is reconciled by the next run's recovery
(§7a — the next piece of work). The poll timeout is a `Config` knob
(`execution_fill_poll_interval_seconds` / `execution_fill_poll_timeout_seconds`); too short a timeout
would prematurely mark a slow fill incomplete.

### Confirmation

Unit tests pin the pure mapping over all 18 alpaca statuses, the outcome precedence, the poll loop
(terminal-first, retry-then-terminal, 404-retryable, timeout→last-non-terminal, transient→unobserved
marker), and the node outcomes (all-filled→CLEAN, rejected→FAILED, partial/open→INCOMPLETE, submit
failure→FAILED, crash-survivable journal). An opt-in `@pytest.mark.live` test placed a $1 SPY paper
order and observed its fill end-to-end through the real alpaca-py poll path.

## Considered Options (key rejections)

- **Fold open-at-timeout / partial into `EXECUTION_FAILED` or `outcome=None`.** Rejected: `EXECUTION_FAILED`
  asserts a broker rejection that did not happen; `None` is reserved for a crashed/incomplete journal
  (ADR 0003). A distinct `EXECUTED_INCOMPLETE` states the truth.
- **Add a `get_order` MCP tool for status.** Rejected: order status is a read, and ADR 0008 puts reads
  on alpaca-py; a new write-transport tool would breach that boundary.
- **Email on every non-clean outcome including `EXECUTION_FAILED`.** Rejected: a rejected independent
  leg moved no capital and §7a re-plans next run; emailing it trains the owner to ignore alerts.

## More Information

Reaches the terminus of ADR 0003 (submission-level `EXECUTED_CLEAN`) for the independent-order path;
atomic-group compensation and the `COMPENSATION_FAILED`/`PARTIAL_COMPENSATED` outcomes remain deferred
stubs. Related: `src/money_pit/alpaca_orders.py`, `src/money_pit/compute/fills.py`,
`src/money_pit/schemas/fills.py`, `src/money_pit/pipeline/execution.py`,
`src/money_pit/pipeline/notification.py`, `src/money_pit/graph/edges.py`, `src/money_pit/config.py`,
`docs/pipeline_contracts.md` §7/§7a.
