---
status: accepted
date: 2026-08-10
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Ingest direct YouTube videos without discovery

## Context and Problem Statement

The YouTube connector used playlist discovery to create every source item. This made a YouTube Data
API key a prerequisite even when the caller already had the exact video URL. Treating a direct URL
as a temporary playlist cursor would also mix caller selection with synchronization state.

## Decision Drivers

- Allow one known YouTube video to enter the durable evidence lifecycle without playlist discovery.
- Preserve the configured source definition, provenance group, allowed uses, and trust settings.
- Keep sync and backfill cursor timelines unchanged.
- Reject playlist, channel, embed, and non-YouTube URLs before media acquisition.
- Keep bounded media acquisition and evidence processing on their existing implementations.

## Considered Options

- Require all videos to pass through uploads-playlist discovery.
- Add the direct URL as a temporary discovery cursor.
- Materialize a stable source item from a validated URL and use the existing fetch and processing lifecycle.

## Decision Outcome

Add `money-pit source ingest SOURCE_ID URL`. The selected source must be configured, enabled, and
support direct URL ingestion. The YouTube connector accepts canonical HTTPS watch and `youtu.be`
URLs, validates the stable 11-character video ID, and normalizes provenance to the canonical watch
URL. After acquisition, yt-dlp metadata must identify the same video and a channel whose `UC` ID
maps exactly to the configured `UU` uploads-playlist ID. The source item identity is
`SOURCE_ID:VIDEO_ID`. The fetched content digest replaces the provisional video ID as the content
version.

Direct ingestion uses the configured connector's bounded `yt-dlp` media transport and the existing
raw-asset, processing-attempt, and evidence persistence lifecycle. It does not call cursor repository
APIs and persists no cursor. The URL selects content only; it does not grant or change evidence
authority. Playlist sync and backfill continue to resolve `YOUTUBE_API_KEY` lazily through the
`youtube_discovery` scope.

Playlist discovery and yt-dlp can report different publication precision for the same video. The
first committed item version therefore owns its `published_at`, `updated_at`, and `discovered_at`
values. A later direct or playlist route must match the source-item ID, content digest, source ID,
definition hash, and canonical URI, then reuse all durable temporal fields.

This normalization occurs after `BEGIN IMMEDIATE` in the evidence-ingestion transaction. It is not a
read-before-write service decision, so concurrent exact ingestions deterministically converge on the
first committed row. The normalized item then flows into extraction, evidence acquisition, and
attempt construction. Successful evidence admission and its succeeded processing attempts commit in
one database transaction.

### Consequences

- Good, because known videos can be ingested without a YouTube Data API credential.
- Good, because direct and playlist ingestion produce the same durable evidence shapes.
- Good, because configured source policy remains the only authority boundary.
- Good, because direct ingestion cannot advance or reset synchronization state.
- Good, because playlist and direct routes are idempotent in either order and under concurrency.
- Bad, because callers must provide one supported canonical URL form.
- Neutral, because OpenAI frame interpretation remains independently credentialed as inference.

## Pros and Cons of the Options

### Playlist discovery only

- Good, because there is one item-materialization path.
- Bad, because an unrelated discovery credential blocks known-item ingestion.

### Temporary cursor

- Good, because the synchronization service could remain unchanged.
- Bad, because caller selection would mutate or overload durable discovery state.

### Direct stable item materialization

- Good, because credential and cursor boundaries match the requested operation.
- Good, because acquisition, processing, attempts, and evidence persistence stay shared.
- Bad, because connectors need an explicit direct-ingestion capability contract.

## More Information

ADR 0052 remains authoritative for bounded YouTube acquisition and derived media evidence. ADR 0063
remains authoritative for SecretSpec capability scoping.
