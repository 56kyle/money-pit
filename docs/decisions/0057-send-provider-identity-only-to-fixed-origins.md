---
status: accepted
date: 2026-08-09
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Send Provider Identity Only to Fixed Origins

## Context and Problem Statement

Brave search requires an API token and SEC EDGAR requires a descriptive User-Agent with a contact address. The normal web transport follows validated redirects. Following a redirect while retaining either header could disclose provider credentials or caller identity to a different origin.

## Decision Drivers

- Keep credentials out of URLs, durable records, and error diagnostics.
- Retain DNS rebinding and private-address protections from the address-pinned transport.
- Meet the SEC caller-identification requirement for search and filing fetches.
- Bound response bytes, elapsed time, and result counts before network access.
- Keep search snippets as discovery metadata rather than evidence.

## Considered Options

- Put credentials in query parameters and use the existing redirect-following request.
- Add arbitrary headers to every normal web request and preserve them across redirects.
- Add a one-hop, fixed-origin request to the existing address-pinned transport.

## Decision Outcome

Add a fixed-origin transport operation that accepts validated headers, performs one address-pinned request, and rejects every redirect. Header names and values are validated for injection and prohibited framing fields. Failures name only the sanitized request URL and never include header values.

The Brave backend calls only the configured Brave API origin with `X-Subscription-Token`. The EDGAR backend calls the SEC search origin and filing fetches accept only HTTPS URLs on `sec.gov` or `www.sec.gov`, both with the configured SEC User-Agent. Provider responses have explicit byte, time, and result limits.

Secrets and caller identity are supplied at composition time. Source definitions identify the provider and its evidence policy but do not contain credentials.

### Consequences

- Good, because credentials cannot follow a redirect to another origin.
- Good, because provider traffic retains the existing address-pinning and public-address checks.
- Good, because diagnostics can be retained without exposing credential values.
- Bad, because provider redirects fail even when the target would otherwise be safe.
- Bad, because each authenticated provider needs an explicit backend rather than the generic web backend.

## Confirmation

Tests must prove that provider headers reach the pinned exchange, redirect responses fail without a second request, error text excludes credentials, result and response bounds fail closed, EDGAR rejects non-SEC filing URLs before I/O, and snippets remain discovery-only.
