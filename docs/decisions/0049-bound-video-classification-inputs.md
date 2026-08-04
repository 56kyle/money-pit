---
status: accepted
date: 2026-08-04
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Bound Video Classification Inputs

## Context and Problem Statement

The video classifier received the complete transcript and a verbose JSON copy of every evidence fragment in one model request. YouTube rolling captions produce overlapping cues, so a 28-minute video created 1,652 transcript fragments and a request that exceeded the GPT-5 context window.

The classifier needs bounded input without weakening the exact-fragment provenance contract in ADR 0045 or rewriting historical cached payloads.

## Decision Drivers

- Every persisted claim must retain exact identifiers from the durable evidence catalog.
- Existing `VideoPayload` and evidence JSON must remain readable without migration.
- Model requests must have a deterministic size limit before network I/O.
- Long videos must retain all evidence through bounded classification chunks.
- Chunk composition must not let model-generated identifiers become downstream join keys.
- Error diagnostics must not include transcript or catalog content.

## Considered Options

- Increase the configured model context window and keep one unbounded request.
- Truncate transcripts or evidence catalogs at the request boundary.
- Normalize and replace durable caption fragments during ingestion.
- Build a bounded, ephemeral evidence projection that maps compact aliases to durable fragments.

## Decision Outcome

Build an ephemeral prompt projection at the video-classification boundary. The projection removes repeated rolling-caption text, groups transcript text into bounded timestamped spans, groups related frame fragments, and assigns compact aliases. Each alias maps to one or more original evidence fragment identifiers. The projection does not change `VideoPayload`, cached artifacts, or durable evidence identity.

Measure the rendered model input with a conservative character budget. Reserve output capacity before allocating space to evidence. Split oversized projections only at evidence-span boundaries. Each chunk includes one adjacent span on each side as context and identifies its core aliases. A chunk can emit only claims whose earliest supporting alias belongs to its core.

The model returns chunk-local drafts and evidence aliases. Deterministic composition resolves aliases to durable fragment identifiers, rejects unknown aliases, removes exact duplicates, orders claims globally, and assigns final source-level claim identifiers once. Multi-chunk summaries use a separate bounded summary reduction. The existing whole-payload evidence allowlist remains a final validation layer.

Use provider-independent character measurement instead of adding a tokenizer dependency. The default context budget is 160,000 characters with a 32,000-character response reserve. A local irreducible-budget failure and a provider context rejection use separate typed exceptions. Neither exception includes source content.

### Consequences

- Good, because request size is bounded before the model call.
- Good, because historical payloads benefit without cache rewrites.
- Good, because claim provenance still resolves to immutable evidence fragments.
- Good, because long videos can be processed without silent truncation.
- Good, because final claim identifiers and ordering are deterministic across chunks.
- Bad, because video classification and testing gain projection and composition complexity.
- Bad, because character measurement is conservative and does not exactly predict provider tokenization.
- Neutral, because chunked videos can require additional model calls.

## Confirmation

Tests must cover rolling-caption overlap, alias resolution, frame grouping, legacy payloads, deterministic chunk boundaries, context ownership, global composition, irreducible budget failures, provider context errors, and a synthetic 1,652-cue regression case. Existing unknown-fragment validation remains a regression requirement.
