# Per-stage ingestion caching — a recoverability ladder extending ADR 0002

- Status: accepted
- Date: 2026-07-14
- Deciders: owner, python-dev

## Context and Problem Statement

ADR 0002 established a single recoverability seam: `VideoAdapter.process` persists the assembled
`VideoPayload` (`video_payload.json`) *before* the LLM classification call, so a re-run after a bad
classification re-reads the payload and skips download + transcription. Phase 9 adds **four expensive
stages upstream** of classification — yt-dlp download, faster-whisper transcription, PySceneDetect
keyframing, and per-keyframe VLM extraction (multi-GB downloads, GPU time, and paid LLM calls). A single
fetch/classify seam is no longer enough: a failure or a parameter change at any stage should not force
redoing the ones before it.

## Decision Drivers

- Each expensive stage should be **independently recoverable** — a re-run resumes from the last good
  artifact, not from the download.
- Schema drift in a cached artifact must fail loudly, not silently feed stale/malformed data downstream.
- The cache key must be known **before** the download so the download itself is skippable.

## Decision Outcome

`ingest_video` (`ingestion/pipeline.py`) writes a **typed JSON artifact per expensive stage** under
`cache_dir/<source_id>/` and cache-skips on artifact presence:

| stage | artifact | model |
|---|---|---|
| fetch (yt-dlp) | `artifacts.json` | `VideoArtifacts` |
| transcript (cascade or whisper) | `transcript.json` | `TranscriptResult` |
| keyframes | `keyframes/index.json` (+ the PNGs) | `list[Keyframe]` |
| on-screen (VLM) | `on_screen.json` | `list[OnScreenExtraction]` |

Each read goes through `model_validate_json` / `TypeAdapter.validate_json`, so a schema drift in a cached
artifact raises rather than silently loading stale data (the same confirmation mechanism ADR 0002
describes). The **cache key is `source_id`** (`yt:<id>`), derived from the URL *before* the fetch
(`_source_id_from_url`, matching `fetch._build_source_ref`), so even the download is skippable on a
re-run. This extends ADR 0002's single seam into a full ladder: download → transcribe → keyframe → VLM →
(adapter: classify → `video_payload.json`). `ingest_video` does not write `video_payload.json` — that
stays the adapter's ADR-0002 responsibility, so the ladder's top rung is unchanged.

The cache root is the configurable `ingest_cache_dir` (default under the platformdirs user-cache dir), so
multi-GB media lives outside the repo `data/` tree.

### Consequences

Good: a re-run after a bad classification, a bad fusion, or changed keyframe parameters skips the
multi-GB download, the GPU transcription, and the paid VLM calls as appropriate — resuming from the
highest still-valid rung. Every artifact re-validates on read.

Bad / to watch: the ladder assumes on-disk artifacts stay **coherent** — if, say, the keyframe PNGs are
evicted while `on_screen.json` remains, `ingest_video` reads the cached extractions without noticing the
missing images. This is acceptable under the ADR-0002 model (the cache is treated as an atomic per-source
directory) but is a noted limitation; a coherence check or per-source cache-clear would be the fix if
partial eviction ever becomes real. Absolute paths in `artifacts.json`/`keyframes/index.json` make a
cache directory machine-local (fine — it lives under the user cache dir).

## Considered Options (key rejections)

- **No caching (redo everything each run).** Rejected: re-downloads gigabytes and re-runs GPU/LLM work on
  every retry.
- **One opaque cache blob for the whole ingestion.** Rejected: can't skip an individual stage; a change
  to keyframe params would still redo download + transcription.
- **Content-hash cache keys.** Rejected: `source_id` (`yt:<id>`) is already stable and unique per video;
  hashing adds nothing and obscures the on-disk layout.

## More Information

Extends `docs/decisions/0002-source-adapter-pattern.md`. Related: `src/money_pit/ingestion/pipeline.py`,
`src/money_pit/adapters/video.py`. Library choices are ADRs 0019 (cascade/whisper) and 0020 (VLM);
packaging is ADR 0022.
