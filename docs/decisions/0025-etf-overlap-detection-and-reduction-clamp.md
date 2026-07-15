# ETF-holdings overlap detection and the overlap-reduction sizing clamp

- Status: accepted
- Date: 2026-07-15
- Deciders: owner, python-dev

## Context and Problem Statement

`architecture.md` §11.5 lists "correlated-overlap reductions" among the deterministic sizing clamps, and
`agent_4.md` Step 3.4 says "a new position that overlaps an existing one must have its size reduced." The
snapshot carries a `correlated_overlaps` field for this (canonical example:
`{"tickers": ["NVDA", "SMH"], "note": "ETF holds the single-name position"}`). But two halves were missing:

1. **Production of the data.** `alpaca_portfolio.py` emitted `correlated_overlaps=[]` (v0-deferred).
2. **Consumption of the data.** The post-processor's overlap headroom was a flat
   `config.overlap_limit × total_account_value` (`analysis.py`) that never read `correlated_overlaps`.
   Populating the field alone would have changed nothing — the clamp ignored it.

So the field was inert on both ends. This ADR covers sourcing the overlaps and making them clamp sizing.
(Factor tagging is the sibling ADR 0024.)

## Decision Drivers

- The clamp is a **capital-safety** control (`architecture.md` §11.5: "arithmetic clamps in the
  post-processor, not model discretion") — it must be deterministic and can only ever *reduce* a size.
- yfinance is already a dependency; `funds_data.top_holdings` exposes an ETF's constituents with no new
  library.
- The canonical spec case is specifically **ETF-holds-a-single-name duplication**, not generic price
  co-movement.
- yfinance calls are unreliable per ticker; detection must fail soft.

## Decision Outcome

1. **Detect overlaps from ETF constituents, not price correlation.** `compute/overlaps.py::detect_etf_overlaps`
   is a pure function: for each **held** ETF whose `funds_data.top_holdings` include **another held**
   single name, emit `CorrelatedOverlap(tickers=[etf, name], note="ETF holds the single-name position")`.
   The snapshot builder fetches holdings only for positions whose yfinance `quoteType == "ETF"`
   (`alpaca_portfolio._fetch_etf_holdings`, `# pragma: no cover`, fail-soft per ticker). This matches the
   spec's example and note directly; price-correlation was rejected (see below).
2. **Consume overlaps as a correlated-group budget** (`analysis.py::_overlap_headroom`). For a candidate
   `instrument`, the overlap headroom is
   `max(0, overlap_limit × TAV − existing_correlated_exposure)`, where the correlated exposure is the summed
   `current_value` of the *other* held tickers that share a `CorrelatedOverlap` group with the instrument
   (case-insensitive match, instrument self-excluded). A candidate in no overlap group keeps the full
   `overlap_limit × TAV` — bit-identical to the prior flat behavior — so the change only ever tightens an
   overlapping candidate's ceiling, never loosens. The headroom is now computed **per candidate** inside the
   sizing loop; sector and cash headrooms remain per-run.

### Consequences

Good: `correlated_overlaps` is now real data with a real deterministic consumer. An ADD to a holding that
already duplicates another holding (e.g. holding both `SMH` and `NVDA`, then adding `NVDA`) is sized down by
the correlated exposure, enforced arithmetically rather than left to A4's narrative. The detection adds no
dependency and degrades to "no overlaps" on any yfinance failure. The clamp is unit-tested to bite (an
overlapping candidate sizes to the reduced ceiling while an otherwise-identical non-overlapping candidate
does not).

Bad / to watch:
- **Detection is over *currently held* positions only.** `detect_etf_overlaps` inspects held ETFs against
  held names, so `correlated_overlaps` describes the portfolio's *internal* duplication. The clamp therefore
  fires for candidates that are themselves already in a held overlap group. Catching a **brand-new buy that
  would create a new overlap** (e.g. buying `SMH` when only `NVDA` is held) requires resolving the
  *candidate's* holdings/constituents at analysis time — the candidate isn't in the snapshot — which is the
  same candidate-side-resolution problem as the flagged **sector-cap gap** (`analysis.py`'s flat sector
  headroom). Both are tracked together as the recommended follow-up.
- `funds_data.top_holdings` is an unofficial scrape returning only the top ~10 constituents, so a smaller
  holding shared with the portfolio can be missed. Acceptable for a fail-soft, conservative-only clamp.
- The `overlap_limit`-budget reading operationalizes the spec's looser "reduce so combined exposure respects
  the 25% sector limit" wording; the two are related but the clamp is self-contained on `overlap_limit`
  (0.30) rather than joined to the sector cap.

## Considered Options (key rejections)

- **Price-correlation overlaps (`pandas.DataFrame.corr()` over yfinance history).** Rejected as the primary
  source: it flags any co-moving pair with a generic note and does not cleanly capture the spec's
  ETF-holds-a-name semantic (though an ETF and its top holding *are* highly correlated, so it would flag the
  same case with less specific provenance). A viable future *fallback* for the general case, not the
  canonical one.
- **Flat overlap headroom (status quo).** Rejected: it ignored the field entirely, so the documented
  "correlated-overlap reductions" safety control did not exist in data-driven form.
- **Joining the overlap reduction to the sector cap.** Deferred with the sector-cap follow-up; kept the
  clamp self-contained on `overlap_limit` for now.

## More Information

Implements `src/money_pit/compute/overlaps.py` (`detect_etf_overlaps`), the fail-soft
`_fetch_etf_holdings`/`_is_etf` in `src/money_pit/alpaca_portfolio.py`, and
`src/money_pit/pipeline/analysis.py::_overlap_headroom` (with `_Headrooms` reduced to sector+cash and the
per-candidate overlap wiring in `_materialize_action_steps`). Consumes `config.overlap_limit`. Sibling ADR
0024 covers factor tagging. The candidate-side overlap/sector follow-up is noted in `implementation_plan.md`.
