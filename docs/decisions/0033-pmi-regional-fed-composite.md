---
status: accepted
date: 2026-07-24
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# PMI Regime Indicator Uses a Regional-Fed Manufacturing Diffusion Composite

## Context and Problem Statement

The `pmi` macro-regime indicator mapped to FRED series `NAPM` (the ISM Manufacturing PMI). `NAPM` has been permanently removed from FRED — the API returns HTTP 400 "The series does not exist," and a FRED series search for "manufacturing PMI" returns zero results. ISM and S&P Global no longer license their PMI series for FRED redistribution, so there is **no** ISM-style 50-centered manufacturing PMI available on FRED at all.

The failure was silent until ADR 0032's follow-up (commit `2c3d6ab`) added `response.raise_for_status()` to `fetch_fred_series`: FRED's 400 error body previously parsed as JSON with no `observations` key and was swallowed as `NoData`, mislabeling a hard fetch failure as a legitimately-empty series. The correct, loud failure exposed the dead series id.

What FRED-available series should the `pmi` regime indicator use, given that `classify_regime` interprets the value numerically against a structural threshold (ADR 0001), not as opaque text?

## Decision Drivers

- The value feeds `discretize(pmi, threshold_pmi, higher-is-better, band)` in `compute/regime.py`. A replacement on a different numeric scale that leaves `threshold_pmi` unchanged silently pins the PMI signal to a constant and corrupts the regime truth table — the swap and the threshold must move together.
- The replacement must be freely and reliably hosted on FRED (the deterministic A3 retrieval path fetches the latest scalar per series id).
- PMI is one of two growth signals (`growth = pmi + earnings`); dropping it degrades the regime classifier's discriminating power.
- The proxy should approximate national manufacturing conditions, not a single region.

## Considered Options

- Regional Fed manufacturing diffusion composite (averaged)
- A single regional Fed diffusion index (e.g. Philadelphia only)
- Industrial Production: Manufacturing (`IPMAN`), year-over-year
- Accept the indicator as unavailable (leave `pmi` null → `UNCERTAIN`)

## Decision Outcome

Chosen option: **regional Fed manufacturing diffusion composite**. The `pmi` indicator now maps to the arithmetic mean of three seasonally-adjusted "current general (business) activity" manufacturing diffusion indices on FRED:

| Region | FRED series id |
| ------ | -------------- |
| Philadelphia (District 3) | `GACDFSA066MSFRBPHI` |
| New York (Empire State) | `GACDISA066MSFRBNY` |
| Dallas (Texas) | `BACTSAMFRBDAL` |

These are net-diffusion indices **centered at 0** (percent-reporting-increase minus percent-reporting-decrease), where positive readings indicate expansion. Accordingly, `threshold_pmi` moves from `50.0` to `0.0`; the higher-is-better orientation and the `0.5` `regime_band` are unchanged and remain correct (a reading within ±0.5 is neutral, >0.5 expansion, <−0.5 contraction). This supersedes the `pmi` row of ADR 0001's threshold table.

Richmond and Kansas City run comparable surveys but are not cleanly retrievable as diffusion series on FRED, so the pinned roster is these three. The roster is trivially extensible: `MACRO_INDICATOR_SERIES` values are now `tuple[str, ...]` (see below), so adding a region is a one-line change.

### Uniform tuple-valued indicator map

`MACRO_INDICATOR_SERIES` changes from `dict[str, str]` to `dict[str, tuple[str, ...]]`. Every indicator is now "one or more FRED series to average"; the four unchanged indicators are single-element tuples (mean of one = itself). This keeps the retrieval path uniform — no special-casing for the composite — and confines the change to the one consumer that reads the map's values (`pipeline/retrieval.py`); `questions.py` and `analysis.py` iterate the map's keys only and are unaffected.

### Fail-closed combine policy

`_combine_fetch_results` combines the per-series `FetchResult`s:

- Any component `FetchError` → return that `FetchError` (fail closed; loud).
- Else all components `NoData` → `NoData` (→ indicator null → `UNCERTAIN`).
- Else → `FetchValue(mean of the readings)`, skipping `NoData` components.

A composite is only as trustworthy as its parts. Failing closed on any component error keeps a transient regional-fetch failure from silently averaging into a smaller, mislabeled sample and corrupting a capital-allocation signal — consistent with the loud-failure intent of ADR 0032 / commit `2c3d6ab`. The proxy exists for national **breadth**, not fault tolerance.

### Consequences

- Good, because the growth signal is preserved with a freely-available FRED source and broader (three-district) coverage than a single survey.
- Good, because the tuple-valued map generalizes the retrieval contract without touching the tool protocol or the four unchanged indicators.
- Good, because the fail-closed policy surfaces upstream problems as ERROR logs rather than a quietly-degraded regime tag.
- Bad, because regional Fed diffusion indices are noisier and less comprehensive than the national ISM PMI they replace; the composite is a proxy, not an equivalent.
- Bad, because fail-closed means any single regional outage yields `UNCERTAIN` for that run rather than a partial average — an intentional trade of availability for trustworthiness.
- Neutral, because ADR 0032's error-text redaction is unchanged: the FRED API key still travels in the query string, so `FetchError.reason` continues to carry only the exception class name.

### Confirmation

`tests/unit_tests/pipeline` covers `_combine_fetch_results` (mean of multiple values; any error → that error; all `NoData` → `NoData`; mixed value/`NoData` → mean of values; single-series pass-through; empty → `NoData`) and the composite `_fetch_deterministic` PMI path (mean on success; one component error → fail-closed `FetchError`). `tests/unit_tests/compute/test_regime.py` is re-based to the 0-centered scale so the truth-table rules remain semantics-preserving under `threshold_pmi = 0.0`. An opt-in live tier (`tests/integration_tests/fred_live`) exercises the three regional ids against the real FRED API.

## Pros and Cons of the Options

### Regional Fed manufacturing diffusion composite (averaged)

- Good, because averaging multiple districts approximates national conditions and dampens single-survey noise.
- Good, because all components are free, seasonally adjusted, and current on FRED.
- Bad, because it is a proxy for, not a reproduction of, the ISM PMI; the scale and methodology differ.

### A single regional Fed diffusion index

- Good, because it is the minimal change (swap one id, re-center the threshold).
- Bad, because a single district (e.g. Philadelphia) is materially noisier and less representative of national manufacturing than a composite.

### Industrial Production: Manufacturing (`IPMAN`), YoY

- Good, because it is a robust national hard-data series.
- Bad, because it is an index level (2017=100), so a regime-relevant signal requires computing a year-over-year change — the retrieval path fetches only the latest scalar, so this needs a new transform in the fetch/analysis path.
- Bad, because hard industrial-production data lags and behaves differently from a forward-looking diffusion survey.

### Accept the indicator as unavailable

- Good, because it requires no new data source.
- Bad, because it permanently collapses the growth signal to `UNCERTAIN`, degrading the classifier; rejected as it abandons a capital-relevant signal rather than solving the problem.

## More Information

Supersedes the `pmi` row of the threshold table in [ADR 0001](0001-regime-scalar-threshold-v0.md) (source: ISM Mfg → regional Fed diffusion composite; threshold: 50.0 → 0.0). Error-text redaction is governed by [ADR 0032](0032-secretstr-held-to-point-of-use-and-redacted-in-errors.md) and is unaffected. If A3 retrieval is later extended to accumulate historical observations, the z-score approach deferred by ADR 0001 becomes viable for all indicators including this composite.
