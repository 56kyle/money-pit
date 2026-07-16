# Candidate-side sector & overlap enforcement with running allocation

- Status: accepted
- Date: 2026-07-15
- Deciders: owner, python-dev

## Context and Problem Statement

ADR 0024/0025 sourced `factor_tags`/`correlated_overlaps` and made the overlap clamp consume held↔held
duplication, but flagged two gaps rooted in one problem: **the deterministic sizing clamps could not see a
*candidate's* sector or constituents, because a candidate is a new name absent from the snapshot.**

- The sector clamp was a flat `sector_cap × TAV` per candidate (`analysis.py`), ignoring `sector_weights`
  and the candidate's sector — so two new same-sector BUYs could each take 25% → **50% in one sector**,
  breaching the 25% cap.
- Overlap detection was held↔held only — a brand-new buy that would *create* duplicative exposure with a
  holding wasn't caught.

A third defect surfaced during the work: **SELL/TRIM were clamped by the exposure-*increase* headrooms
identically to BUY/ADD** (semantically wrong — an exit reduces exposure). And a latent one: the cash
headroom was a *static* per-run ceiling, so N BUYs against one cash figure could jointly overdraw settled
cash — the same class of bug as the sector gap.

## Decision Drivers

- Capital safety over opportunity: an uncertified over-deployment breaches a hard cap; the caps must be
  enforced, deterministically, in the post-processor (`architecture.md` §11.5).
- Reuse the existing injected-`Callable`-seam + fail-soft-yfinance conventions; no new dependency.
- A single run can propose several exposure-increasing candidates; the caps must hold *across* them, not
  just per-candidate.

## Decision Outcome

1. **Candidate resolver seam.** A `ResolveInstrumentFacts = Callable[[str], InstrumentFacts]` seam
   (`InstrumentFacts = sector, is_etf, holdings`) is injected into `make_analysis_node` exactly like
   `thesis_agent` (non-capital-critical → `ov.X or make_yfinance_instrument_resolver()` fallback). The
   yfinance reference helpers were first extracted to a neutral `market_data.py` leaf so the resolver reuses
   them without importing alpaca-py. The node resolves only the distinct **BUY/ADD** instruments into a
   `dict[str, InstrumentFacts]` (the I/O step) and passes that plain dict into the pure
   `_materialize_action_steps`.
2. **Persist held-ETF holdings on the snapshot.** `PortfolioSnapshot.etf_holdings` (already fetched by the
   snapshot builder, previously discarded) is persisted so the post-processor can detect "candidate
   single-name is held by a held ETF" deterministically.
3. **Running-tally allocation with priority order.** Surviving theses are sized in priority order —
   conviction (HIGH>MEDIUM>LOW) desc, then `expected_value` desc, then stable by emission index (resolving
   the fact that A4 emits theses unordered). The loop maintains running `sector_used` (seeded from held
   exposure), `cash_available` (seeded `max(0, available_cash − cash_min·TAV)`), and `in_run_by_ticker`.
   Each exposure-increasing candidate's headrooms are `max(0, sector_cap·TAV − sector_used[sector])`,
   `cash_available`, and `max(0, overlap_limit·TAV − (held-correlated value + in-run-correlated dollars))`;
   after sizing, all three tallies accrue. N same-sector (or same-cash, or correlated) candidates in one run
   therefore collectively respect the cap.
4. **Cash is a running tally too** (a deliberate strengthening beyond the initial plan, which said "cash
   stays per-run"). A static cash ceiling is the same overdraw hole the sector fix closes; making it running
   is the faithful, capital-safe extension of the running-tally decision.
5. **Gate the exposure clamps to BUY/ADD.** Only exposure-increasing actions (`BUY_SIDES = {BUY, ADD}`,
   promoted to public) get the sector/cash/overlap clamps and contribute to the tallies. SELL/TRIM pass
   `math.inf` for all three (float-safe; `dollars = w·TAV` stays finite) — they stop being wrongly clamped.
6. **Drop sub-minimum-notional sizes.** A candidate clamped by a running tally to a tiny positive residual
   below `MIN_NOTIONAL_DOLLARS` (one cent) is dropped, not fed to `build_execution_params` (whose
   `InvalidExecutionAmountError` would otherwise HALT the whole run). That guard stays as a defensive
   invariant.

### Semantics fixed deliberately
- **Sector normalization:** sector keys are casefolded; a candidate whose sector resolves to `"unknown"`
  (yfinance miss) falls into a shared `"unknown"` bucket under the same running 25% cap — conservative
  (caps aggregate unknown-sector exposure) rather than dropping the trade or failing open.
- **Overlap self-exclusion:** a name's own held value is *not* counted against the correlated-group budget
  (an ADD to a held name is governed by the sector cap for its own concentration; overlap governs
  *cross-name* duplication).
- **In-run candidate↔candidate correlation** is bounded to *direct* ETF-constituent relationships (one
  candidate's holdings contain the other). Two single names that merely share a held ETF are not linked
  in-run — an explicit non-goal, mirroring ADR 0025's held-scope bound.

### Consequences

Good: the 25% sector cap, the cash ceiling, and correlated-overlap reductions are now genuinely enforced
across a whole run, deterministically, sized best-idea-first. The change only ever tightens a BUY/ADD vs.
before and stops mis-clamping SELL/TRIM; sub-minimum residuals drop gracefully instead of crashing. The
analysis node gains one fail-soft yfinance seam (it already ran an LLM, so it was never offline-pure).

Bad / to watch: the resolver adds one yfinance `.info` call per distinct BUY/ADD candidate (fail-soft to
`unknown`/`[]`). Priority order now determines which of several competing same-sector candidates gets scarce
headroom — intended, but it means a mis-ranked conviction/EV starves a better idea. `funds_data` top-N
holdings can miss a smaller shared constituent (inherited from ADR 0025). yfinance sector strings are the
matching key across held + candidate sides; casefolding mitigates but a taxonomy change would mis-bucket.

### Still open (flagged follow-up)
- `design_decisions.md` §4 SELL/TRIM **exit sizing** (SELL as a full-position exit by held quantity; TRIM
  toward a reduced target) remains unbuilt — SELL/TRIM still route through the Kelly/notional path and its
  EV gate, which is semantically an entry sizer. Gating the clamps here does not fix that; it is the next gap.

## Considered Options (key rejections)
- **Resolve candidate facts inside the deterministic post-processor directly (no seam).** Rejected: breaks
  offline testability; the seam keeps `_materialize_action_steps` pure and fake-injectable.
- **Fail-closed drop on unknown/missing sector.** Rejected in favor of the conservative shared `"unknown"`
  bucket — the resolver already fail-softs, so a transient yfinance blip caps rather than vanishes a trade.
- **Keep cash static per-run.** Rejected: a latent overdraw; see decision #4.
- **Second-order / shared-held-ETF candidate correlation.** Deferred as an explicit non-goal.

## More Information

Resolves the sector-cap gap flagged in ADR 0024/0025 and generalizes ADR 0025's overlap clamp to the
candidate-vs-holdings case. Implements `src/money_pit/market_data.py`
(`make_yfinance_instrument_resolver`), `src/money_pit/schemas/instrument.py` (`InstrumentFacts`),
`contracts.py` (`ResolveInstrumentFacts`), `PortfolioSnapshot.etf_holdings`, the DI wiring
(`orchestration.py`, `graph/graph.py`), and the reworked `pipeline/analysis.py::_materialize_action_steps`
(+ `_priority_ordered`, `_seed_sector_exposure`, `_held_correlated_value`, `_in_run_correlated_dollars`).
`BUY_SIDES`/`MIN_NOTIONAL_DOLLARS` promoted to public in `compute/execution_params.py`.
