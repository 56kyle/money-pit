# money_pit — Resolved Design Decisions (architecture §15)

These resolve the **blocking** open decisions so the `compute/` modules can be built.

**Governing principle (per owner): no magic numbers.** A threshold is never a bare literal in
`compute/` code. Wherever a cutoff is meaningful only relative to context, it is **derived from the
data's own distribution** rather than fixed; the few values that remain fixed are either _spec_ (an
explicit, auditable decision table) or _risk policy_ (hard limits the owner sets), and those live in
`Config`, surfaced and overridable — never inline. The result is a small, named configuration surface
(see the last section) instead of constants scattered through the deterministic layer.

These encode the owner's risk preferences and are a v0 to calibrate against real data — robust
engineering, not investment advice, and no substitute for a sound edge.

Status of §15: #1, #4, #5, #15 resolved; #2 and #3 designed below (they blocked `compute/regime.py`
and `compute/sizing.py`); the rest stand as in architecture §15.

**Status of §15 open decisions after the conformance review.** Several §15 items were subsequently
resolved during remediation and now carry their rationale in accepted ADRs rather than here: the
execution-journal semantics of #9/#12 (nullable `outcome`, submission-level `EXECUTED_CLEAN`,
`AtomicGroupNotSupportedError`) in **ADR 0003**; the #1 order-schema manifest (static pinned, A5
fail-closed) in **ADR 0004**; A4's Step-1 disposition threading and macro-read-as-narrative in
**ADR 0005**; and the go/no-go determination + finalizer with the 4-member `TerminalState` in
**ADR 0006**; and the re-enforcement of the #1 order-schema gate to fail closed while the committed
stub is unpinned (in-band sentinel + `AlpacaOrderSchemaNotPinnedError`) in **ADR 0007**. The narrative
docs (`architecture.md`, `pipeline_contracts.md`) describe the as-built design; these ADRs hold the "why."

---

## 1. Macro regime classification → `compute/regime.py` (§15 #2)

**Two layers: data-driven discretization (tunable) + a fixed truth table (spec).** The fragile version
of this would hardcode levels like "PMI > 50" or "spreads > 400bps." Those are only meaningful against
history and drift with the regime. So the **discretization** of each indicator to a signal in
`{+1, 0, -1}` is computed from the indicator's **own trailing distribution**, and only the mapping from
the five-signal vector to a regime tag is a fixed rule table.

### 1a. Discretization (data-driven)

`discretize(series, orientation) -> {+1, 0, -1, missing}` combines an indicator's **level** (its
position in its trailing window) and **momentum** (recent change) into a standardized score, then bands
it around zero:

- `score = z_level(series, window) + z_momentum(series, window)` (a trailing z-score of the latest
  level, plus a z-score of its recent change).
- `signal = +1 if score > band; -1 if score < -band; else 0`, then multiplied by the indicator's fixed
  **orientation** (so "expansionary" is always `+1` regardless of whether high raw values are good).
- Insufficient history or a stale series → `missing`.

Config knobs (the _only_ tunables here): `regime_lookback` (trailing window) and `regime_band`
(neutral-zone half-width, in z units). `orientation` is a fixed per-indicator property, not a tunable:

| indicator          | source                                 | orientation (what `+1` means)          |
| ------------------ | -------------------------------------- | -------------------------------------- |
| yield-curve shape  | FRED `T10Y2Y`                          | steeper = `+1`                         |
| credit spreads     | FRED `BAMLH0A0HYM2` (HY OAS)           | **tighter** = `+1` (inverted polarity) |
| PMI                | ISM Mfg / S&P Global                   | higher & rising = `+1`                 |
| earnings revisions | fwd-EPS revision breadth               | up = `+1`                              |
| inflation          | FRED `CPILFESL` YoY or ISM prices-paid | **cooling** = `+1` (inverted polarity) |

> The fifth (inflation) indicator is required: the other four are all growth/stress signals, so without
> it `STAGFLATION` can never be distinguished from ordinary deceleration and would silently collapse to
> `UNCERTAIN`. (If you'd rather not add it, drop `STAGFLATION` from the `RegimeTag` enum so the taxonomy
> matches what the indicators can actually separate — don't leave a tag that can never fire.)

### 1b. Truth table (fixed spec)

This is intentionally fixed — it is the specification, the opposite of a magic number, and there is no
distribution to derive it from. Signals are `[curve, credit, pmi, earnings, inflation] ∈ {+1,0,-1}`;
`growth = pmi + earnings`. Ordered, first full match wins:

| #   | tag                   | curve                       | credit | pmi  | earnings | inflation | signature                                                                                                       |
| --- | --------------------- | --------------------------- | ------ | ---- | -------- | --------- | --------------------------------------------------------------------------------------------------------------- |
| 0   | `UNCERTAIN`           | — any indicator `missing` — |        |      |          |           | insufficient/stale data                                                                                         |
| 1   | `LATE_CYCLE_STRESS`   | `-1`                        | `-1`   | `≤0` | `≤0`     | any       | inverted curve + widening credit, growth rolling over                                                           |
| 2   | `STAGFLATION`         | any                         | `≤0`   | `≤0` | `≤0`     | `-1`      | growth weak **and** inflation hot                                                                               |
| 3   | `GROWTH_ACCELERATING` | `≥0`                        | `≥0`   | `+1` | `+1`     | `≥0`      | demand & profits rising, no credit stress, inflation not hot                                                    |
| 4   | `RECOVERY`            | `+1`                        | `+1`   | `≥0` | `≥0`     | `≥0`      | steep + tightening + an up-inflection (pmi/earnings moved `-1`→`≥0` within lookback)                            |
| 5   | `GROWTH_DECELERATING` | any                         | `≥0`   | `≤0` | `≤0`     | `≥0`      | softening without acute stress or hot inflation                                                                 |
| —   | **conflict guard**    |                             |        |      |          |           | if `(curve+credit) ≥ +1` while `growth ≤ -1`, or `(curve+credit) ≤ -1` while `growth ≥ +1` → `UNCERTAIN`        |
| 6   | signed-sum fallback   |                             |        |      |          |           | `total = curve+credit+pmi+earnings`: `>+1`→`GROWTH_ACCELERATING`; `<-1`→`GROWTH_DECELERATING`; else `UNCERTAIN` |

Notes for the ADR: rule order matters (acute stress and the inflation overlay must win before the plain
growth paths). Every tag requires a _positive_ signature; anything unmatched is `UNCERTAIN` by
construction — the conservative default — and the audit log records which indicators conflicted or were
missing. **RECOVERY** is the one rule needing trailing state (the inflection); if you don't want to
carry prior-period state in v0, drop rule 4 and early-cycle conditions resolve to `GROWTH_ACCELERATING`,
adding `RECOVERY` back when a trailing trough is tracked. Record which choice you made.

**v0 decision: Rule 4 (`RECOVERY`) is deferred.** Prior-period state is not carried in v0; early-cycle
conditions resolve to `GROWTH_ACCELERATING`. `RECOVERY` is added back when the pipeline tracks trailing
indicator values across runs.

---

## 2. Position sizing → `compute/sizing.py` (§15 #3)

**Continuous fractional-Kelly. No EV bands, no multiplier stairs.** The three-cutoff / `1.5×/1.0×/0.5×`
scheme was exactly the arbitrary, discontinuous hardcoding to avoid (an EV of 5.99% vs 6.01% should not
halve a position). Replace it with sizing that scales smoothly with edge and risk, driven by **one**
parameter.

The A4 scenarios already give a return distribution per thesis — probabilities `p_i` and fractional
returns `r_i` over `{bull, base, bear}` — which yields both the edge **and** its variance for free.

**Full-Kelly weight** maximizes long-run log-growth over that 3-point distribution:

```
f_kelly = argmax_{f ≥ 0}  Σ_i p_i · ln(1 + f · r_i)
```

a concave 1-D problem solved by bisection on `g(f) = Σ_i p_i · r_i / (1 + f·r_i)` (with `g(0)=EV>0`).
Intuition: `f_kelly ≈ EV / Var` for small returns — edge over variance — but the exact log-growth solve
is used because it makes no small-return assumption and is self-limiting: since `r_bear < 0`, the `ln`
term drives the objective to `−∞` before `f` reaches the ruin point, so **Kelly never sizes into a
wipe-out on the bear case by construction.**

**Applied weight** layers the risk dial, the conservative haircuts, and the hard cap:

```
w = clamp( kelly_fraction · h_unverified · h_uncertain · f_kelly,  0,  max_position_weight )
dollars = w · total_account_value         # then clamp to §3 sector / cash / overlap headroom; drop if 0
```

- `kelly_fraction` (κ): the **single global risk dial** — fractional Kelly (e.g. quarter-Kelly) for the
  cushion against estimation error in a coarse 3-point distribution.
- `h_unverified`, `h_uncertain`: multiplicative haircuts (`< 1`, else `1.0`) replacing the old
  band-caps — an unverified claim or `UNCERTAIN` regime shrinks size smoothly instead of snapping a tier.
- `max_position_weight`: a hard per-name cap (risk policy) so Kelly can't over-concentrate on a high
  edge / low variance estimate.
- The `EV ≥ 3%` gate stays as the entry **floor** (a genuine go/no-go); below it the thesis is dropped
  before sizing. Diversification emerges from the caps + the modest fractional-Kelly weights rather than
  from a fixed "÷20" base, which is removed.

**Exits.** `BUY`/`ADD` size _to_ the Kelly target (ADD tops up toward it). `TRIM` reduces _toward_ the
lower Kelly target implied by the weakened thesis; `SELL` is a full exit. The field translation
(notional vs quantity) is §4.

**Prompt note.** This supersedes agent_4 Step 7's "base ÷ 20 × {1.5/1.0/0.5}" arithmetic — but that
arithmetic had already been moved out of A4 into the deterministic post-processor (architecture §9), so
A4's prompt only needs Step 7 reframed to "emit a well-formed scenario distribution and the conviction
inputs; sizing is downstream." Kelly is an implementation detail of `compute/sizing.py` the prompt need
not know. Good-reason prompt change, per the reconciliation rule.

---

## 3. Snapshot vs. live reads (§15 #4) — **RESOLVED**

The run-start `portfolio_snapshot.json` is the single source of truth for all sizing and constraint math
within a run. Live Alpaca reads serve only the current-price element of a question and the actual order
moment — never sizing/concentration decisions. A run's arithmetic stays reproducible against a frozen
snapshot.

## 4. Sell / trim translation (§15 #5) — **RESOLVED**

`SELL` (full exit) closes the position by **quantity** (snapshot `quantity`) to avoid fractional dust.
`TRIM` uses **notional** (dollars to remove) if the official Alpaca order tool accepts notional sells,
else converts to quantity via the snapshot price. Entries are Kelly-sized (§2);
`compute/execution_params.py` emits whichever field the official order schema literally defines (§7).

## 5. Atomic groups at N=1 (§15 #9) — **STUB**

At N=1 the analysis produces single-name independent decisions; interdependent legs are rare. v0: A4
emits `group_id = null` for every step. Build `pipeline/execution.py` independent-path first; the
pre-flight/compensation branch (§7a of contracts) is wired but exercised only once `group_id` is
populated. Define grouping criteria when the first interdependent thesis appears or N>1 makes them likely.

The stub has a **marked terminus** (→ see ADR 0003): a non-null `group_id` fails closed by raising
`AtomicGroupNotSupportedError` **before** any `place_order` call, rather than executing one leg of an
all-or-nothing group. The same ADR makes the execution journal honest at this wave — `outcome` is
**nullable** (`None` = incomplete/crashed) and `EXECUTED_CLEAN` means "all independent legs submitted"
(not filled) pre-Phase-7.

## 6. Tier reconciliation (§15 #15) — **RESOLVED (inert at N=1)**

`max` tier across a corroborated claim, with corroboration raising A4 Step-1 confidence. Inert until N>1
— same horizon as the corroboration stub.

## 7. Alpaca order schema (§15 #1) — **deployment prerequisite**

The shared-loader / co-located-path shape has **landed**: `mcp/alpaca_order_schema.json` is the pinned
artifact and `mcp/order_schema.py` is the single loader (`load_order_schema`, exposing
`ALPACA_ORDER_SCHEMA_PATH`), so the post-processor's `compute/execution_params.py` emission and
`pipeline/validator.py`'s `jsonschema` check share one literal source. A5 validates against a **static,
pinned manifest** (`pinned_manifest()` = `{"place_order": <that schema>}`) rather than live
introspection (→ see ADR 0004); introspecting the registered MCP servers is the Phase-7 swap of the
injected default. The committed schema is still a **stub** — pinning the real OpenAPI-generated schema
from the live Alpaca MCP is the remaining prerequisite, gated so the default production path fails
closed until it lands via an in-band stub sentinel + `AlpacaOrderSchemaNotPinnedError` (→ see ADR 0007;
§7 of contracts).

## 8. Step ID origin — **post-processor assigns, not A4**

A4's `analysis_judgment.json` objects are keyed by `claim_id` (the run-global claim identifier from `aggregated_signals.json`). The post-processor mints `step_id` values (`A001`, `A002`, …) when it materializes `action_steps.json`, alongside the other fields it fills in (`regime_tag`, `dollar_amount`, `execution_parameters`).

**Rationale.** Step IDs are execution-layer identifiers — they match order keys to compensation paths and journal entries. Assigning them in the LLM core (A4) would couple the LLM output to execution concerns and make the IDs non-deterministic under retries. The post-processor, which owns all deterministic enrichment of A4's output, is the single place where execution identity is established. This also means `step_id` ordering reflects the post-processor's processing order rather than the LLM's output order, which is cleaner for idempotency.

**Consequence.** Any downstream reference to a specific action step (A5 validation records, execution journal, `determination.json`) uses `step_id`. Any upstream join back to the originating claim uses `claim_id`. The boundary between the two is the post-processor output file: everything before `action_steps.json` uses `claim_id`; everything after uses `step_id`.

The Alpaca order's `client_order_id` (idempotency key) is `{slug}:{step_id}` — e.g., `2026-06-27_14-30-00:A001`. This makes every order globally unique across runs and locally unique within a run, and allows the execution loop to safely retry without double-filling.

Read the official Alpaca MCP order tool's `inputSchema` (OpenAPI-generated) at integration time; pin
`compute/execution_params.py`'s output keys to it and store the snapshot as a repo artifact (e.g.
`money_pit/mcp/alpaca_order_schema.json`) read by both the post-processor and `pipeline/validator.py`,
so A5's `jsonschema` check and the emission share one literal source.

---

## Configuration surface (the entire tunable set)

The anti-hardcoding payoff: the deterministic layer carries **no** bare cutoffs. Everything tunable is a
named field in `Config` with a documented default; everything fixed is either the regime truth table
(spec) or a hard risk limit (policy).

| knob                                      | governs                                      | kind        |
| ----------------------------------------- | -------------------------------------------- | ----------- |
| `regime_lookback`                         | trailing window for indicator discretization | tuning      |
| `regime_band`                             | neutral-zone half-width (z units)            | tuning      |
| `kelly_fraction`                          | the single global risk dial for sizing       | risk dial   |
| `max_position_weight`                     | hard per-name cap                            | risk policy |
| `haircut_unverified`, `haircut_uncertain` | multiplicative size penalties                | risk policy |
| `ev_gate` (= 3%)                          | entry go/no-go floor                         | risk policy |
| `sector_cap` (= 25%), cash, overlap       | concentration limits                         | risk policy |

Fixed, by design: the regime truth table (§1b) and indicator orientations (§1a) — spec, not magic.
