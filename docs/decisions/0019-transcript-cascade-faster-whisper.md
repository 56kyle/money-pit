# Captions-first transcript cascade with a faster-whisper fallback

- Status: accepted
- Date: 2026-07-14
- Deciders: owner, python-dev

## Context and Problem Statement

Phase 9's video ingestion must produce, for each episode, a transcript plus a `has_word_timestamps` flag
(the `VideoPayload` contract). The architecture spec (§8a build-vs-integrate, §13 stack) named **WhisperX
/ faster-whisper** for word-level timestamps. Two decisions were open: how to obtain the transcript
(always transcribe vs. use the video's own captions), and which transcription backend.

## Decision Drivers

- The video's own captions (uploader, then YouTube auto-captions) are free, already accurate for the
  common case, and avoid running an expensive model at all.
- Transcription is the expensive fallback; it should run only when captions are absent.
- Word-level timestamps (needed for narration↔frame fusion) come only from the transcription path.
- Keep the dependency tree as light as the runtime allows (owner runs locally with a GPU).

## Decision Outcome

1. **Captions-first cascade** (`ingestion/captions.py`, `ingestion/transcribe.py`): prefer uploader
   captions → auto captions → **faster-whisper transcription only when no captions exist**. The existing
   `TranscriptSource` enum records which ran (`UPLOADER_CAPTIONS` / `AUTO_CAPTIONS` / `WHISPER`). Captions
   are segment-level, so `has_word_timestamps=False` on both caption branches; faster-whisper is run with
   `word_timestamps=True`, so the whisper branch sets `has_word_timestamps=True`.
2. **Backend: faster-whisper (CTranslate2), not WhisperX.** faster-whisper rides CTranslate2 — no PyTorch,
   no pyannote — a materially lighter dependency tree that still yields word-level timestamps. WhisperX's
   extra value is forced alignment via pyannote, which adds substantial weight for marginal benefit given
   captions cover the common path and word timing is a secondary fusion input. **No torch promotion:**
   CTranslate2 is independent of PyTorch; GPU acceleration needs the CUDA + cuDNN *runtime* libraries (a
   machine-setup note), not a Python dependency change.
3. **Captions parsed in-house** (WebVTT): strip inline word-timing tags (`<00:00:01.240>`, `<c>` markup),
   dedupe rolling-duplicate cues (a consecutive-duplicate heuristic). Adopt `webvtt-py` only if real
   captions prove too fiddly.

### Consequences

Good: the cheap common path (captions) avoids the model entirely; `transcript_source` records honest
provenance; the backend is far lighter than WhisperX while still producing word timestamps when it runs.

Bad / to watch: the caption path carries `has_word_timestamps=False`, so precise narration↔frame timing is
unavailable when captions are used — acceptable, since on-screen extraction keys off keyframe locators
independently. The rolling-caption dedupe is a heuristic and may under-/over-merge unusual caption files
(the fallback is `webvtt-py`). faster-whisper GPU acceleration depends on CUDA+cuDNN runtime libs present
on the machine, surfaced by the live-video tier rather than a Python dep.

## Considered Options (key rejections)

- **WhisperX (the literal spec choice).** Rejected: heaviest dep tree (torch + pyannote) for forced
  alignment whose benefit is marginal here; faster-whisper gives word timestamps without it.
- **Always transcribe, ignore captions.** Rejected: wasteful and slow when accurate captions exist.
- **Captions-only, no transcription fallback.** Rejected: fails on caption-less videos and never yields
  word timestamps.
- **Cloud transcription API.** Rejected by the owner in favor of local GPU (avoids a new network
  dependency, per-minute cost, and API key).

## More Information

Records the deviation from the WhisperX naming in `docs/architecture.md` §8a/§13. Related:
`src/money_pit/ingestion/captions.py`, `src/money_pit/ingestion/transcribe.py`,
`src/money_pit/ingestion/artifacts.py`, `src/money_pit/adapters/video_llm.py` (`TranscriptSource`).
The VLM-for-on-screen decision is ADR 0020; per-stage caching is ADR 0021; packaging is ADR 0022.
