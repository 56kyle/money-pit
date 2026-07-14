# Recovery reconciles a prior run's open orders and halts on genuine open exposure

- Status: accepted
- Date: 2026-07-13
- Deciders: owner, python-dev

## Context and Problem Statement

Architecture §15 #12 ("Recovery semantics") was left open: *on a prior crashed or `COMPENSATION_FAILED`
run, define exactly how next-run reconciliation reads the journal + live positions and decides what, if
anything, to finish or unwind — versus simply re-planning from current state.* `pipeline_contracts.md`
§7a states the intent — "reconcile against current broker positions (the fresh snapshot) before
planning … re-plans from reality — it never blind-replays" — but not the mechanics.

Now that execution observes **real fills** (ADR 0017), recovery has concrete inputs. The unreconciled
prior states that can actually occur (atomic-group compensation and `COMPENSATION_FAILED` stay deferred
stubs) are `EXECUTED_INCOMPLETE` (a leg left open at poll timeout or terminally partial) and a crashed
run (`outcome=None`).

The load-bearing realization: **the fresh snapshot already handles every *filled* leg correctly.** A4
sizes from current positions, so a prior fill is simply part of reality — undoing a good independent
trade would be a mistake (§7a's independent-order principle). The genuine correctness risk is narrow and
specific: **a prior order still *open* at the broker when the new run starts.** An open order is not a
position yet, so the snapshot misses it — if the new run plans as if it were absent and *both* the old
open order and the new order fill, the result is **double exposure**. That is the one thing "just
re-plan from reality" gets wrong.

## Decision Drivers

- Close the double-exposure hole without introducing auto-unwind (a capital action whose policy —
  §15 #11, compensation cost bound — is deliberately deferred with the atomic-group work).
- Use only capabilities we have: the fill observer (ADR 0017) can re-query any order's current status.
- Fail closed: if a prior order cannot be confirmed settled, do not risk trading alongside it.
- Do not wedge the scheduler on the common case (a prior abnormal run that has since settled cleanly).

## Decision Outcome

Recovery is the graph **entry node**, running before the snapshot/planning:

1. **Locate** the most recent prior run's execution journal — scan the daily-show root for the greatest
   slug lexicographically less than the current slug (slug `YYYY-MM-DD_HH-MM-SS` makes lexicographic ==
   chronological) that has an `execution_journal.json`. Runs that never executed (no journal) are
   skipped. No prior journal → proceed silently.
2. **Re-observe** each potentially-open leg (journal phase `SUBMITTED` / `PARTIALLY_FILLED`) at its
   *current* broker status via the fill observer: terminal now → settled; still non-terminal → open; a
   broker 404 (`OrderNotYetVisibleError`) → settled (the order is gone/expired); a transport error
   (`FillObservationError`) → **fail closed as open**. Already-terminal legs (`FILLED`/`REJECTED`/`FAILED`)
   need no re-query.
3. **Decide** and record (`recovery.json` written either way):
   - any leg still open → **HALT** the new run + email "money-pit: Recovery Halt" (double-exposure risk;
     a human reconciles). The graph routes recovery → END; nothing is planned or executed.
   - all settled but the prior run was abnormal (`EXECUTED_INCOMPLETE` or crashed `None`) →
     **PROCEED_WITH_NOTICE**: email "money-pit: Prior Run Reconciled" and continue to planning.
   - otherwise → **PROCEED** silently.

**No auto-unwind.** Recovery never cancels or compensates a stray order — that is unwinding, whose
policy belongs with the deferred §15 #11 / atomic-group work. Halting hands the rare lingering-open case
to a human. The behavior is **self-healing**: once the open order settles (fills or expires — market DAY
orders expire end of day), the next run re-observes it as terminal and proceeds.

### Consequences

Good: the double-exposure hole is closed with no new capability and no auto-unwind; the common case (a
prior run that settled) proceeds and re-plans from reality; an abnormal-but-settled prior run gets a
closure notice; a genuinely-open prior order fails closed. `recovery.json` gives a per-run audit trail.

Bad / to watch: a genuinely-stuck open order wedges the scheduler until a human clears it (rare). The
`COMPENSATION_FAILED` reconciliation branch of §7a stays a fail-closed stub — no compensation exists to
produce it. Recovery checks only the single most-recent prior run, relying on the invariant that each run
reconciles its immediate predecessor; a skipped or lost run could leave an older unreconciled order
unseen. A narrow crash window (a crash between `place_order` returning and the incremental `SUBMITTED`
journal write) could leave an order at the broker that is absent from the journal, which recovery keys
on; the incremental write makes this window very small. Both are noted limitations, not addressed here.

### Confirmation

Unit tests pin `reconcile_prior_run` across the decision axes (no prior run; settled clean; abnormal-now-
settled → notice; still-open → halt; 404 → settled; transport error → fail-closed open; prior-journal
selection), the node (recovery.json write, halt/notice emails, silent proceed), and `recovery_router`.
Integration tests confirm a recovery HALT ends the run before any snapshot/planning artifacts are written,
and a notice run proceeds through the normal execute path.

## Considered Options (key rejections)

- **Always halt+notify on any unreconciled prior run.** Rejected: wedges the scheduler on the common,
  safe case where the prior order has already settled and re-planning from the snapshot is correct.
- **Auto-cancel lingering open orders, then proceed.** Rejected: needs a new cancel capability and
  commits to an auto-unwind policy (§15 #11), both deliberately deferred with the atomic-group work.
- **Notify + proceed always.** Rejected: leaves the double-exposure hole open — unacceptable for a
  capital-moving program.
- **A pre-graph orchestration step instead of a graph node.** Rejected in favor of a node: it fits the
  node architecture, records its decision in graph state + `recovery.json`, and reuses the already-injected
  `observe_fill` / `send_email` deps.

## More Information

Resolves architecture §15 #12. Builds on ADR 0017 (real fill observation) and ADR 0003 (execution-journal
semantics). The atomic-group compensation and `COMPENSATION_FAILED` reconciliation remain deferred stubs.
Related: `src/money_pit/pipeline/recovery.py`, `src/money_pit/schemas/recovery.py`,
`src/money_pit/graph/edges.py`, `src/money_pit/graph/graph.py`, `docs/pipeline_contracts.md` §7a.
