# Unpinned Alpaca order schema fails closed via an in-band stub sentinel

- Status: accepted
- Date: 2026-07-05
- Deciders: owner, architecture author

## Context and Problem Statement

ADR 0004 pinned A5's manifest and the order schema to a single committed artifact read through
`mcp.order_schema.load_order_schema`, with a fail-closed posture: a safety gate that cannot run its
check must never answer "safe." The original Phase-2 intent (`docs/implementation_plan.md`) was that
`compute/execution_params.py` stays **CI-red until the real OpenAPI-generated schema is pinned** from
the live Alpaca MCP server — enforced by `load_order_schema` raising when the file was **absent**.

That intent silently rotted. Once a generic, real-_shaped_ stub `mcp/alpaca_order_schema.json` was
committed so the deterministic spine could run and be tested, the file was no longer absent, so
`load_order_schema` returned it happily. The gate went green while the schema was, in truth, still
unpinned — the exact "default to the passing answer" failure ADR 0003/0004 forbid. Both the
post-processor (`build_execution_params`) and A5's `pinned_manifest()` validate real order payloads
against a schema that does not describe the real Alpaca order tool.

We need the gate to fail closed again **while the stub is in place**, without bricking the green
paper-trade spine (which must keep exercising the execute path end-to-end).

Governing principle (ADR 0003/0004): **a stub must fail closed or announce "not yet real" — never
default to the passing answer.**

## Decision Drivers

- Restore the "CI-red until pinned" signal: the default production path must refuse to run against an
  unpinned stub.
- Keep the paper-trade spine and unit tests green via an explicit, honest opt-in — not by weakening
  the gate.
- Avoid a behavior-changing boolean at the shared choke point (the ethos flags `allow_stub`-style
  flags as a stepping-stone smell).
- Single source of truth: the post-processor emission and A5's manifest must keep sharing one loader.
- The pinning event should announce itself — flipping the gate is a visible, forcing signal, not a
  silent state change.

## Decision Outcome

**1. The committed stub carries an in-band sentinel; `load_order_schema` fails closed on it.** The
stub schema carries a top-level `"x_stub": true` — an `x-`-style vendor-extension key that JSON Schema
validators ignore as unknown, so it never affects a real `jsonschema.validate`. `load_order_schema`
gains a third precedence rung, after the existing two: absent → `AlpacaOrderSchemaMissingError`;
unparseable / non-object → `AlpacaOrderSchemaMalformedError`; **sentinel present and truthy →
`AlpacaOrderSchemaNotPinnedError`**; otherwise return the dict. The sentinel key is a named constant
(`ALPACA_ORDER_SCHEMA_STUB_SENTINEL`), never a bare literal. The guard keys on **truthiness**, so a
real schema that ever collided on the key still errs closed rather than open.

**2. The sole opt-in is injecting a sentinel-free `schema_path` — no `allow_stub` boolean.** Both
public callers already accept a `schema_path` seam (`build_execution_params(..., schema_path=…)`,
`pinned_manifest(schema_path=…)`). The one seam that was missing — the analysis node's deep call to
`build_execution_params` — is added as `PipelineOverrides.order_schema_path`, threaded through
`build_graph` → `make_analysis_node`, mirroring the existing `manifest` seam. A boolean that flips the
choke point's behavior was rejected: it would sit at the widest blast radius and change what a function
_means_, where a path injection simply points the existing loader at a real-shaped artifact.

**3. Production and `phase4_overrides()` stay fully fail-closed; only per-test opt-in proceeds.**
Nothing in the package opts into the stub. The paper-trade spine tests inject a sentinel-free schema
(derived from the committed stub, so it stays in lockstep with the real shape) on both seams. A real
`run_pipeline` with default overrides fails closed — including a no-trade `NO_ACTION` run, because
`make_validator_node` resolves `pinned_manifest()` eagerly at graph construction (see "to watch").

**4. A strict-xfail tripwire marks the pinning event.** A checkpoint test asserts the default committed
path loads successfully, marked `xfail(raises=AlpacaOrderSchemaNotPinnedError, strict=True)`. While
stubbed it reports XFAIL (suite green); when the real schema is pinned (sentinel removed) it XPASSes,
which strict-xfail turns into a hard failure — forcing removal of the tripwire and the per-test stub
opt-ins. This restores the "flips at pin time" signal without leaving a permanently-red test.

### Consequences

Good: the gate is honest again — the default path cannot validate capital-moving payloads against an
unpinned schema; the spine stays green via an explicit, mock-free opt-in; the loader stays the single
source; the pinning event announces itself. The Phase-7 upgrade is a three-line delete (remove the
sentinel from the real schema, drop the tripwire, drop the per-test opt-ins).

Bad / to watch: the eager `pinned_manifest()` resolution in `make_validator_node` means **every** run,
including `NO_ACTION`, fails closed at graph-build without a pinned schema or an injected manifest —
broader than the analysis-stage gate and surfaced as a raw `ManifestUnavailableError` at construction
rather than routed to `ORCHESTRATION_ERROR` + notification. This is pre-existing eager-resolution
behavior that the sentinel merely tripped; whether graph-build capability failures should route to a
terminal state instead of raising is left as a separate design question.

### Confirmation

Failure-mode tests pin the gate: `load_order_schema()` on the default committed path →
`AlpacaOrderSchemaNotPinnedError` (unit); a fully-default `run_pipeline` → `ManifestUnavailableError`
at graph-build and `AlpacaOrderSchemaNotPinnedError` at the analysis stage (integration, both loud
raises, nothing swallowed); a sentinel-free injected schema → the spine completes the execute path;
the strict-xfail checkpoint flips the suite red the moment the real schema is pinned.

## Considered Options (key rejections)

- **Leave the file absent → `Missing`.** Rejected: the deterministic spine needs a real-shaped schema
  to run and be tested end-to-end; absence blocks the spine entirely, which is why a stub was committed
  in the first place.
- **`allow_stub: bool` on the shared loader.** Rejected: a behavior-changing flag at the widest choke
  point; path injection is the narrower, more honest opt-in and reuses seams that already exist.
- **A permanently-red "not pinned" test as the forcing function.** Rejected: a CI job that is red by
  design trains reviewers to ignore red; the strict-xfail tripwire gives the same pin-time signal while
  keeping the suite green until the event.

## More Information

Extends ADR 0004 (static pinned manifest) and shares ADR 0003's capability-vs-dependency,
fail-closed-stub bar. Related: `docs/design_decisions.md` §7, `docs/architecture.md` §15 #1,
`docs/implementation_plan.md` (Phase 2 / Watch Items), `src/money_pit/mcp/order_schema.py`,
`src/money_pit/mcp/manifest.py`, `src/money_pit/compute/execution_params.py`.
