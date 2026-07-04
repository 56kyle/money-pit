# Static Alpaca tool manifest for A5 capability validation (pre-Phase-7)

- Status: accepted
- Date: 2026-07-02
- Deciders: owner, architecture author

## Context and Problem Statement

A5 (`pipeline/validator.py`) is the capability gate: for each action step it must confirm the
tool the router selected **exists** in the MCP manifest and that the emitted parameters are
**accepted** by that tool's `inputSchema` (`docs/architecture.md` §6.6, `docs/pipeline_contracts.md`
§6; the "manifest ↔ A5 agreement" Gate 3 in `docs/reviews/phase-1-6-findings.md`). Two defects
(T4): the tool-existence check was stubbed (`# … assume all tools present`) and A5 also took an
injected **LLM** `behavioral_match` predicate defaulting to always-`True` — so A5 was silently
green on checks it wasn't performing, contradicting the pinned "A5 contains no LLM"
(§9/§11.2).

Live introspection of registered MCP servers — the documented Phase-7 source of the manifest —
does not exist yet (`mcp/manifest.py` is an empty stub). We need an honest existence check now
without pulling live introspection forward. Governing principle (ADR 0003): **a safety gate
that cannot run its check must not answer "safe" — fail closed or announce "not yet real,"
never default to the passing answer.**

## Decision Drivers

- Fail-closed honesty; do not report a capability verified against a check that didn't run.
- Consistency with ADR 0003 (which, one day earlier, **rejected** the posture that blocks the
  paper pipeline from completing) and with the P1 pinned-schema single-source pattern.
- Capability vs. dependency: build the conformance half now; defer live introspection to Phase 7.
- Keep A5 fully deterministic (no LLM) and the paper pipeline runnable + green.

## Decision Outcome

**1. A5 validates against a static manifest of the closed Alpaca write-tool set.** The manifest
is `{"place_order": <the pinned alpaca_order_schema>}` — the only tool `ACTION_TYPE_TO_TOOL`
targets — assembled from `mcp.order_schema.load_order_schema()`. Existence check: the router's
tool name is a key in the manifest. Schema-acceptance check: `jsonschema.validate(params, manifest[tool])`. This is **contract-level** existence (does the selected tool appear in the
pinned write-tool set), **not** runtime availability (a live server is up and exposes it) — the
latter is the honest Phase-7 deferral.

**2. The manifest is injected at node construction** (`make_validator_node(..., manifest=…)`),
defaulting to the static provider, mirroring P1's `schema_path` seam. Phase 7 swaps only the
default for live introspection — the marked terminus. The static provider is **named so the
static→live swap is obvious** (e.g. `pinned_manifest()`), and must not hide behind a docstring
claiming it "introspects registered servers at runtime" — that would reclaim the exact lie T4
fixes.

**3. An unavailable manifest fails closed via a typed `ManifestUnavailableError`,** so the
Phase-7 live path (where the manifest genuinely can be absent) already has a fail-closed type
pinned by a test rather than retrofitted when introspection lands.

**4. The behavioral-match check becomes the deterministic `ACTION_TYPE_TO_TOOL[action_type]`
lookup; the injected LLM predicate is removed.** This is a straight restore-to-spec (no ADR of
its own; logged as a revert in `docs/reviews/phase-1-6-findings.md`), recorded here only for
completeness.

### Consequences

Good: an honest contract-existence check runs today; A5 is fully deterministic; the paper
pipeline stays runnable and green; the Phase-7 static→live swap touches one default; a
fail-closed manifest type is pre-pinned.

Bad / to watch: `ACTION_TYPE_TO_TOOL` is **total** over `ActionType`, so the step-level tool
lookup cannot fail for a validly-parsed step — the reachable capability-failure paths are a
manifest that **omits** the router's tool (router↔manifest disagreement) or an **unavailable**
manifest. Tests must target those, and map totality is pinned by an invariant test; a
manufactured check-that-can't-fail would be theater, not a fail-closed proof. Standing up
`mcp/manifest.py`'s **conformance half** (static set) slightly pre-touches a Phase-7 file —
accepted, mirroring ADR 0003's journal split (conformance now, introspection deferred).

### Confirmation

Tests: a manifest omitting `place_order` → step `UNMATCHED` with an honest gap; an unavailable
manifest → `ManifestUnavailableError` (fail-closed); the invariant `set(ACTION_TYPE_TO_TOOL) == set(ActionType)`; and the emitted params still validate against `manifest["place_order"]`
(preserving the P1 single-source). The static→live terminus is signalled when introspection
replaces the static default.

## More Information

Mirrors ADR 0003's capability-vs-dependency split. Related: `docs/design_decisions.md` §7/§8
(pinned order schema as a shared artifact), `docs/architecture.md` §8, `docs/pipeline_contracts.md`
§6, and P1 (`src/money_pit/mcp/order_schema.py`).
