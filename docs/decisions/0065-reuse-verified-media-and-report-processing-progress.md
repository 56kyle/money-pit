---
status: accepted
date: 2026-08-12
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Reuse verified media and report processing progress

## Context and Problem Statement

Direct YouTube ingestion downloaded the same immutable bytes on every attempt, even when a prior
attempt had persisted the raw acquisition before evidence processing failed. Media processing then
ran for many minutes without application-level progress. Faster Whisper used an automatic device
with `int8`, which did not take advantage of a supported CUDA `float16` backend. Frame extraction
also sent structured output and reasoning through Chat Completions, a combination rejected by the
configured OpenAI reasoning model.

## Decision Drivers

- Reuse raw acquisitions without introducing a second downloader cache or cache database.
- Keep source-definition revisions and configured byte limits in the cache authority boundary.
- Detect missing and corrupt durable content before it enters media processing.
- Make long-running work observable without changing machine-readable command output.
- Prefer a supported GPU path while retaining a deterministic CPU fallback.
- Preserve reasoning and structured frame output on the API intended for that combination.

## Considered Options

- Always download, use yt-dlp's cache, or reuse the application's durable content-addressed assets.
- Expire cached videos by time, or retain them until an explicit refresh.
- Use Faster Whisper's automatic device selection, force CPU, or explicitly prefer supported CUDA
  `float16` and otherwise use CPU `int8`.
- Disable reasoning for frame extraction on Chat Completions, or move only frame extraction to the
  Responses API.
- Print progress from library code, or emit typed events to an injected observer.

## Decision Outcome

Direct ingestion reuses the latest raw acquisition whose source-item identity and current source
definition hash match the requested URL. The existing source item, acquisition, and asset rows are
the cache index. A raw acquisition is identified by its audio or video media type; derived frame
assets use an image media type. The `AssetStore` verifies the canonical path, full SHA-256 digest,
and current connector byte bound before returning bytes. A missing row or missing file causes a new
bounded fetch. Conflicting paths, hashes, metadata, or oversized cached content fail closed as
integrity errors.

Cache entries do not expire. `money-pit source ingest --refresh` bypasses lookup and performs a new
fetch. A source-definition change also prevents reuse because the definition hash is part of the
lookup. Raw acquisitions persisted by failed processing attempts remain eligible, so retrying an
analysis failure does not download the video again.

Schema 0.0.2 keys a source item by item ID and content version but omits the definition hash. Until
that key can be migrated, every new YouTube acquisition uses a transparent
`CONTENT_DIGEST:DEFINITION_HASH` content version to prevent collision between policy revisions.
Cache lookup remains compatible with legacy digest-only rows. This compatibility step is tracked in
the stepping-stone ledger; the asset identity remains the exact content digest.

Long-running components emit frozen, typed progress events through an injected callback. Library
callers receive a no-op default. The CLI renders events to standard error, leaving the final JSON on
standard output. Events contain only stable stages, bounded counts, backend names, and non-secret
details.

Scene detection probes at most 2,000 frames across the full video. The analyzer derives
PySceneDetect's `frame_skip` from the actual frame count, delegates detection through `SceneManager`,
and emits throttled typed progress from 0 through 100 without enabling tqdm. Frame selection divides
the complete duration into at most the configured maximum number of time bins. Each bin uses the
detected cut nearest its midpoint when one exists, otherwise the midpoint itself. This makes scene
cuts useful without allowing a no-cut or low-cut video to lose timeline coverage. The
`timestamped-media` processor version is 2 because this bounded hybrid policy changes which frame
evidence can be produced.

Faster Whisper selects CUDA `float16` only when CTranslate2 reports a CUDA device and support for
that compute type. It otherwise uses CPU `int8`. A CUDA attempt falls back to CPU only when the
failure identifies a CUDA, cuBLAS, cuDNN, compute-type, or GPU-driver capability problem; unrelated
runtime failures remain extraction errors.

Frame extraction uses a frame-specific `OpenAIResponsesModel` factory with the same SecretSpec
credential resolution and configured model name. The A1 through A4 agents remain on the existing
Chat Completions factory. This preserves the requested reasoning behavior instead of silently
setting its effort to `none`.

### Consequences

- Good, because sequential direct retries avoid repeated downloads and can recover old failed work.
- Good, because cache authority remains in the durable provenance and asset system.
- Good, because operators can distinguish downloading, transcription, frame work, and persistence.
- Good, because scene detection cost is bounded independently of the video's total frame count.
- Good, because every full-duration time bin contributes a frame candidate even when no cuts exist.
- Good, because supported NVIDIA GPUs reduce transcription time without making CUDA mandatory.
- Good, because frame reasoning and structured output use a compatible OpenAI endpoint.
- Bad, because cached YouTube bytes can become stale if a creator replaces content at the same URL;
  callers must request `--refresh` when they need reacquisition.
- Bad, because GPU capability classification depends on native-library failure descriptions.
- Bad, because sparse scene probing can miss cuts between probed frames.
- Neutral, because a missing cache file is repaired by fetching, while evidence of corruption stops
  ingestion for operator review.

## Pros and Cons of the Options

### Application durable-asset reuse with explicit refresh

- Good, because it avoids duplicate caches and preserves acquisition provenance.
- Good, because refresh is intentional and easy to audit.
- Bad, because there is no automatic freshness interval.

### Time-based expiry

- Good, because remote replacements would eventually be fetched.
- Bad, because unchanged large videos would be downloaded repeatedly without evidence of change.

### Disable reasoning on Chat Completions

- Good, because it is a small endpoint-compatible change.
- Bad, because it silently weakens frame interpretation to accommodate an older endpoint.

### Frame-specific Responses API

- Good, because it supports the configured reasoning model and structured frame result.
- Good, because it does not migrate unrelated agents.
- Bad, because the application now intentionally uses two OpenAI model factories.

## Confirmation

Tests must pin refresh bypass, definition-bound cache lookup, reuse after failed processing, missing
and corrupt cache behavior, progress stream separation, backend selection and fallback, and the
frame-specific Responses factory. Ruff, basedpyright, and scoped offline tests must pass.
