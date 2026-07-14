# On-screen text extracted by a multimodal VLM, not native OCR

- Status: accepted
- Date: 2026-07-14
- Deciders: owner, python-dev

## Context and Problem Statement

Phase 9 must read the **on-screen text** of a market-commentary video — chart titles, tickers, figures,
and especially chart-footer **source attribution** (e.g. "Source: Bloomberg") that populates a claim's
`cited_sources`. The architecture spec named **Tesseract / PaddleOCR / EasyOCR** for this
(`architecture.md` §8a/§13, `pipeline_contracts.md` §11 "on-screen text / source extraction"). We had to
choose an extraction mechanism.

## Decision Drivers

- The valuable output is not raw glyphs but **interpreted** on-screen content — which strings are data
  attributions vs. labels vs. figures. Native OCR gives raw text; the attribution split still needs a
  second interpretation step.
- The system already runs a multimodal-capable LLM (Claude via pydantic-ai/anthropic); a VLM can read the
  frame **and** do the attribution split in one call.
- Native OCR adds heavy, platform-specific dependencies: Tesseract needs a **system binary**; PaddleOCR/
  EasyOCR pull large ML/torch stacks. The runtime is a personal machine.
- Keep the dependency footprint minimal and reuse the existing stack.

## Decision Outcome

**Extract on-screen text with the Claude multimodal VLM via pydantic-ai, not native OCR.**
`ingestion/on_screen.py` builds a pydantic-ai `Agent` (mirroring `make_video_llm_agent`) whose output is a
small `OnScreenDraft{on_screen_text, cited_sources}`; each selected keyframe PNG is sent as a
`pydantic_ai.BinaryContent(data=png_bytes, media_type="image/png")` alongside a short instruction, and a
pure `_to_extraction` stamps the keyframe's `locator` onto the result → `OnScreenExtraction`. The prompt
(`prompts/agent_video_onscreen.md`) instructs the model to transcribe only legibly-visible text, to put
data-source attributions in `cited_sources`, and to never fabricate a source.

- **No native OCR libraries** (no Tesseract/PaddleOCR/EasyOCR, no system binary).
- **No direct `anthropic` dependency** — images reach the model through `pydantic_ai.BinaryContent`;
  `anthropic` stays transitive via pydantic-ai.
- Keyframe count is capped (`keyframe_max_frames`, ADR 0022/Wave 2) since each keyframe is one VLM call —
  the cost knob.

### Consequences

Good: one step yields both the on-screen text and the interpreted attribution split; no system-binary or
heavy-OCR dependency; reuses the existing LLM stack and the established injected-callable seam (tests
inject a fake `OnScreenExtractor`). The `_to_extraction` locator-stamp is a pure, tested unit.

Bad / to watch: on-screen extraction now costs one LLM call per keyframe (bounded by `keyframe_max_frames`)
and inherits LLM latency/nondeterminism, versus deterministic local OCR. A VLM can mis-read or omit text;
the "never fabricate a source" prompt rule and the downstream classifier's evidence-only posture are the
mitigations. This is a deliberate deviation from the spec's named OCR libraries, recorded here.

## Considered Options (key rejections)

- **Native OCR (Tesseract/PaddleOCR/EasyOCR) — the literal spec.** Rejected: a system binary or a heavy
  ML/torch stack, and it still needs a second step to interpret which text is a source attribution.
- **Hybrid OCR + VLM.** Rejected: the most dependencies and moving parts for little gain when the VLM
  already reads the frame directly.
- **A direct `anthropic` multimodal client.** Rejected: `pydantic_ai.BinaryContent` reaches the same model
  through the stack already in use, keeping `anthropic` transitive and the agent seam uniform.

## More Information

Deviation from `docs/architecture.md` §8a/§13 and `pipeline_contracts.md` §11 (named OCR libraries).
Related: `src/money_pit/ingestion/on_screen.py`, `src/money_pit/prompts/agent_video_onscreen.md`,
`src/money_pit/ingestion/keyframes.py`, `src/money_pit/adapters/video_llm.py` (the mirrored agent pattern).
Cascade/transcription is ADR 0019; caching is ADR 0021; packaging is ADR 0022.
