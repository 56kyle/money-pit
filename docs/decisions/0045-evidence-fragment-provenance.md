# Preserve claim provenance as immutable evidence fragments

## Status

Accepted

## Context and problem statement

The video adapter flattened transcripts and frame observations into prose. Downstream claims could retain a label such as `Bloomberg`, but could not identify the transcript cue or image that supplied the claim. A cited label is attribution, not independent proof.

Legacy cached payloads and signal JSON must remain readable while the persistent evidence system is introduced.

## Decision drivers

- Claims need stable locators into transcripts and frames.
- The LLM must not invent provenance identifiers.
- Existing cached JSON must continue to validate.

## Considered options

- Keep flattened strings and add human-readable locators.
- Store only source-level asset references on claims.
- Store immutable assets and exact fragment identifiers on claims.

## Decision outcome

Chosen option: store immutable assets and exact fragment identifiers on claims.

Caption and Whisper segments retain numeric start and end times. Frame extraction retains the numeric timestamp, image path, extractor method and model, and optional bounding box and confidence. Deterministic ingestion converts those records into `EvidenceAsset`, `EvidenceDocument`, and `EvidenceFragment` values with full SHA-256-based identifiers.

`VideoPayload` carries the asset and fragment catalog. A1 must return exact catalog identifiers for every video claim. Assembly rejects unknown identifiers before a `Claim` can enter the pipeline. `cited_sources` remains available as source attribution but does not satisfy verification or corroboration gates.

All newly persisted fields have defaults. Old transcript, payload, draft-claim, and claim JSON remains readable without a rewrite.

### Positive consequences

- Reports and verification can link a claim to exact transcript or frame evidence.
- Model-generated provenance is bounded by a deterministic allowlist.
- Historical cache recovery remains available.

### Negative consequences

- New payloads are larger than legacy payloads.
- Legacy payloads have no exact fragments until they are re-ingested.

## Confirmation

- Caption and Whisper ingestion preserve timestamped segments without removing flat transcript text.
- Video A1 receives a serialized evidence fragment catalog.
- Assembly raises a specific error for an unknown fragment identifier.

## More information
