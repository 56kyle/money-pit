---
status: accepted
date: 2026-07-26
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Determination Persists Its Verdict So Every Node Reads From Disk

## Context and Problem Statement

Nearly every handoff between pipeline nodes is disk-backed. `run_has_actionable_content` also lands
in `aggregated_signals.json`; `recovery_decision` in `recovery.json`; `determination_node`
deliberately *recomputes* its go/no-go from the persisted validation rather than trusting anything
carried in memory. Domain data moves through the run directory, and `PipelineState` carries only
`slug`, `working_dir`, and routing flags.

One handoff broke that pattern. `determination_node` computed `determination`,
`determination_reason`, `failed_steps` and `sub_agent_spawned` and **persisted none of them** —
they went into `PipelineState`, and `make_finalizer_node` read them back out, raising a bare
`ValueError` when `determination` was absent.

Two consequences. The finalizer was the only node that could not run standalone against a run
directory, and `determination` could not be safely re-run in place, because its outputs existed
only in the memory of a graph invocation that had already ended. Both block a stage runner
(ADR 0036), whose whole premise is that a node is a function of a run directory.

## Decision Drivers

- A stage runner needs every node to be a function of the run directory. One node that is not
  breaks the uniformity the whole design rests on.
- `determination` is the pipeline's go/no-go gate. A wrong or stale record here is the worst
  failure mode in the system — worse than no record.
- The change must be behavior-identical for graph runs. It enables granular execution; it does not
  alter how a full pipeline behaves.

## Considered Options

- A separate verdict artifact written by `determination_node`
- Have `determination_node` write `determination.json` directly
- Have the finalizer recompute the verdict from the validation artifact
- Leave the seam and exclude the finalizer from the stage runner

## Decision Outcome

Chosen option: **a separate verdict artifact**. `determination_node` writes
`determination_verdict.json` — a frozen `DeterminationVerdict` carrying `slug`, `determination`,
`reason`, `failed_steps`, `sub_agent_spawned` — on **all three** return paths, including the
`DeterminationParseError` path that yields `ORCHESTRATION_ERROR`. A re-run must leave a truthful
artifact regardless of outcome.

`make_finalizer_node` resolves the verdict from disk, falling back to `PipelineState` when the
artifact is absent, and raising the typed `DeterminationVerdictError` when neither is available.
The fallback is not scaffolding: it is what keeps a graph run concluding if the write ever fails,
and it is what makes this change behavior-identical for the graph path.

**The verdict is kept distinct from `DeterminationReport`.** The verdict is determination's output;
the report is the finalizer's, and it additionally resolves `sub_agent_outcome` from the execution
journal and stamps a timestamp. Collapsing them would mean `determination_node` writing
`determination.json`, which ADR 0035 depends on being absent to mean "the run was never concluded."
The verdict is JSON only — no `.md` sibling — because it is a machine handoff, not an operator
report; the human-readable rendering remains the finalizer's `determination.md`.

### Disk wins, and staleness fails closed

Where both sources exist, disk wins: it is what a re-run of determination updates, and preferring
state would make a re-run invisible to the finalizer.

That preference is only safe if a *foreign* artifact can be detected, which is why
`DeterminationVerdict` carries `slug`. `resolve_verdict` compares it against the run's slug and
raises on mismatch — it does **not** fall back to state, even when state holds a perfectly usable
verdict. Silently falling back would look like correct behavior while concealing that the run
directory belongs to a different run.

This is reachable rather than theoretical: `run_pipeline` accepts an explicit `run_dir`, and the
stage runner operates against existing run directories by design. Without the slug the invariant
"disk and state agree" would live only in a docstring; with it, the gate fails closed and names
both runs.

### A newly distinguishable state

Because the verdict is written *before* the sub-agent runs,
`determination_verdict.json` present together with `determination.json` absent now means
**"the gate decided, the run never concluded."** That is exactly what a plan-only run (ADR 0035)
leaves behind. This strengthens rather than weakens ADR 0035's signal — absence of the report still
means the run was never concluded — but a reader should not mistake the presence of a verdict for a
concluded run.

### Consequences

- Good, because every node is now a function of the run directory, which is what makes the stage
  runner possible and `determination` safely re-runnable in place.
- Good, because a stale or foreign verdict fails closed and loudly on the capital gate, rather than
  being silently recorded into `determination.json`.
- Good, because the artifacts now distinguish "paused before execution" from "concluded".
- Bad, because `determination_node` gained `require_slug` as a precondition; it previously needed
  only `working_dir`. Only observable to callers driving the node with a hand-built state.
- Bad, because `resolve_verdict` raises the same `DeterminationVerdictError` for three
  distinguishable conditions — malformed artifact, nothing available, and foreign slug. Only the
  last has a usable verdict being deliberately refused, and a caller can currently tell it apart
  only by message text. A `DeterminationVerdictSlugMismatchError` subclass would make that a
  type-level contract; deferred as not blocking, since the current shape fails closed correctly.
- Neutral, because `_verdict_from_state` still defaults `reason` to `""`. That is pre-existing
  behavior, and it was confirmed unreachable from the graph path: `determination` is written into
  state in exactly three places, all inside `determination_node`, and each writes the reason too.

### Confirmation

`tests/unit_tests/pipeline/test_determination.py` pins the verdict on all three write paths
(including parse failure), `load_verdict`'s absent/malformed/valid split, and `resolve_verdict`'s
precedence — the disk-wins case uses a persisted verdict differing from the state one on *every*
field, so the test can tell which source was used. The staleness guard is pinned by a test where
state carries a fully usable verdict and resolution still raises, plus a consequence pin that
`determination.json` is absent afterwards. Discrimination was verified by flipping the foreign slug
to a matching one and confirming all six guard tests fail.

`tests/integration_tests/pipeline/test_determination_seam.py` runs `determination_node` and then
`make_finalizer_node` with a state containing only `slug` and `working_dir` — nothing but the
directory passes between them — for both the UNMATCHED and parse-failure paths, and pins that
pointing the finalizer at another run's directory raises and writes no report.

Behavior-identity for graph runs is confirmed by the pre-existing suite passing unchanged.

## Pros and Cons of the Options

### A separate verdict artifact

- Good, because it closes the seam without touching the meaning of any existing artifact.
- Good, because the verdict/report split mirrors the real division of labour: the gate decides, the
  finalizer records the outcome once the sub-agent has run.
- Bad, because it adds a file to the run directory whose distinction from `determination.json` a
  reader has to learn.

### `determination_node` writes `determination.json`

- Good, because no new artifact.
- Bad, because ADR 0035 relies on that file's absence meaning "never concluded" — a plan-only run
  would start producing one. Decisive against.
- Bad, because the report legitimately carries fields determination cannot know: the sub-agent
  outcome does not exist until the sub-agent has run.

### The finalizer recomputes from validation

- Good, because it adds no artifact and no state dependency, and mirrors how `determination_node`
  itself recomputes.
- Bad, because it duplicates the go/no-go logic in a second place. Two implementations of the
  capital gate that could disagree is a worse failure mode than the one being fixed.

### Leave the seam, exclude the finalizer

- Good, because no change at all.
- Bad, because "every stage is runnable" acquires a permanent asterisk, and `determination` stays
  unsafe to re-run in place — the inconsistency stays exactly where it does the most harm.

## More Information

The stage runner this enables is described in
[ADR 0036](0036-per-dependency-builders-as-the-composition-root.md). The plan-only pause and the
meaning of an absent `determination.json` are
[ADR 0035](0035-plan-only-runs-pause-before-execution.md). Execution journal semantics, which the
finalizer reads to resolve the sub-agent outcome, are
[ADR 0003](0003-execution-journal-semantics-paper-stub.md).
