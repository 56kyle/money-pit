# money-pit — Pipeline Contracts (Source of Truth)

This document defines the **authoritative schema at every hand-off boundary** in the
money-pit pipeline. Every agent prompt must be edited to conform to the definitions here.
Where this document and an existing prompt disagree, **this document wins** — fix the prompt.

The pipeline:

```
0  orchestration (scheduler + yt-dlp/Whisper + money-pit portfolio CLI)
1  Source adapters (video + others) → SignalSet → aggregator → aggregated_signals.{json,md}
2  Question Generation                          → initial_questions.{json,md}
3  Information Retrieval                         → initial_answers.{json,md}
4  Analysis (seven-step framework)              → analysis.md, action_steps.{json,md}
5  MCP Validation                               → action_steps_validation.{json,md}, validation_status.json
6  Determination (PROCEED / HALT)               → determination.{json,md} → execution OR email sub-agent
```

A reader's index of which existing prompts need edits is in the final section
("Change list per agent").

---

## 0. Global conventions

These apply to **every** boundary and override anything in an individual prompt.

**Working directory & slug.** Each run owns `data/daily_show/{slug}/` where
`slug = {YYYY-MM-DD_HH-MM-SS}`. The slug is the **basename of the working directory**,
delivered to every node in graph state. It is the single source of truth for run identity.
No agent re-derives, validates, reformats, or invents it, and no agent reads it out of a
file's contents — it comes from graph state. Any field named `slug` or `run_id` is this value.

**File pairing.** Every agent that writes output writes **both** a `.json` (authoritative,
machine-readable) and a `.md` (supplementary, human-readable) with the same basename. The JSON
is authoritative; if the two ever disagree, the JSON wins. (This corrects Agent 2, which
currently writes JSON only.)

**Datetimes.** All datetimes are ISO 8601 strings. `published_at` is the video publication
datetime and is **mandatory downstream** — it flows 1 → 2 → 3 → 4 unchanged.

**Field-name discipline.** Field names below are literal and case-sensitive. Downstream agents
match on exact names. "Semantically equivalent" names (`why_it_matters` vs `rationale`,
`data_sources` vs `suggested_data_sources`, `verdict` vs `status`) are **not** interchangeable —
that class of drift is exactly what broke 2→3 and 5→6.

**Canonical enums** (used in multiple places):

- **source type:** `narrated_video` | `newsletter` | `rss` | `research_pdf` | `manual_note` (extensible; the video adapter is `narrated_video`)
- **claim relation** (across sources, §2): `agree` | `disagree`
- **signal tier:** `high` | `medium` | `low` (and `portfolio` where a question is motivated by a holding rather than a claim)
- **claim category:** `fundamental` | `technical` | `macro` | `sentiment` | `catalyst`
- **question category:** `thesis_validation` | `macro_regime` | `current_events` | `portfolio_gap` | `invalidation_conditions`
- **data source token:** `fred_mcp` | `yfinance_mcp` | `edgartools_mcp` | `brave_search_mcp` | `alpaca_mcp`
- **confidence:** `high` | `medium` | `low`
- **action type:** `BUY` | `SELL` | `TRIM` | `ADD` (Agent 4's set; no `monitor` — monitoring lives in `invalidation_conditions`; no `close`/`reduce` — use `SELL`/`TRIM`)
- **compensating action** (within an atomic group only): `BUY→SELL`, `ADD→TRIM`, `SELL→BUY`, `TRIM→ADD` — the neutralizing action used to unwind a filled leg when a sibling leg fails (§7a)
- **factor set:** `growth` | `value` | `momentum` | `quality` | `low_vol` (five factors; pin this everywhere — Agent 2 and the snapshot must use the same five, not `volatility`)
- **validation status:** `MATCHED` | `UNMATCHED`
- **execution phase** (journal entry lifecycle, §7a): `PLANNED` | `PREFLIGHT_OK` | `PREFLIGHT_FAILED` | `SUBMITTED` | `FILLED` | `PARTIALLY_FILLED` | `REJECTED` | `FAILED` | `COMPENSATING` | `COMPENSATED` | `COMPENSATION_FAILED` | `SKIPPED`
- **execution outcome** (whole run): `EXECUTED_CLEAN` | `PARTIAL_COMPENSATED` | `COMPENSATION_FAILED` | `EXECUTION_FAILED`

---

## 1. Boundary 0 → 1 — orchestration → source adapters

**Producer:** orchestration layer. **Consumer:** each source adapter.

Each adapter receives its source payload from the orchestration layer (the video adapter gets the
transcript string plus the keyframe/OCR artifacts; a text adapter gets the fetched document). An
adapter has no filesystem access of its own beyond what it is handed; it returns the `SignalSet`
(json + markdown), which the orchestration layer writes to `signals/{source_id}.json/.md`. The
original single-video pathway (Agent 1 returning two fenced blocks from a transcript string) is the
video adapter's special case of this boundary.

---

## 2. Boundary: source adapters → aggregator → A2/A4

**Producers:** one or more **source adapters** (the narrated-video adapter — formerly Agent 1 — plus
any future newsletter/RSS/PDF/note adapters), then the **aggregator** node. **Consumers:** A2 and A4.

The signal has always originated as a _classified claim_; the video was merely the only thing that
produced claims. Genericizing names that boundary: every input becomes a `Source` adapter that emits a
normalized `SignalSet` of `Claim`s, and an aggregator merges all of a run's `SignalSet`s into one
`AggregatedSignals` that the rest of the pipeline consumes — source-agnostic. This quarantines the
hard multimodal video extraction inside one adapter; nothing downstream knows video exists. Start at
**N = 1** (video adapter only): one source in, one aggregate out, today's behavior unchanged.

### 2.1 `SourceRef` — structured provenance (replaces free-text `source_context`)

Two distinct things, kept separate: the source that **delivered** a claim (`SourceRef`), and the
source the claim **cites** for its data (`cited_sources`). The on-screen attribution discussed for the
video adapter (OCR/VLM-extracted chart footers) populates `cited_sources`; a newsletter adapter fills
it from links, a PDF adapter from footnotes — same field, every adapter.

```jsonc
{
  "source_id": "yt:dQw4w9WgXcQ", // unique per source instance
  "source_type": "narrated_video", // source-type enum
  "title": "Daily market wrap — 2026-06-18",
  "url": "https://…", // or null
  "published_at": "2026-06-18T13:00:00Z", // this source's own date (ISO 8601, or null)
  "retrieved_at": "2026-06-18T14:30:00Z",
  "locator": "00:14:32" // in-source position: video timestamp / PDF page / null
}
```

### 2.2 `Claim` (generalized)

```jsonc
{
  "claim_id": "yt:dQw4w9WgXcQ:S001", // namespaced by source_id (aggregator re-IDs run-global)
  "tier": "high", // high | medium | low (explicit field)
  "claim": "One-sentence claim in the adapter's own words.",
  "category": "fundamental", // fundamental|technical|macro|sentiment|catalyst
  "tickers_affected": ["NVDA"],
  "requires_validation": true, // derived: true for high & medium
  "source_ref": { "...": "SourceRef above" }, // who delivered it
  "cited_sources": ["FRED", "company 10-Q"] // what the claim attributes its data to (may be [])
}
```

### 2.3 `SignalSet` — one per adapter → `signals/{source_id}.json`

```jsonc
{
  "slug": "2026-06-18_14-30-00",
  "source_ref": { "...": "SourceRef" },
  "summary": "2-3 sentence neutral overview of what THIS source covered.",
  "claims": ["...Claim..."],
  "tickers_mentioned": ["NVDA"],
  "sectors_mentioned": ["semiconductors"],
  "macro_themes": ["Fed rate policy"],
  "has_actionable_content": true // per-source; the run gate is computed at the aggregate
}
```

### 2.4 `AggregatedSignals` — the merge → `aggregated_signals.json` (what A2/A4 read)

```jsonc
{
  "slug": "2026-06-18_14-30-00",
  "sources": ["...SourceRef per included source..."],
  "claims": ["...union of all sources' claims, run-global claim_ids..."],
  "corroborations": [
    { "relation": "agree", "claim_ids": ["yt:…:S001", "news:…:S004"] } // independent sources, same assertion
  ],
  "conflicts": [{ "relation": "disagree", "claim_ids": ["yt:…:S002", "rss:…:S009"] }],
  "has_actionable_content": true // run gate: any source has high/medium signal
}
```

Notes:

- `claim_id` remains the join key for `signal_source` (§3) and is carried through to
  `initial_answers.json` (§4). The aggregator owns the run-global namespace.
- `requires_validation` / `has_actionable_content` are **deterministic** — compute in code (§9).
- **Corroboration/conflict** is the one new judgment surface: cluster claims by embedding similarity,
  then a _thin_ LLM pass confirms and labels `agree`/`disagree` within a cluster (§9/§11). Independent
  sources asserting the same claim is genuinely stronger signal — A4 Step 1 uses this as a positive
  input. **Tier reconciliation** for a corroborated claim: take the max tier across its sources.
- **`published_at` is now per-`SourceRef`, not a single run value.** Downstream recency (A2's
  current-events questions) anchors to _the originating claim's_ source date — see §3.
- **A4 consumes `aggregated_signals.json`.** The tier field is `tier` (`high`/`medium`/`low`), not a
  `classification` string; there is no separate per-claim timestamp (`source_ref.published_at` carries
  it); and `low`-tier claims are present — A4 filters to `high`/`medium` itself (its Step 1 does this).
- The **video adapter** is where the multimodal extraction lives (keyframes, OCR/VLM, fused
  transcript); see the architecture spec. Its complexity is fully contained behind this `SignalSet`
  contract — it can be stubbed or deferred without touching anything downstream.

---

## 3. Boundary 2 → 3 — `initial_questions.json`

**Producer:** Agent 2. **Consumer:** Agent 3.

**A2 now reads `aggregated_signals.json`** (not `transcript_summary.json`) plus `portfolio_snapshot.json`.
The claim contract is unchanged from A2's perspective — it still consumes claims with `claim_id`/`tier`
and writes `signal_source` referencing a (now run-global) `claim_id`.

**The fixes:** (a) one canonical `category` enum in snake_case (Agent 2 used Title Case, Agent 3
used a different 4-value set — neither matched); (b) canonical field names `data_sources` and
`rationale` (Agent 2 used `data_sources`/`why_it_matters`, Agent 3 read `suggested_data_sources`/
`rationale`); (c) Agent 2 must also write `initial_questions.md`.

```jsonc
{
  "slug": "2026-06-18_14-30-00",
  "generated_at": "2026-06-18T14:31:05Z",
  "signal_summary": {
    "high_signal_count": 0,
    "medium_signal_count": 0,
    "low_signal_count": 0,
    "questions_generated": 0 // equals questions.length, incl. placeholders
  },
  "questions": [
    {
      "id": "Q001", // Q + zero-padded, sequential in final order
      "category": "thesis_validation", // the 5-value canonical enum below
      "question": "Externally-answerable query text.",
      "signal_source": "yt:…:S003", // run-global claim_id, OR a portfolio
      // element (ticker/sector/factor), OR "none" (placeholder)
      "signal_tier": "high", // high | medium | portfolio
      "rationale": "1-2 sentences: why this must be answered before a decision.",
      "data_sources": ["yfinance_mcp"], // non-empty; tokens from canonical set. ["none"] only for placeholders
      "answer": null // always null here; Agent 3 fills initial_answers.json
    }
  ],
  "error": null // string on validation/insufficient-signal failure (see below)
}
```

**Canonical question categories** (all five must be represented; emit a placeholder record per
§3.6 of agent_2.md for any with no real question):

| category                  | meaning                                                                                                                                                                                            |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `thesis_validation`       | Does data support the specific video claim?                                                                                                                                                        |
| `macro_regime`            | The five regime indicators — yield curve, credit spreads, PMI, earnings revisions, **inflation** — one question per indicator. Together they populate `MacroIndicators` for regime classification. |
| `current_events`          | Anything material since the originating claim's `source_ref.published_at` (embed that datetime in the question; with multiple sources there is no single run-level `published_at`).                |
| `portfolio_gap`           | How a signal interacts with the **actual** portfolio (real positions/weights/exposures/cash).                                                                                                      |
| `invalidation_conditions` | Data needed to define what would make the thesis wrong.                                                                                                                                            |

**Failure shapes** (write to `initial_questions.json`, then halt): on input-validation failure or
zero high/medium signal, top level keeps `slug`, `generated_at`, `signal_summary`, `questions: []`,
and a specific `error` string. (Same as agent_2.md today; just keep the canonical top-level keys.)

**Category → default tool routing** (so Agent 3 §4 can be rewritten against this):

| category                  | primary tools                                                    | secondary                                     |
| ------------------------- | ---------------------------------------------------------------- | --------------------------------------------- |
| `thesis_validation`       | `edgartools_mcp`, `yfinance_mcp`                                 | `fred_mcp` (macro thesis), `brave_search_mcp` |
| `macro_regime`            | `fred_mcp`                                                       | `brave_search_mcp` (recent commentary)        |
| `current_events`          | `brave_search_mcp`, `edgartools_mcp` (8-K)                       | `alpaca_mcp` (current price)                  |
| `portfolio_gap`           | `alpaca_mcp`, `yfinance_mcp`                                     | `edgartools_mcp`                              |
| `invalidation_conditions` | metric-dependent: `yfinance_mcp` / `edgartools_mcp` / `fred_mcp` | `brave_search_mcp`                            |

`data_sources` on each question is a **suggestion**; Agent 3 retains final say but the tokens must
come from the canonical set so routing is deterministic.

---

## 4. Boundary 3 → 4 — `initial_answers.json`

**Producer:** Agent 3. **Consumer:** Agent 4.

**The fix:** carry `category`, `signal_source`, and `signal_tier` **through** from the question so
Agent 4 can reconstruct which claim each answer supports without a brittle three-file join.

```jsonc
{
  "slug": "2026-06-18_14-30-00",
  "sources": ["...SourceRef per source, carried from aggregated_signals..."], // replaces single published_at
  "answers": [
    {
      "question_id": "Q001", // verbatim from initial_questions
      "question": "Verbatim question text.",
      "category": "thesis_validation", // carried through
      "signal_source": "yt:…:S003", // carried through (run-global claim_id or portfolio element)
      "signal_tier": "high", // carried through
      "answer": "Retrieved factual answer, or structured explanation of why not. Never blank, never analytical.",
      "confidence": "high", // high|medium|low per §9 confidence rule
      "sources_used": ["FRED (DGS10, T10Y2Y)"],
      "data_retrieved": { "DGS10": 4.28, "as_of": "2026-06-17" }, // object, or null
      "limitations": "" // "" if none
    }
  ]
}
```

Rules unchanged from agent_3.md: one answer object per input question, in order; `answer` never
blank; **no analytical/interpretive language** (that is Agent 4's job); confidence per §9.

**Agent 4 must use the join key.** Agent 4 currently re-matches claims to answers by reading text,
which violates its own "determinism of method." Have it join `aggregated_signals.claims[].claim_id`
↔ `initial_answers.answers[].signal_source` and filter macro answers by `category == "macro_regime"`.
That is the entire reason `signal_source`/`category` are carried through here.

---

## 5. Boundary 4 → 5 — `action_steps.json`

**Producer:** Agent 4. **Consumer:** Agent 5. **This boundary is broken as written** — Agent 4's
output will make Agent 5 halt as "malformed," and even if it parsed, every step would be
`UNMATCHED`. There are four distinct problems.

**5a. Top-level shape: array, not object.** Agent 4 emits a top-level array and Agent 5 expects a
top-level array — they agree. (My earlier draft wrapped it in `{slug, steps}`; that was wrong.
Keep the **array**; carry `slug` in graph state, not in this file.)

**5b. Agent 5's mandatory field names are missing.** Agent 5 halts on "malformed" if a step lacks
`step_id`, `description`, `instrument`, or `action_type` _by literal name_. Agent 4 ships none of
those names: no `step_id`, `one_sentence_thesis` instead of `description`, `ticker` instead of
`instrument`, `action` instead of `action_type`. Add/rename these.

**5c. No execution parameters — the literal-match three-way contract.** Agent 5 matches each step's
execution parameters against the MCP tool input schema by **literal field name**, with no semantic
mapping allowed. Agent 4 supplies only `ticker`, `action` (`BUY`), and `dollar_amount`. The Alpaca
order tool expects (e.g.) `symbol`, `notional`, `side`, `type`, `time_in_force`. `ticker`≠`symbol`,
`dollar_amount`≠`notional`, `action:"BUY"`≠`side:"buy"`, and `type`/`time_in_force` are absent ⇒
every step `UNMATCHED (schema mismatch)` ⇒ Agent 6 HALTs and emails. These three must be identical:

> **`execution_parameters` keys (Agent 4) == Agent 5's literal check == the Alpaca MCP tool input schema (manifest)**

The keys are **not invented by Agent 4** — they come from the official Alpaca MCP order tool's
input schema (OpenAPI-generated, so it is stable and readable now). Pin `execution_parameters` to it,
or have the deterministic post-processor (below) emit them from it.

**5d. Agent 4's "no additional keys" rule blocks the fix.** Agent 4's schema says "exactly these
keys, no additional keys," which forbids adding `execution_parameters`. Relax it: the step object
carries both an **execution** block (literal tool params, what Agent 5 matches) and an **analysis**
block (thesis, scenarios, sizing — Agent 5 ignores these but they feed the email/audit path).

**Canonical step (array element):**

```jsonc
[
  {
    // --- identity & routing (Agent 5 mandatory; literal names) ---
    "step_id": "A001",
    "instrument": "NVDA",
    "action_type": "BUY",                  // BUY | SELL | TRIM | ADD
    "description": "Open a starter long in NVDA sized to conviction.",

    // --- atomicity (§7a): null = independent; shared non-null id = one all-or-nothing group ---
    "group_id": null,                      // e.g. "G1" for both legs of a pairs trade / hedge

    // --- execution: keys MUST match the Alpaca MCP tool input schema literally ---
    "execution_parameters": {
      "symbol": "NVDA",
      "notional": 1500.00,                 // BUY/ADD: capital to deploy; SELL/TRIM: amount to remove
      "side": "buy",                       // buy | sell  (derive from action_type)
      "type": "market",
      "time_in_force": "day",
      "client_order_id": "2026-06-18_14-30-00:A001"  // idempotency key, deterministic {slug}:{step_id}
    },

    // --- analysis: Agent 5 ignores; carried for the email/human/audit path ---
    "one_sentence_thesis": "…",
    "regime_tag": "GROWTH_DECELERATING",
    "expected_value": 7.4,
    "scenario_table": { "bull": {…}, "base": {…}, "bear": {…} },
    "invalidation_conditions": [ { "condition": "…", "action": "reduce by 50%" } ],
    "sizing_rationale": "base 5% × 1.0; sector headroom OK; no overlap.",
    "conviction": "MEDIUM",
    "step_failed": null
  }
]
```

`group_id` is set by Agent 4 / the post-processor: it is `null` for the common standalone step, and a
shared non-null id for the rare set of interdependent legs (pairs trade, hedged entry, funded roll)
that must execute all-or-nothing. `client_order_id` is emitted deterministically by the post-processor
as `{slug}:{step_id}` so re-submission after a crash/retry never double-executes; it must be a real
field in the Alpaca order schema (confirm) so Agent 5 validates it like any other execution parameter.

**Recommended structure — split judgment from arithmetic.** Steps 3, 5, 7 of Agent 4 are pure
arithmetic the prompt fully specifies (sector headroom to 25%, `EV = ΣP×R`, base = equity ÷ 20 ×
multiplier, constraint reductions). LLMs do this unreliably. Let Agent 4 emit the **judgments**
(surviving claims, scenario probabilities/returns, conviction band, chosen names) and let a
**deterministic post-processor** (code/tool) (a) compute EV and apply the ≥ +3.0% gate, (b) compute
`dollar_amount` and apply the 25% / cash / overlap constraints, and (c) **emit `execution_parameters`
with manifest-correct keys**. This single component removes the arithmetic-reliability risk _and_
closes 5b/5c in one place. See §9.

**`SELL`/`TRIM` semantics.** For exits, `notional` (or the manifest's quantity field) is the amount
to **remove**; `side` is `sell`. The post-processor must translate "dollar_amount to remove" into
whatever the Alpaca sell tool actually accepts (notional vs qty) — another reason to centralize it.

**`monitor` is resolved.** Agent 4 emits only `BUY`/`SELL`/`TRIM`/`ADD`; monitoring lives in each
position's `invalidation_conditions`. No un-executable step type reaches Agent 5. (This closes the
earlier open decision in favor of "no monitor steps.")

Agent 4 also writes `analysis.md` (full reasoning) and `action_steps.md`.

**Terminal-state propagation (empty / halt) — see §6a; do not let a no-trade day fire the error email.**

---

## 6. Boundary 5 → 6 — `action_steps_validation.json` (+ `validation_status.json`)

**Producer:** Agent 5. **Consumer:** Agent 6 (report) + orchestration (status signal).

**The fix — this boundary is currently broken and HALTs every run.** Agent 5 emits a top-level
**array** of `{step_id, verdict, ...}`; Agent 6 reads a top-level **object** with `slug`,
`overall_status`, and `steps[].status`. They were generated from two different schema versions.
Canonical form below: an **object** wrapping Agent 5's richer per-step structure, with `slug` and
a `status` field name. PARTIAL is dropped (Agent 5 never produced it; partial sequences are
`UNMATCHED` with a gap description).

```jsonc
{
  "slug": "2026-06-18_14-30-00", // from graph state — Agent 5 MUST write this
  "overall_status": "PASS", // advisory only; Agent 6 recomputes from steps
  "steps": [
    {
      "step_id": "A001",
      "status": "MATCHED", // MATCHED | UNMATCHED  (no PARTIAL)
      "tool_sequence": [
        // non-empty on MATCHED; null on UNMATCHED
        {
          "tool_name": "place_order",
          "server": "alpaca",
          "input_parameters": {
            "symbol": "NVDA",
            "notional": 1500.0,
            "side": "buy",
            "type": "market",
            "time_in_force": "day"
          }
        }
      ],
      "compensation_sequence": null, // see rule below: required (non-null) iff this step is in an atomic group
      "gap_description": null // null on MATCHED; non-empty string on UNMATCHED
    }
  ]
}
```

**`compensation_sequence` (atomic-group capability check).** For any step with a non-null `group_id`
(§7a), Agent 5 must also verify that a **compensating** tool path exists in the manifest — the tool
that would unwind a filled leg (`BUY→SELL`, etc., per the compensating-action map). It records that
path here as `[{tool_name, server}]` with **no `input_parameters`** (the compensation's parameters are
computed at runtime from the _realized_ fill, not now). Rules:

- Independent step (`group_id == null`): `compensation_sequence` is `null` — no compensation needed,
  because re-evaluation from current state (§7a) handles a lone failure.
- Atomic-group step (`group_id != null`): `compensation_sequence` must be non-null. If no compensating
  capability exists in the manifest, the step is `UNMATCHED` with gap `"no compensating capability for atomic group member {step_id}"`. An un-unwindable atomic group must never reach execution.
- Agent 5 validates compensation **capability only** (the tool exists), never runtime success
  (liquidity, market-open) — that remains the execution stage's concern (§7a). Config addition: an
  `action_type → compensating_action_type` map alongside the existing `action_type → tool` map.

**`validation_status.json`** stays as Agent 5's completion signal **for the orchestration layer
only** (it gates graph advance / node completion). Agent 6 does **not** read it. Keep its schema
as in agent_5.md (`status`, `validation_performed`, `unmatched_steps`, `error`). The two files have
distinct, non-overlapping consumers, so there is no conflict once that's stated.

Write order is unchanged from agent_5.md: `action_steps_validation.json` → `.md` → `validation_status.json` last.

**Agent 6 determination logic against this schema:** read `action_steps_validation.json`; recompute
from per-step `status`: every step `MATCHED` → `PROCEED`; **any** step not `MATCHED` → `HALT`. Treat
a missing/empty/non-array `steps`, an unreadable file, or any unknown `status` literal as a parse
failure → `HALT`, spawn no sub-agent, surface to orchestration (agent_6.md §6.1 unchanged). Get
`slug` from this file (now present) **or** graph state — never fabricate it.

---

## 6a. Terminal-state propagation (the "no-trade day" gap)

Agent 4 has three clean terminal states; only one of them currently survives to execution. The
other two get mishandled as errors, and a normal no-trade day fires the validation-error email.
Define this explicitly so the three states map to three distinct, intended pipeline outcomes.

| Agent 4 state                                     | `action_steps.json`                                             | desired pipeline outcome                                                     | what breaks today                                                                                                      |
| ------------------------------------------------- | --------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Recommendations                                   | array of N step objects                                         | validate → PROCEED or HALT+email on real gaps                                | works (once §5 is fixed)                                                                                               |
| **Clean empty** (all candidates dropped on merit) | `[]`                                                            | **stop quietly, no email, no execution**                                     | Agent 6 sees empty `steps` → §6.1 "empty is never PROCEED" → HALT → **error email**                                    |
| **Halt** (a step could not be executed)           | single object, all fields `null` except `step_id`/`step_failed` | **stop, notify human that analysis halted** (distinct from a validation gap) | Agent 5 reads null `instrument`/`action_type` → "malformed" → Agent 5 halts → Agent 6 parse-fail → orchestration error |

Recommended handling, decided at the orchestration layer (not by overloading Agent 6's gap logic):

- **Clean empty (`[]`):** orchestration detects zero action steps _before_ Agent 5 and routes to a
  clean `NO_ACTION` terminal (log it, optionally a low-priority "nothing today" digest — **not** the
  `MCP Validation Error` email). Agents 5 and 6 are skipped.
- **Halt object:** orchestration detects `step_failed != null` _before_ Agent 5 and routes to a
  distinct `ANALYSIS_HALT` notification (its own subject line and body), not the validation-error
  path. Agents 5 and 6 are skipped.
- **Non-empty real steps:** proceed into Agent 5 / Agent 6 as normal.

Net: the `MCP Validation Error` email should fire **only** for genuine tool-coverage gaps on real
recommendations — never for "no trade today" and never for an analysis halt.

---

## 7. Boundary 6 → execution / email sub-agents

**PROCEED →** Execution sub-agent receives `working_dir` and the validated steps (each MATCHED
step's `tool_sequence` from `action_steps_validation.json` is what actually executes).

**HALT →** Email Notification sub-agent receives `working_dir`, `slug`, `recipient`
(`56kyleoliver@gmail.com`), and the full validation report. Subject convention:
`money-pit: MCP Validation Error - {slug}`. Agent 6 waits for send confirmation before completing.

**`determination.json`** (Agent 6 output) schema is as in agent_6.md §9 (`slug`, `determination`,
`reason`, `failed_steps`, `sub_agent_spawned`, `sub_agent_outcome`, `sub_agent_error`, `timestamp`).
One change: `failed_steps` is populated from steps whose `status == "UNMATCHED"` (PARTIAL removed).
The execution sub-agent's `sub_agent_outcome` maps from the §7a execution outcome:
`EXECUTED_CLEAN`/`PARTIAL_COMPENSATED` → `success`; `COMPENSATION_FAILED`/`EXECUTION_FAILED` →
`failure` (with `COMPENSATION_FAILED` additionally triggering the urgent escalation in §7a).

---

## 7a. Execution transactional model

The determination gate guarantees we never execute a plan with an _unmatched_ capability, so the only
remaining way to land in a bad state is a **runtime** failure inside an already-approved plan. This
section defines how the execution sub-agent stays consistent through that. Two ideas do the work, and
neither is "pre-plan a static inverse for every order" (a market action has no true inverse — slippage,
partial fills, taxes, and one-way doors mean the undo is a fresh action with its own cost and its own
failure mode).

**Principle — independent steps re-evaluate; only interdependent groups get unwound.** A standalone
step that fails is not a half-finished plan: the steps that succeeded were each independently validated
as good, and the failed one simply didn't happen. The snapshot builder reads live state at the top of
every run and Agent 4 sizes from current positions, so the next run re-plans from reality — undoing a
good independent trade would be a _mistake_. Transactional unwinding applies **only** to steps sharing
a `group_id` (pairs trade, hedged entry, funded roll), where a partial fill leaves exposure nobody chose.

**Idempotency.** Every order is submitted with the deterministic `client_order_id = {slug}:{step_id}`
(compensations use `{slug}:{step_id}:comp`). A retry or crash-recovery re-submission is therefore a
no-op at the broker, never a double-execution.

**Execution journal — `execution_journal.json`** (new working-dir artifact, append-only, written
_incrementally_ by the execution sub-agent so a crash leaves a truthful partial record). This is the
adopted half of the Alembic analogy: a durable record of what was actually applied, not a set of
down-migrations.

```jsonc
{
  "slug": "2026-06-18_14-30-00",
  "outcome": "EXECUTED_CLEAN", // EXECUTED_CLEAN | PARTIAL_COMPENSATED | COMPENSATION_FAILED | EXECUTION_FAILED
  "entries": [
    {
      "step_id": "A001",
      "group_id": null,
      "client_order_id": "2026-06-18_14-30-00:A001",
      "phase": "FILLED", // execution-phase enum (§0)
      "intended": { "symbol": "NVDA", "side": "buy", "notional": 1500.0 },
      "broker_order_id": "…",
      "status": "filled",
      "filled_qty": 8.13,
      "filled_avg_price": 184.5,
      "realized_notional": 1500.0,
      "compensation_of": null, // step_id this entry unwinds, if it is a compensation
      "error": null,
      "timestamp": "2026-06-18T14:32:11Z"
    }
  ]
}
```

**Execution algorithm.**

1. Partition steps by `group_id`: independent steps (null) and atomic groups (shared id).
2. **Independent steps:** submit each idempotently; journal each. On failure → journal `FAILED` and
   **continue** to the next step. No compensation; the next run re-evaluates.
3. **Atomic groups, per group:**
   a. **Pre-flight (all-or-nothing):** check every leg's feasibility (buying power, shortability,
   market-open). If _any_ leg fails pre-flight → journal the whole group `SKIPPED`/`PREFLIGHT_FAILED`,
   place **none** of it, flag for notification. (This prevents most partials proactively.)
   b. **Execute legs in safe order** — the leg whose solo-failure is least harmful first (e.g. the
   protective leg before the exposed leg). Journal each submit/fill.
   c. **Mid-flight leg failure (after pre-flight passed):** enter compensation. For every already-filled
   leg in the group, compute a compensating order **from its realized fill** (not the intended size),
   submit idempotently, journal `COMPENSATING`→`COMPENSATED`. Group outcome `PARTIAL_COMPENSATED`.
   d. **Compensation itself fails:** journal `COMPENSATION_FAILED`. This is the one state with
   _unintended exposure the system could not neutralize_ → **urgent** escalation (its own high-priority
   email subject, distinct from the validation-error mail), and the run outcome is `COMPENSATION_FAILED`.
4. Write the final journal `outcome`; record it into `determination.json` per the mapping above.

**Recovery on the next run.** At run start, if a prior `execution_journal.json` shows an incomplete or
`COMPENSATION_FAILED` run, reconcile against current broker positions (the fresh snapshot) before
planning. Because submission is idempotent and the journal is truthful, the system knows exactly what
happened and re-plans from reality — it never blind-replays.

**What this adds elsewhere:** `ActionStep.group_id` + `client_order_id` (§5); `StepValidation. compensation_sequence` + the compensation-capability check (§6); the `execution_journal.json` artifact
and the execution/outcome enums (§0). Agent 5 stays deterministic config-driven code — verifying the
compensation path is one more lookup, not a judgment.

---

## 8. Cross-cutting input — `portfolio_snapshot.json`

**Producer:** `money-pit portfolio` CLI / MCP tool (orchestration). **Consumers:** Agent 2, Agent 4.

Previously TBD; pinning it here so Agent 2's strict input validation has a target. **Agent 4 is the
heavier consumer** and needs more than Agent 2 — three additions/decisions below.

```jsonc
{
  "slug": "2026-06-18_14-30-00",
  "as_of": "2026-06-18T14:30:00Z",
  "total_account_value": 102000.0, // REQUIRED by Agent 4: Step 3 (25% limit) and Step 7 (base = ÷20)
  "available_cash": 5120.45,
  "positions": [
    {
      "ticker": "NVDA",
      "quantity": 40,
      "cost_basis": 110.2,
      "current_value": 7368.0,
      "unrealized_pl": 2960.0, // Agent 4 lists this as an expected per-position field
      "sector": "semiconductors", // per-position sector classification
      "factor_tags": ["growth", "momentum"] // per-position tags; aggregate is derived from these
    }
  ],
  "sector_weights": { "semiconductors": 0.31, "regional_banks": 0.08 }, // fractions summing ~1.0
  "correlated_overlaps": [{ "tickers": ["NVDA", "SMH"], "note": "ETF holds the single-name position" }]
}
```

Three reconciliations Agent 4 forces:

1. **`total_account_value` must be explicit.** Agent 4 Step 3 (25% sector cap) and Step 7 (fractional
   Kelly sizing) both depend on it. Don't make agents sum positions + cash themselves.
2. **Per-position `factor_tags`, not a pre-aggregated profile.** Agent 4 Step 3.2 derives the
   portfolio factor profile _from the tags on each position_. So the snapshot should ship the tags;
   the aggregate `factor_exposure` profile (which Agent 2 references) is then a **derived** value —
   compute it deterministically (code) and, if Agent 2 needs it, attach it as a derived field rather
   than a second source of truth.
3. **One factor set, five factors.** Agent 4 uses `growth | value | momentum | quality | low_vol`.
   Agent 2 and my earlier draft used `value | momentum | quality | volatility` (four, no growth,
   `volatility` not `low_vol`). Pin the five-factor set from §0 everywhere; `factor_tags` values and
   any derived profile keys draw from it.

---

## 8a. `MacroIndicators` schema — five-indicator struct consumed by A4 post-processor

A4 reads the `macro_regime` answers from `initial_answers.json` and the post-processor assembles
them into this struct before calling `classify_regime()` (`design_decisions.md §1`). Five indicators;
any missing/stale series leaves the corresponding field `null`, which triggers `UNCERTAIN`.

```jsonc
{
  "yield_curve": -0.41, // T10Y2Y spread in percentage points; null if unavailable
  "credit_spreads": 3.82, // HY OAS in percentage points; null if unavailable
  "pmi": 48.7, // ISM Mfg PMI level; null if unavailable
  "earnings_revisions": -0.12, // fwd-EPS revision breadth (fraction, negative = net down); null
  "inflation": 3.1, // CPILFESL YoY % or ISM prices-paid index; null if unavailable
  "as_of": "2026-06-26" // date of the most recent data point used
}
```

This struct does not land on disk as a named file — it is assembled inline by the post-processor
from the five `macro_regime` answers in `initial_answers.json` (`data_retrieved` field).

---

## 9. Deterministic-flag / tooling split

These computations are currently done by an LLM but are **mechanical**. Move them to code
(post-process or orchestration node) to delete a class of self-consistency bugs and prompt-burden.
The contract above is unaffected by _where_ they run — only by getting the values right. (§11 is the
full architectural treatment — the complete per-agent split, tool/function inventories, and the
structural-enforcement principle. This table is the quick reference.)

| computation                                                       | currently in                      | move to                        | rule                                                                                                                                                                            |
| ----------------------------------------------------------------- | --------------------------------- | ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `requires_validation`                                             | source adapter (LLM)              | code post-process              | `tier in {high, medium}`                                                                                                                                                        |
| `has_actionable_content`                                          | source adapter / aggregator (LLM) | code post-process              | per-source then run-level: any high/medium                                                                                                                                      |
| ticker normalization                                              | source adapter (LLM)              | code post-process              | uppercase, strip punctuation, dedupe                                                                                                                                            |
| signal counts                                                     | Agent 2 (LLM)                     | code post-process              | count by tier                                                                                                                                                                   |
| category → tool routing                                           | Agent 3 (LLM)                     | code lookup table              | the routing table in §3                                                                                                                                                         |
| `confidence`                                                      | Agent 3 (LLM)                     | code from `sources_used`       | primary (FRED/EDGAR/Alpaca)→`high`; yfinance/combined/inferred→`medium`; Brave-only/partial/unanswered→`low`; rate by weakest materially-relied source                          |
| **Agent 5 checks 1,2,4** (existence + literal schema field match) | Agent 5 (LLM)                     | **code (`jsonschema`)**        | MCP `inputSchema` is JSON Schema → `jsonschema.validate()`; LLM is the _wrong_ tool for "never map `share_count`→`quantity`"                                                    |
| **Agent 6 determination**                                         | Agent 6 (LLM)                     | **LangGraph conditional edge** | pure `all(MATCHED) ? PROCEED : HALT`; no model call needed                                                                                                                      |
| **Agent 4 EV**                                                    | Agent 4 (LLM)                     | **code post-process**          | `EV = Σ(Pᵢ/100 × Rᵢ)`; then apply the ≥ +3.0% gate                                                                                                                              |
| **Agent 4 constraint extraction**                                 | Agent 4 (LLM)                     | **code**                       | sector headroom to 25% in $ and %, cash %, overlap reductions — all arithmetic from the snapshot                                                                                |
| **Agent 4 position sizing**                                       | Agent 4 (LLM)                     | **code post-process**          | fractional Kelly: `w = kelly_fraction × h_unverified × h_uncertain × f_kelly`; clamp to `max_position_weight`; then to 25%/cash/overlap headroom. See `design_decisions.md §2`. |
| **Agent 4 `execution_parameters`**                                | Agent 4 (LLM)                     | **code post-process**          | emit manifest-correct keys from `ticker`/`action_type`/`dollar_amount` — fixes §5b/§5c in one place                                                                             |
| factor profile aggregation                                        | (new)                             | **code**                       | sum/normalize per-position `factor_tags` into the five-factor profile                                                                                                           |

What stays LLM: source-adapter classification, the aggregator's thin corroboration label, Agent 2 question authoring, Agent 3 retrieval +
sufficiency judgment, **Agent 4's _judgments_ only** (which claims survive each gate, scenario
probabilities/returns, conviction level, thesis text), and **Agent 5 check 3 only** (behavioral/
semantic match between a tool's description and an action's intent — code can't judge that).

The clean shape for **Agent 4** is the same hybrid as Agent 5: the model decides _what_ and _how
confident_; a deterministic post-processor does every multiplication, gate, and clamp, and writes
the execution params. That keeps "two runs on identical inputs produce identical outputs" (Agent 4's
own determinism rule) actually true, which a model doing the arithmetic cannot guarantee.

The clean shape for **Agent 5** is: LLM proposes candidate tools by behavioral match; deterministic
code verifies existence and literal schema acceptance.

---

## 10. Change list per agent

- **Source adapters (was Agent 1)** — become one adapter per source type, each emitting the generic
  `SignalSet` (§2): flat `claims[]` with run-namespaced `claim_id` + explicit `tier`, structured
  `source_ref`, and `cited_sources`. The **video adapter** owns the multimodal extraction; lighter
  adapters are parse-and-classify. Echo `slug`. Hand the deterministic flags to code (§9).
- **Aggregator (new node)** — union the per-source `SignalSet`s into `aggregated_signals.json`:
  re-ID claims run-global, compute run-level `has_actionable_content`, reconcile tier (max across a
  corroboration), and produce `corroborations`/`conflicts` via embedding-cluster + thin-LLM confirm (§9).
- **Agent 2** — **read `aggregated_signals.json`** (not `transcript_summary.json`); adopt the 5
  snake_case categories; rename output fields to `data_sources` and `rationale`; **also write
  `initial_questions.md`**; anchor `current_events` recency to each claim's `source_ref.published_at`;
  validate `portfolio_snapshot.json` against §8; use the five-factor set (§0), not `volatility`;
  emit **five** `macro_regime` questions (one per indicator: yield curve, credit spreads, PMI,
  earnings revisions, inflation — see §8a).
- **Agent 3** — read `data_sources` and `rationale`; route on the 5 canonical categories via the
  §3 table; carry `category`/`signal_source`/`signal_tier` through into `initial_answers.json`
  (top level now carries `sources`, not a single `published_at`); fix illustrative IDs to `Q001` style.
- **Agent 4** — read `aggregated_signals.json`; tier is `tier`=`high`/`medium`/`low` (not
  `classification`=`high-signal`); per-claim date is `source_ref.published_at`; `low` claims are **not**
  pre-stripped (filter them yourself); use `corroborations` as a positive input to Step 1. Join answers
  on `claim_id`↔`signal_source` (§4). **Rework `action_steps.json` per §5:** add `step_id`, rename to
  `instrument`/`action_type`/`description`, add an `execution_parameters` block with manifest-correct
  keys, and relax "no additional keys." Move EV / sizing / constraint arithmetic and execution-param
  emission to a deterministic post-processor (§9). Read `total_account_value` and per-position
  `factor_tags`/`sector`/`unrealized_pl` from the snapshot (§8). Route the empty/halt terminal states per §6a.
- **Agent 5** — wrap output as an **object** with `slug` + `steps[]`; rename `verdict` → `status`;
  keep `tool_sequence`/`gap_description`; write `slug`; keep `validation_status.json` as the
  orchestration-only signal. **Add `compensation_sequence` and the atomic-group compensation-capability
  check (§6/§7a)** with an `action_type → compensating_action_type` config. (Consider extracting checks
  1/2/4 to code per §9.)
- **Agent 6** — read the §6 object schema (`steps[].status`, values `MATCHED`/`UNMATCHED`); treat
  anything not `MATCHED` as HALT; get `slug` from the file or graph state; map the execution sub-agent's
  §7a outcome into `sub_agent_outcome`. (Consider replacing the node with a conditional edge per §9.)
- **Execution sub-agent** — implement the §7a transactional model: partition by `group_id`, idempotent
  submission via `client_order_id`, the incremental `execution_journal.json`, atomic-group pre-flight,
  realized-fill compensation, and the `COMPENSATION_FAILED` urgent-escalation path.
- **Orchestration** — owns slug/working-dir, the deterministic post-processing of §9, the
  `money-pit portfolio` snapshot (§8), and reading `validation_status.json` to advance the graph.
  Also routes Agent 4's three terminal states (§6a): real steps → Agent 5/6; clean empty `[]` →
  `NO_ACTION` (no email); halt object → `ANALYSIS_HALT` notification (distinct from the validation-
  error email). On run start, runs §7a recovery: reconcile a prior incomplete `execution_journal.json`
  against the fresh snapshot before planning.

---

## 11. Maximal logic extraction (LLM → tools & functions)

Goal: the LLM does **only irreducible judgment** — turning free text into structure, and forming
views. Everything deterministic becomes an **MCP tool** or a **background function**. This makes the
pipeline reproducible, auditable, cheaper, and safer.

**Where each piece of logic goes — the decision rule:**

- **MCP tool** when it is an external integration (brokerage, market data, email), must be _callable
  on demand_ by a model mid-reasoning, or is shared across stages/processes.
- **Background function** when it is a deterministic transform at a _fixed_ pipeline point, fully
  determined by data already in the working dir / graph state, with no model call needed at call time.
- **LLM** only when the input is open-ended natural language or the output is a genuine _view/estimate_.

**Structural enforcement (most important):** safety and scope limits are guaranteed by _tool
exposure_, not prompt text. Read-only stages get clients with no write tools. Only the execution
stage holds the Alpaca order tools. "Please don't place orders" must never be the only thing between
analysis and the brokerage.

### Per-agent decomposition

**Source adapters (formerly Agent 1) — one per source type.** Each adapter turns one input into a
`SignalSet`. The **video adapter** is the heaviest: _LLM (irreducible):_ comprehend the (multimodal)
content, identify substantive claims, assign tier and category, write claims in own words, write the
source summary, extract tickers/sectors/macro-themes, and fill `cited_sources` from on-screen
attribution. _MCP tool:_ `resolve_ticker(name_or_symbol)` — kills the Whisper-homophone error mode.
_Background functions:_ `requires_validation`, per-source `has_actionable_content`, ticker
normalization, ISO-date parsing, keyframe sampling + OCR (the deterministic source-attribution layer),
schema validation. Lighter adapters (newsletter/RSS/PDF) are mostly parse-and-classify and may need
only a thin LLM for the classification step. **Every adapter emits the same `SignalSet` contract**, so
the rest of the pipeline never branches on source type.

**Aggregator (new node).** _Background functions:_ union the per-source `SignalSet`s, re-ID claims to
the run-global namespace, compute the run-level `has_actionable_content`, reconcile tier (max across a
corroborated claim). _LLM (thin, the one new judgment surface):_ corroboration/conflict detection —
embed claims, cluster by cosine similarity (deterministic), and use a _thin_ LLM pass only to confirm
and label `agree`/`disagree` **within** a cluster. Output: `aggregated_signals.json`.

**Agent 2 — Questions.** _LLM (irreducible):_ author the _claim-specific_ `thesis_validation` and
`invalidation_conditions` questions anchored to each claim's numeric/mechanistic substance — that's
the only generative part. _Background functions:_ input validation; the insufficient-signal gate
(driven by `has_actionable_content` — skip the node entirely, no LLM); signal counts; **template
emission** for the standing questions that are the same every run — the **five** `macro_regime`
questions (one per indicator: yield curve, credit spreads, PMI, earnings revisions, inflation —
constant), the per-ticker `current_events` questions (anchored to each claim's `source_ref.published_at`),
and the per-position `portfolio_gap` questions (filled from the snapshot); ordering, `Q###` numbering,
empty-category placeholders, `answer: null` init, `data_sources` assignment from the §3 routing table.

**Agent 3 — Retrieval.** _LLM (irreducible, thin):_ only the open-ended questions whose retrieval
needs relevance judgment (free-text Brave/EDGAR lookups) plus short answer synthesis. _MCP tools:_
the existing read-only data sources, **plus** a `get_macro_regime_indicators()` convenience tool that
bundles the five standard series (yield-curve shape, credit-spread direction, PMI trend, real
earnings-revision direction, inflation — FRED `CPILFESL` or ISM prices-paid) into one deterministic call. _Background functions:_ the category→tool
routing table; **deterministic retrieval** for every question that carries a known tool+params
(named FRED series, current price, P/E) — fetched by code, no model; `confidence` from `sources_used`;
call-budget control; output validation. _Structural:_ read-only Alpaca is enforced by exposing only
market-data tools to this stage.

**Agent 4 — Analysis.** _LLM (irreducible):_ Step 1 supported/contradicted/unverified judgment per
claim (after a code join on `claim_id`↔`signal_source`); Step 4 thesis narratives; the Step 5
scenario probabilities and returns; Step 6 invalidation-condition authoring. _Background functions —
"the post-processor":_ Step 2 regime **tagging** as a decision table over the indicators (any
missing/conflicting → `UNCERTAIN`); Step 3 constraint extraction (sector headroom to 25% in $ and %,
cash %, overlap reductions); Step 5 `EV = Σ(Pᵢ/100 × Rᵢ)` and the ≥ +3.0% gate; Step 7
fractional-Kelly sizing (`w = kelly_fraction × h_unverified × h_uncertain × f_kelly`, clamped to
`max_position_weight`, then to §3 headroom — see `design_decisions.md §2`); action mapping (held+bullish → ADD, not-held+bullish →
BUY, held+bearish → TRIM/SELL); probability-sums-to-100 check; **`execution_parameters` emission with
manifest-correct keys** (this is also the §5b/§5c fix). The model emits judgments; code does every
multiplication, gate, clamp, and the schema. This is the only way Agent 4's own "identical inputs →
identical outputs" rule can actually hold.

**Agent 5 — Validation → becomes a background function (no LLM).** With a fixed Alpaca tool set, the
`action_type → tool` mapping is _config_, so check 3 (behavioral match) is a lookup, and checks 1/2/4
(existence + literal field-name match + sequence sufficiency) are exactly what code does reliably and
a model does not. Keep an LLM here only if your tool set is open-ended and you need fuzzy matching to
unknown tools — not your case. The validator reads the manifest (auto-generated from registered MCP
defs) and writes the three files. Optionally expose it as a `validate_action_steps(steps, manifest)`
tool, but a graph-node function is the natural home.

**Agent 6 — Determination → becomes a conditional edge (no LLM).** `all(MATCHED) ? PROCEED : HALT`,
plus parse-failure handling and the §6a terminal-state routing. No model call.

**Sub-agents.** _Execution:_ a background loop that calls the Alpaca **order** MCP tools over the
MATCHED `tool_sequence`, checking each structured result and halting on rejection/oddity — no model
needed to place an order with known params. _Email:_ the communication MCP `send_email` tool with a
**templated** body from the validation report (subject `money-pit: MCP Validation Error - {slug}`);
an LLM for prose is optional, not required.

### Consolidated MCP tool inventory

Almost all integrate; only a small email server is built. The Alpaca read/write split is **toolset
scoping on the one official server** (`ALPACA_TOOLSETS`), not two servers.

| server               | build/integrate                                   | tools                                                                      | exposed to                | read/write |
| -------------------- | ------------------------------------------------- | -------------------------------------------------------------------------- | ------------------------- | ---------- |
| FRED                 | integrate (community)                             | series fetch + economic-snapshot (covers `get_macro_regime_indicators`)    | Agent 3 / macro bundle    | read       |
| SEC EDGAR            | integrate — edgartools built-in MCP               | filings, financials, insider, ticker/CIK resolve (covers `resolve_ticker`) | Agent 3 / adapters        | read       |
| yfinance             | integrate (community)                             | fundamentals, price history, earnings                                      | Agent 3                   | read       |
| Brave                | integrate (official)                              | web search                                                                 | Agent 3                   | read       |
| Alpaca (read scope)  | integrate official, `ALPACA_TOOLSETS`=market-data | account snapshot, quotes, trades, assets                                   | snapshot builder, Agent 3 | read       |
| Alpaca (write scope) | integrate official, `ALPACA_TOOLSETS`=trading     | place/get order                                                            | **execution stage only**  | write      |
| communication        | **build (small)** FastMCP or smtplib              | `send_email(...)`                                                          | email stage only          | write      |

`execution_parameters` field names are pinned to the official Alpaca server's OpenAPI order schema.
A5's schema-acceptance check is `jsonschema.validate(params, tool.inputSchema)` — MCP `inputSchema`
is JSON Schema — so only the `action_type→tool` routing + compensation lookup are custom.

### Consolidated background-function inventory

| stage                        | functions                                                                                                                                                    |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| A1 post                      | derived flags, ticker normalization, date→ISO, schema validation                                                                                             |
| A2 (mostly)                  | input validation, signal gate/counts, macro/current-event/portfolio-gap **templating**, ordering, IDs, placeholders, routing-table `data_sources`            |
| A3 wrap                      | routing table, deterministic known-param retrieval, `confidence` derivation, budget control, schema validation                                               |
| A4 post (the post-processor) | regime decision-table tag, constraint extraction, EV + gate, sizing, action mapping, prob-sum check, **`execution_parameters` emission**, schema conformance |
| A5 (entire)                  | manifest parse, existence + literal-schema + sequence checks, `action_type→tool` lookup, verdict/gap assembly, three-file write + self-validate              |
| A6 (entire)                  | determination conditional edge, §6a terminal-state routing                                                                                                   |
| sub-agents                   | execution loop (calls trading tools), email templating                                                                                                       |

### What this leaves as LLM, and where not to over-extract

After extraction, only four thin LLM cores remain: **A1** (text → structured claims), **A2** (the
claim-specific questions only), **A3** (open-ended retrieval + synthesis), **A4** (the four judgment
outputs). **A5 and A6 stop being LLM agents entirely.**

Honest trade-offs to keep in view:

- Templating A2/A3 trades adaptability for reproducibility. Keep the LLM for _claim-specific_ and
  _open-ended_ items; template only the _standing_ questions that recur identically every run. If a
  show raises something the templates don't anticipate, that path still goes through the model.
- The regime decision table and the numeric conviction bands require you to _define the thresholds
  up front_. That's a feature — it makes the two most consequential judgments (regime, sizing)
  auditable and reproducible — but it is real design work, not a freebie.
- A5-as-config means adding a tool or action type is a config edit, not a prompt edit. Good, but it
  must actually be maintained alongside the Alpaca tool set.
- Do **not** push claim identification, tier/category classification, thesis formation, or scenario
  estimation into code. Those are the irreducible judgments; rule-ifying them is how you get a system
  that is deterministic and wrong.
