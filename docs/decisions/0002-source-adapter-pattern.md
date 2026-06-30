---
status: accepted
date: 2026-06-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Source Adapter Uses Generic ABC, Payload-Carried Slug, and Pre-LLM Payload Persistence

## Context and Problem Statement

Phase 6 introduces the source adapter layer: a thin translation tier that sits between raw source data (video, text, PDF) and the LLM agents that produce `SignalSet` output. Three design questions arose together during implementation.

**Question 1 — adapter interface typing**: `base.py` needs a `SourceAdapter` base class with a `process()` method. Each concrete adapter accepts a fundamentally different input structure: `VideoPayload` carries a transcript and keyframes, a text payload carries a raw string, a PDF payload carries pages. Should the base class enforce this distinction at the type level, or use `Any`?

**Question 2 — slug placement**: `process()` needs access to the run slug to construct the output path. Should the slug travel on the payload (`VideoPayload.slug`), or be passed to the adapter at construction time (`VideoAdapter.__init__(slug=...)`)?

**Question 3 — intermediate persistence**: `VideoAdapter.process()` assembles a `VideoPayload` from downloaded video and transcription before calling the LLM agent. Should that payload be written to disk before the LLM call, or should the two steps be a single in-memory pipeline?

## Decision Drivers

* Each source type has a structurally distinct payload; collapsing them to `Any` discards that contract.
* Adapters should be stateless (hold configuration only) so that a single instance can be reused across runs.
* Video fetching and transcription are expensive; LLM classification is cheap to retry. The two costs should be independently recoverable.
* The test seam for `VideoAdapter` should be a concrete, inspectable intermediate — not a mock of a download or transcription call.
* `SourceAdapter.process()` must take a single payload argument to conform to the adapter pattern's interface.

## Considered Options

* **Adapter interface**: `SourceAdapter[T]` generic ABC vs. `process(payload: Any)` with no type parameter
* **Slug placement**: `VideoPayload.slug` (on payload) vs. `VideoAdapter.__init__(slug=...)` (on constructor)
* **Intermediate persistence**: write `video_payload.json` before LLM call vs. keep both steps in memory

## Decision Outcome

All three chosen options form a coherent whole:

1. **`SourceAdapter[T]` generic ABC** — `base.py` uses `Generic[Payload]` so that each concrete adapter's `process()` method has a typed signature (e.g., `VideoAdapter.process(payload: VideoPayload) -> SignalSet`). basedpyright enforces the contract at the call site.

2. **Slug on `VideoPayload`** — `VideoPayload.slug: str` carries the run slug through `process(payload)`. The adapter holds only configuration; per-run identity travels with the payload.

3. **`video_payload.json` persisted before LLM call** — `VideoAdapter.process()` writes the fully assembled `VideoPayload` to `cache_dir` before invoking the LLM agent. A subsequent LLM re-run reads the persisted file directly, skipping download and transcription.

### Consequences

* Good, because `SourceAdapter[VideoPayload]` is visible to the type checker and to readers — the interface is the contract.
* Good, because stateless adapters have a clear, stable lifetime: construct once, call `process()` many times with different payloads.
* Good, because LLM retries (e.g., after a bad classification) are cheap: re-read the cached `video_payload.json` and re-call the agent. No re-download.
* Good, because the test seam is a hand-authored `VideoPayload` JSON fixture, not a mock of network or GPU calls.
* Bad, because the `Generic[Payload]` pattern requires callers to instantiate the correct concrete type; there is no runtime enforcement of the type parameter.
* Bad, because `video_payload.json` written to `cache_dir` creates an implicit dependency on the filesystem that pure in-memory tests must account for.

### Confirmation

The test fixture pattern (`VideoPayload` loaded from a hand-authored JSON file) acts as the confirmation: if `VideoPayload` serialization drifts, fixture loading fails immediately, surfacing the breakage before any LLM call is made.

## Pros and Cons of the Options

### `SourceAdapter[T]` generic ABC

* Good, because `VideoAdapter.process(payload: VideoPayload)` is statically verifiable — a caller passing a `TextPayload` is a type error, not a silent runtime failure.
* Good, because the generic parameter makes each adapter's contract self-documenting without a comment.
* Bad, because `Generic[Payload]` adds a layer of abstraction that may be unfamiliar to readers who expect a simpler ABC.

### `process(payload: Any)` with no type parameter

* Good, because it is simple — one base class, no type parameter, no import of `Generic`.
* Bad, because the distinction between `VideoPayload`, `TextPayload`, and `PDFPayload` is lost at the interface boundary; the type checker cannot catch a mismatched payload.
* Bad, because the docstring or a comment would be the only contract documentation, violating the project naming and commenting ethos.

### Slug on `VideoPayload`

* Good, because the payload is self-contained: everything the LLM agent needs (transcript, keyframes, slug) arrives in one argument.
* Good, because the adapter is stateless and reusable across runs without re-instantiation.
* Neutral, because it requires callers to set `slug` on the payload before calling `process()`, which is an extra step but an explicit one.

### Slug on `VideoAdapter.__init__`

* Good, because the constructor signature makes clear that the adapter is bound to a specific run.
* Bad, because adapter lifetime is now coupled to run lifetime — a new adapter instance per run, which contradicts the configuration-only intent.
* Bad, because a single adapter instance can no longer be reused across runs without mutation or re-construction.

### Persist `video_payload.json` before LLM call

* Good, because fetch cost (network, GPU) and classification cost (LLM tokens) are independently retryable.
* Good, because the persisted file is the concrete test seam: hand-author it in tests, point `process()` at a fixture directory, verify LLM behavior without any I/O.
* Bad, because it introduces a write to `cache_dir` as a side effect of `process()`, which must be accounted for in isolation tests.

### Keep both steps in memory

* Good, because `process()` is a pure function with no filesystem side effects.
* Bad, because a failed LLM call after a successful (expensive) transcription requires a full re-fetch and re-transcription on retry.
* Bad, because the test seam becomes the download and transcription calls, which require heavier mocking to bypass.

## More Information

When additional source types are added (text, PDF), each follows the same three decisions: a typed `SourceAdapter[TextPayload]` or `SourceAdapter[PDFPayload]` subclass, slug carried on the payload, and a source-specific intermediate (e.g., `text_payload.json`) persisted before the LLM call. No new ADR is required for conforming additions; a superseding ADR is warranted only if one of the three decisions is reversed.
