# Content-addressed source_id for manual-note theses

- Status: accepted
- Date: 2026-07-17
- Deciders: owner, Claude Code

## Context and Problem Statement

The text-thesis source adapter (`adapters/text_source.py`) mints a `source_id` for each hand-written thesis,
the analog of the video path's `yt:<videoId>` (`ingestion/fetch.py`). Unlike a video, a written thesis has
no external stable identifier, so the adapter must *derive* one. That id is not cosmetic: it names the
persisted `text_payload.json` cache directory (via `source_id_to_dirname`), names the `signals/<id>.json`
file, and — crucially — becomes the **prefix of every `claim_id`** (`{source_id}:S001`, …). The `claim_id` is
a load-bearing join key: `compute/aggregation.py` uses it as a dedup and dict key, and A2/A4 must echo it
**verbatim** across three LLM hops.

The original plan minted `source_id = note:<run-slug>`, where the slug is the second-resolution UTC datetime
(`2026-07-17_14-30-05`). The design-questioner flagged the consequence: a long, punctuation-heavy prefix that
the LLM must reproduce character-for-character three times is a real, text-specific failure mode the video
path never faced (its `yt:<id>` is short and stable).

## Decision Drivers

- The `claim_id` prefix must be short and clean enough for reliable verbatim LLM echo (parity with the video
  path's risk level).
- Determinism/testability: id derivation should not depend on a wall clock, so the builder is unit-testable
  without freezing time.
- The per-run identity (the datetime slug, which drives the working directory) is a *separate* concern from
  the claim-id prefix; conflating them into one string is what created the problem.

## Considered Options

- **Content hash** — `source_id = note:<first 8 hex of sha256(body)>`.
- **Run slug** — `source_id = note:<datetime-run-slug>` (the original plan).
- **Random UUID** — `source_id = note:<uuid4>`.

## Decision Outcome

Chosen: **content hash**, `f"{_SOURCE_ID_PREFIX}:{sha256(body).hexdigest()[:_SOURCE_ID_HASH_LENGTH]}"`
with `_SOURCE_ID_PREFIX = "note"` and `_SOURCE_ID_HASH_LENGTH = 8` (`adapters/text_source.py`). This decouples
the two identities: `payload.slug` stays the per-run datetime (working directory), while `source_id` is a
short, clean, clock-free content address structurally identical to video's `yt:<id>`. Because the prefix is
now short, `claim_id` echo carries the same risk as the (green) video path, so claim ids remain LLM-minted per
the prompt — no deterministic re-minting was introduced.

### Consequences

- Good: the `claim_id` echo risk that would otherwise be text-specific is removed; the id matches the video
  path's shape and length.
- Good: deterministic and clock-free — `text_source_id` and `build_text_payload` are pure and unit-testable
  without a clock seam.
- Good/neutral: identical thesis body → identical `source_id`, so a re-run of the same thesis overwrites its
  own cache dir and signal file rather than accumulating duplicates. This is desirable idempotency for a
  content address (the run still gets a fresh working directory via the datetime slug).
- Bad (bounded): 8 hex is 32 bits of collision space. Two *distinct* theses whose sha256 collide in the first
  8 hex would share a cache dir / signal filename and overwrite each other. For a single-operator tool running
  a handful of theses, birthday-collision probability is negligible; if the tool ever ingests theses at scale,
  widen `_SOURCE_ID_HASH_LENGTH`. The knob is named for exactly this.

### Confirmation

`tests/unit_tests/adapters/test_text_source.py` pins the format (`note:` + 8 hex) and determinism (same body →
same id, different body → different id). ADR 0002 covers the adapter *shape*; this ADR covers only the
id-derivation decision that 0002 left open.
