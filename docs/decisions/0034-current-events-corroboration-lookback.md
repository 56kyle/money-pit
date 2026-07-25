---
status: accepted
date: 2026-07-25
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Current-Events Evidence Window Is Anchored Before Publication

## Context and Problem Statement

A2 emits one `current_events` question per high/medium claim, asking what has happened "since" a cutoff timestamp. That cutoff was the source's `published_at` verbatim. For the YouTube ingestion path, `published_at` comes from `_upload_date_to_iso` (`ingestion/fetch.py`), which converts yt-dlp's date-only `YYYYMMDD` upload date to **UTC midnight of the upload day** — the only precision yt-dlp offers.

The consequence is a same-day run searching a window a few hours wide. In run `2026-07-24_15-42-21`, the video published `2026-07-24T00:00:00Z` and A3 executed at `15:40Z`; all seven `current_events` questions returned some variant of "Could not retrieve any confirmed material developments since 2026-07-24T00:00:00Z," each with `data_retrieved: null` and `confidence: "low"`.

The window is not merely narrow — it is anchored on the wrong side of the evidence. The claims describe events that have **already occurred**: "Tesla fell about 14.5–15% in one session," "Google reported its first negative free cash flow since 2004." The reporting that would corroborate them was published *before* the video, because the video is a reaction to it. A cutoff at the publication-day midnight excludes the corroborating evidence by construction, and no amount of waiting for the search index to catch up fixes it.

Downstream, an uncorroborated claim takes `haircut_unverified` in A4 (`config.haircut_unverified = 0.5`). So every claim from every run was being halved for lack of evidence that the pipeline had structurally forbidden itself from finding.

What should anchor the `current_events` evidence window?

## Decision Drivers

- The window governs what counts as corroboration for a claim that will be sized with real capital. It must be wide enough to reach the evidence and narrow enough that stale coverage cannot masquerade as confirmation.
- Publication timestamps from the ingestion path carry day-level precision at best. Any design that assumes intraday precision on `published_at` is unsound for this source type.
- A malformed or absent upload date must not drop a capital-relevant question. Losing a question silently removes a claim's only corroboration path and hands it a haircut on a technicality.
- The window must not perturb the horizons that A2/A3 LLM-authored questions set for themselves ("over the last 1 month and 3 months," "last 5 trading days"), nor the `macro_regime` path, which fetches FRED's single latest observation with no date range at all.

## Considered Options

- Backdate the cutoff by a configured lookback from `published_at`
- Anchor on `retrieved_at` instead of `published_at`
- Split corroboration into a separate question category with its own window
- Leave the anchor and widen only forward (i.e. accept the run-time end of the window as the fix)

## Decision Outcome

Chosen option: **backdate the cutoff by a configured lookback**. `Config.current_events_lookback_days` defaults to **7**, and `_evidence_cutoff_text` in `pipeline/questions.py` emits `published_at − lookback_days` as the cutoff in the question text.

Seven days is the smallest value that survives the pipeline's actual cadence:

| Case | Calendar days back to the session the claim describes |
| ---- | ----- |
| Weekday video about the prior session | 1–2 |
| Monday video about Friday's session | 3 |
| Post-holiday Tuesday video about the prior Friday | 4 |

Seven covers all of these with margin while remaining inside "recent news," so genuinely stale coverage still cannot pass as corroboration. The value is a `Config` field rather than a module constant because it is a capital-relevant knob and belongs beside `regime_lookback`, `haircut_unverified`, and the thresholds; it is overridable via `MONEY_PIT__CURRENT_EVENTS_LOOKBACK_DAYS`.

`make_questions_node` takes `current_events_lookback_days` as a **required keyword-only** argument with no default. A default at both the `Config` layer and the node layer is how the two silently drift apart, and the resulting divergence would be invisible — it manifests only as a subtly different string inside a question.

### The semantic shift

With the anchor moved before publication, a `current_events` answer can now be satisfied by the reporting the video was reacting to. "Corroborated" therefore comes to mean *the underlying event is real* in addition to *the thesis survived subsequent developments*. This is a real change in what the flag asserts and is accepted deliberately: under the old anchor the default outcome was "uncorroborated" for every claim, which made the flag carry no information at all.

This is not circular corroboration. The corroborating hit is independent primary reporting; the source video is not itself a search result, and A1's claims are extracted from the transcript rather than from the underlying articles. The residual risk — an A3 answer citing the source video back as its own confirmation — is a prompt-level concern, not a windowing one, and is left open below.

### Fail-soft on an unparseable date

`_evidence_cutoff_text` handles three cases explicitly:

- `None` → the existing `"the source publication date"` fallback text, unchanged.
- Parseable ISO string → an aware value is converted to UTC, a naive value is *declared* UTC via `replace(tzinfo=timezone.utc)`; then backdated and re-emitted as `ISO_UTC_FORMAT`.
- Unparseable string → `logger.warning` carrying the raw value, and the same `"the source publication date"` text the `None` case returns.

The third case is a deliberate soft failure and the one asymmetry in this repo's otherwise fail-closed posture. Failing closed here would mean dropping the question, which *guarantees* the claim is haircut as unverified. Dropping a question is strictly worse than asking a suboptimal one. The malformed value does **not** reach the question text: `"...since not-a-date?"` is an incoherent search directive for A3 and lands in `initial_questions.json` looking like a legitimate cutoff. The raw value is preserved in the warning, which is where a bad date belongs.

No `Z`-to-`+00:00` rewriting happens. `requires-python = ">=3.11"` and `datetime.fromisoformat` accepts a trailing `Z` natively from 3.11 on, so the normalization could never change a parse outcome.

The naive branch stamps UTC explicitly rather than passing the value through, because the emitted `Z` suffix asserts UTC either way; the assertion belongs in the code that makes it. Date-only strings such as `"2026-07-24"` parse naive and take this branch.

`ISO_UTC_FORMAT` lives in `money_pit.constants` and is shared with `ingestion/fetch.py`, which produces the exact wire format this consumes. The two are coupled and must not drift.

### Consequences

- Good, because `current_events` corroboration becomes reachable at all; the flag now carries information rather than being uniformly negative.
- Good, because claims backed by real, findable reporting stop taking `haircut_unverified`, correcting a systematic undersizing of well-documented signals.
- Good, because the window is a config knob, so retuning it after observing live corroboration rates is not a code change.
- Bad, because "material developments since" now spans a period partly *before* the claim was made, so the question's phrasing is looser than its literal reading — the answer conflates event-is-real with thesis-still-holds.
- Bad, because a malformed upload date yields an unanchored question — the same one an absent date yields — visible only in a warning rather than as a failure.
- Neutral, because no threshold, haircut, Kelly fraction, or sizing input changes. The blast radius is the cutoff string in one question category.

### Confirmation

`tests/unit_tests/pipeline/test_questions.py` pins `_evidence_cutoff_text` across lookbacks 0/1/7/30 (four distinct expectations, so no baked-in constant passes), offset-bearing and non-UTC-offset inputs, the `None` fallback, and the unparseable-date path — the last asserting *both* that the warning fires carrying the raw value and that the question is still emitted, carrying the missing-date fallback text rather than the raw string. `tests/unit_tests/test_config.py` pins the default of 7, the `MONEY_PIT__CURRENT_EVENTS_LOOKBACK_DAYS` override through `load_config`, and the `ge=0` rejection of a negative lookback — negative values would move the cutoff *forward* of publication, re-creating the bug this ADR exists to fix.

The `build_graph` pass-through is not directly pinned. `make_questions_node` returns a closure, so its configuration is unobservable from outside without either white-box introspection of the closure cell or an acceptance-shaped full-graph run; both contort the test around the shape rather than pinning a contract. The forwarded value is pinned by the config test and the behavior it drives by the questions tests. Should this wiring ever need pinning for real, the node would want to expose its configuration rather than the test working around its opacity.

## Pros and Cons of the Options

### Backdate the cutoff by a configured lookback

- Good, because it puts the window where the evidence actually is, with one string changing and no new question category.
- Good, because the lookback is tunable without a code change once live corroboration rates are observable.
- Bad, because the question's "developments since" phrasing now understates what the window covers.

### Anchor on `retrieved_at` instead of `published_at`

- Good, because `retrieved_at` has genuine intraday precision, unlike the midnight-stamped `published_at`.
- Bad, because it anchors on when *the pipeline happened to run*, making the window a function of scheduling rather than of the claim. A re-run days later would silently ask a different question about the same claim, and the artifacts would not be comparable across runs.
- Bad, because it moves the anchor in the wrong direction — later, not earlier — worsening the actual problem.

### A separate corroboration question category

- Good, because it would cleanly separate "is this event real" from "has the thesis survived," letting each carry its own window and its own downstream weight.
- Bad, because it is a substantially larger change: a new `QuestionCategory`, a routing-table entry, A3 prompt guidance, and an A4 decision on how two flags combine into one haircut.
- Deferred rather than rejected — see below.

### Widen only forward

- Good, because it requires no change at all beyond running later in the day.
- Bad, because it does not address the anchor. The corroborating reporting precedes publication; no end-of-window extension reaches it.

## More Information

The `haircut_unverified` mechanism this corrects is defined in [ADR 0005](0005-a4-disposition-and-macro-read.md). The `macro_regime` retrieval path, which this change does not touch, is governed by [ADR 0033](0033-pmi-regional-fed-composite.md) and [ADR 0001](0001-regime-scalar-threshold-v0.md).

Two follow-ups are open and named:

1. **A3 must not cite the source video back as its own corroboration.** With the widened window this becomes reachable in principle. It is a prompt-level constraint in `agent_3.md`, not a windowing one.
2. **Splitting event-is-real from thesis-still-holds** remains the more principled end state. This ADR's decision is the correct behavior under a single window, not a stepping stone toward the split — the split should be taken up on its own merits if the conflation proves to cost real precision in A4 sizing.
