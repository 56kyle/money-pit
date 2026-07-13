# `pinned_manifest` propagates the loader's typed error instead of wrapping it in `ManifestUnavailableError`

- Status: accepted
- Date: 2026-07-13
- Deciders: owner, python-dev

## Context and Problem Statement

ADR 0004 established `ManifestUnavailableError` as A5's fail-closed signal for an unavailable tool
manifest, and both manifest builders raised it. `pinned_manifest()` did so by wrapping the shared
loader's already-typed `AlpacaOrderSchemaError` (missing / malformed / not-pinned) into
`ManifestUnavailableError`:

```python
try:
    schema = load_order_schema()
except AlpacaOrderSchemaError as err:
    raise ManifestUnavailableError(...) from err
```

While retiring the pre-pin stub scaffolding (the `schema_path` test seams — see the ADR-0007
terminus), that `except` branch became reachable only by re-introducing a test-only injection seam or
by monkeypatching the loader. Rather than pick between a mock and a resurrected seam, we questioned
whether the wrap earns its keep at all.

## Decision Drivers

- `live_manifest` genuinely needs its wrap: it funnels **arbitrary untyped** `Exception`s from live
  MCP introspection into one typed fail-closed error. `pinned_manifest`'s underlying failure is
  **already** a precise, typed `AlpacaOrderSchemaError` that already fails closed — wrapping
  typed→typed adds a layer without adding safety.
- The sibling capital-path consumer of the same loader, `build_execution_params`, already lets the
  raw `AlpacaOrderSchemaError` propagate, and that is accepted. The two consumers were inconsistent.
- `ManifestUnavailableError` is **never caught** anywhere in the system. At graph-build the default
  `pinned_manifest()` resolves eagerly, and any failure crashes loudly regardless of the error's type
  (ADR 0007's documented eager-resolution behavior).
- A capital-path branch should be pinned at a **real boundary**, not behind a mock — and the loader's
  own failure modes are already pinned that way in `test_order_schema.py`.

## Considered Options

- **Keep the wrap; restore a `schema_path` (or `load=`) seam to test it at a real boundary.**
- **Keep the wrap; cover the branch by monkeypatching `load_order_schema`.**
- **Remove the wrap; let `AlpacaOrderSchemaError` propagate.**

## Decision Outcome

Chosen: **remove the wrap.** `pinned_manifest()` now calls `load_order_schema()` and returns the
manifest, letting the loader's typed `AlpacaOrderSchemaError` propagate unflattened — consistent with
`build_execution_params`. `live_manifest` is untouched and keeps its `ManifestUnavailableError` wrap,
which it needs.

### Consequences

Good: one fewer hollow layer; the two `load_order_schema` consumers now propagate failure
consistently; callers receive the **more specific** typed error (which of missing / malformed /
not-pinned occurred) rather than a flattened one; and the branch that could only be pinned by a mock
or a test-only seam is gone, so the fail-closed contract stays tested at the real loader boundary
(`test_order_schema.py`) with no mock. `pinned_manifest` now has no branches and is fully covered by
its happy path.

Bad / to watch: the documented graph-build failure type for a broken/unpinned pinned schema changes
from `ManifestUnavailableError` to `AlpacaOrderSchemaError`. `ManifestUnavailableError` now signals
**only** the live-introspection path. Because nothing catches the type today, this is a
documentation / mental-model change, not a behavioral change for any handler — but if a future caller
wants a single "manifest could not be built" type across both builders, it would need to catch both
(or re-introduce a deliberate, non-test wrap).

### Confirmation

`pinned_manifest` has no `try`/`except` branch after the change and is at 100% coverage via its happy
path. The loader precedence (missing → `AlpacaOrderSchemaMissingError`, malformed →
`AlpacaOrderSchemaMalformedError`, truthy sentinel → `AlpacaOrderSchemaNotPinnedError`) remains pinned
in `tests/unit_tests/mcp/test_order_schema.py`; `live_manifest`'s `ManifestUnavailableError`
fail-closed contract remains pinned (via the injectable `list_tools` seam) in
`tests/unit_tests/mcp/test_manifest.py`.

## Considered Options (key rejections)

- **Restore a test-only seam to keep the wrap testable.** Rejected: it re-adds injection surface for
  a wrap that adds no safety over the typed error it wraps — scaffolding in service of a hollow layer.
- **Monkeypatch `load_order_schema` to cover the branch.** Rejected: it pins a capital-path branch
  behind a mock, the design smell this project avoids; the honest fix is to remove the branch, not to
  mock it.

## More Information

Emerged during the ADR-0007-terminus seam retirement (removing the post-pin `schema_path` test
seams). Supersedes ADR 0004's point that an unavailable manifest fails closed specifically via
`ManifestUnavailableError` — now true only for `live_manifest`. Related:
`src/money_pit/mcp/manifest.py`, `src/money_pit/mcp/order_schema.py`,
`src/money_pit/compute/execution_params.py`, `src/money_pit/pipeline/validator.py`.
