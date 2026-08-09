---
status: accepted
date: 2026-08-09
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Fetch Research Before Evidence Admission

## Context and Problem Statement

Research agents need web, filing, economic, market, and portfolio data. Search services return titles and snippets that can be incomplete, stale, or copied from another source. The system must keep search useful without treating discovery metadata as verified evidence.

## Decision Drivers

- A claim must cite an immutable evidence fragment.
- Source trust and allowed uses must apply to each fetched item.
- Research must not receive broker-write access.
- Provider calls need durable budgets and replayable outcomes.
- Long documents need bounded model-visible aliases without changing durable fragment IDs.

## Considered Options

- Let agents cite search snippets directly.
- Store provider responses only in run artifacts.
- Fetch selected results through the source and evidence lifecycle.

## Decision Outcome

Use `ResearchProvider.search` only for discovery. A provider must fetch a selected `ResearchDiscoveryResult` before the result can support a claim. The fetch creates a `RawArtifact`, a versioned `SourceItem`, a content-addressed asset, and processed evidence fragments. Search snippets stay in research records and never enter verification evidence.

Each provider has a configured source definition with a provenance group, allowed uses, and category trust. The research service enforces durable session, round, query, fetch, time, and provider bounds. It records searches, results, fetches, failures, stop decisions, and evidence-processing attempts.

Model prompts receive deterministic evidence aliases in bounded chunks. Model output must resolve each alias to a durable fragment ID before persistence. Unknown aliases fail closed.

IMAP access is read-only. It uses mailbox `UIDVALIDITY` and UIDs for cursor identity, limits discovery and message bytes, and does not modify message flags.

### Consequences

- Good, because every cited research fact has fetched source provenance.
- Good, because repeated and syndicated material can retain one provenance group.
- Good, because provider and prompt bounds are deterministic.
- Bad, because useful search results require a second request before citation.
- Bad, because each media type needs an explicit evidence processor.

## Confirmation

Tests must reject unknown providers and aliases, enforce every budget, keep snippets out of evidence, persist successful and failed attempts, deduplicate discovery URIs, and prove that selected results become durable evidence before citation.
