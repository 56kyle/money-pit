# Video ingestion is an optional dependency extra, owned by orchestration

- Status: accepted
- Date: 2026-07-14
- Deciders: owner, python-dev

## Context and Problem Statement

Phase 9 adds real video multimodal ingestion (yt-dlp fetch, caption cascade, faster-whisper, PySceneDetect
keyframes, a multimodal VLM). This introduces heavy, platform-specific libraries (a Whisper backend,
OpenCV, a media downloader) that the rest of the system — the deterministic spine, execution, recovery —
does not need. Two packaging/boundary questions had to be settled before writing any producer code:
where the ingestion libraries live in the dependency graph, and where the ingestion code lives in the
architecture.

## Decision Drivers

- The downstream pipeline (analysis → execution) must stay installable and testable without pulling heavy
  media/ML wheels; the default test suite must never import `yt_dlp` / `faster_whisper` / `scenedetect` /
  `cv2`.
- `docs/pipeline_contracts.md` §1 already fixes the boundary: "the adapter itself must NOT do its own
  fetch" — the adapter consumes an assembled `VideoPayload`; the orchestration layer produces it.
- `contracts.py` is a documented cycle-free leaf importing only `money_pit.schemas`.

## Decision Outcome

1. **Ingestion libraries are a `video` optional-dependency extra** (`pyproject.toml`
   `[project.optional-dependencies].video` = yt-dlp, faster-whisper, scenedetect, opencv-python, pillow),
   mirroring the existing `cpu`/`cu128` torch extras. Default installs and the default test suite stay
   lean; the heavy libs are opted into only for ingestion. Each real seam imports its heavy library
   **lazily inside the function** (mirroring `orchestration.py`'s `import yfinance` inside
   `fetch_ticker_price`), so importing the ingestion modules never imports the heavy libraries until a
   real seam actually runs.
2. **Ingestion is orchestration-owned**, in a new `src/money_pit/ingestion/` subpackage invoked by the CLI
   *before* `VideoAdapter.process` — reaffirming §1. The adapter is unchanged and consumes the assembled
   `VideoPayload`. Ingestion sits upstream of the graph (which begins at the recovery node); it only
   produces the `signals/{source_id}.json` disk state the aggregator later reads.
3. **A `VideoAgent` type alias** is added to `contracts.py` for symmetry with the other agent aliases,
   with `VideoPayload` imported only under `TYPE_CHECKING` and stringized in the alias — preserving the
   cycle-free-leaf property (no runtime import of `adapters/`).
4. A `live_video` pytest marker is registered and `addopts` set to `-m 'not live and not live_video'`, so
   the real ingestion tier (network + GPU + LLM) is independently selectable and deselected by default.

### Consequences

Good: the capital/analysis path is unencumbered by media/ML deps; the default suite stays fast and
hermetic; the fetch-vs-consume boundary of §1 is honored; the leaf property of `contracts.py` holds.

Bad / to watch: a `video` extra + lazy imports means a misconfigured environment (extra not installed)
fails at ingestion runtime rather than import time — acceptable, and the CLI/live tier surface it clearly.
The specific library choices (faster-whisper over WhisperX; a VLM over native OCR) are their own decisions,
recorded in ADRs 0019 and 0020; the per-stage caching that makes re-runs cheap is ADR 0021.

## Considered Options (key rejections)

- **Core (non-optional) dependencies.** Rejected: forces every install of the trading spine to pull
  OpenCV / a Whisper backend / a media downloader it never uses.
- **Ingestion inside the adapter or as a graph node.** Rejected: §1 fixes the adapter as a pure consumer;
  ingestion is a pre-graph orchestration concern that produces the signal files the graph consumes.
- **Runtime `VideoPayload` import in `contracts.py`.** Rejected: breaks the cycle-free-leaf property; the
  `TYPE_CHECKING`-stringized alias keeps it intact.

## More Information

Frames Phase 9. Related: `pyproject.toml`, `src/money_pit/contracts.py`, `src/money_pit/adapters/video.py`,
`docs/pipeline_contracts.md` §1, `docs/decisions/0002-source-adapter-pattern.md`; the library and caching
decisions are ADRs 0019 (cascade + faster-whisper), 0020 (VLM on-screen extraction), 0021 (raw-artifact
caching).
