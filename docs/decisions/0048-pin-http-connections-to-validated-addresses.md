---
status: accepted
date: 2026-07-29
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Pin HTTP Connections to Validated Addresses

## Context and Problem Statement

Network-backed source adapters accept user-configured URLs. The previous transport validated DNS results before opening a request with `urllib`, but `urllib` performed a second independent DNS lookup when it created the socket. A hostname could therefore resolve to a public address during validation and a private or otherwise non-global address during connection. Redirects repeated validation, but retained the same time-of-check/time-of-use gap.

The system needs an HTTP boundary that rejects non-global destinations and guarantees that the connection uses an address from the validated result set while preserving the URL hostname for HTTP routing and TLS certificate verification.

## Decision Drivers

- Every connected address must come from the exact DNS result set that passed validation.
- All returned A and AAAA records must be global; one non-global result rejects the hostname.
- DNS work, redirect handling, connection attempts, and response reads must share one request deadline.
- Resolution must have an enforceable timeout and a bounded result count.
- HTTPS must preserve the original hostname for SNI and certificate verification.
- HTTP must preserve the original hostname in the `Host` header.
- Redirects must repeat URL validation, DNS resolution, and address pinning.
- Source connector and `HttpTransport` contracts must remain stable and testable without network access.

## Considered Options

- Continue validating with `socket.getaddrinfo` before handing the URL to `urllib`.
- Resolve once and rewrite the request URL to contain the selected IP address.
- Use a third-party general-purpose HTTP client with custom DNS or transport hooks.
- Resolve with dnspython and own socket creation through an address-pinned `http.client` transport.

## Decision Outcome

Adopt dnspython 2.8.0 for bounded A and AAAA resolution. Resolution queries share a monotonic deadline, accept at most 32 unique addresses, and reject the hostname if any returned address is invalid or non-global.

The production transport connects directly to one of those validated IP addresses. Plain HTTP sends the validated URL hostname in the `Host` header. HTTPS wraps the connected socket with the URL hostname as the TLS `server_hostname`, retaining normal certificate-chain and hostname verification. The socket layer never resolves the hostname a second time.

Redirects are followed manually up to a fixed count of five. Every target is resolved relative to the prior URL and then passes the complete validation, resolution, and address-pinning process. All hops and address attempts consume one shared monotonic deadline.

The existing `HttpTransport` and connector interfaces remain unchanged. `HttpNameResolver` and `HttpAddressPinnedExchange` are injectable seams for deterministic tests. The historical `UrllibHttpTransport` class name remains temporarily for import compatibility even though its production implementation no longer uses `urllib`.

dnspython 2.8.0 was audited before adoption. It supports the project’s Python range, uses the ISC license, publishes a universal wheel, has no transitive runtime dependencies, was not yanked, and had no OSV advisories for version 2.8.0 at the time of review. Its active repository has multiple maintainers. The earlier CVE-2023-29483 issue was fixed in dnspython 2.6.1.

### Consequences

- Good, because DNS rebinding cannot change the address between validation and socket creation.
- Good, because A and AAAA resolution now has an enforceable deadline.
- Good, because TLS continues to verify the source hostname instead of the selected IP literal.
- Good, because redirects receive the same security checks as the initial URL.
- Good, because tests can inject resolution and exchange behavior without opening sockets.
- Bad, because the project gains a direct runtime dependency.
- Bad, because the transport owns more HTTP lifecycle code than the previous `urllib` wrapper.
- Neutral, because connection fallback may try multiple already validated public addresses.
- Neutral, because the compatibility class name is retained until connector imports can migrate in a separate bounded rename.

## Confirmation

Repository tests must demonstrate bounded A and AAAA resolution, rejection when any answer is non-global, address-count enforcement, one shared deadline, direct connection to the validated address, preserved `Host` and TLS SNI hostname, certificate verification, per-hop redirect revalidation, redirect limits, multi-address fallback, response-size enforcement, and unchanged connector behavior through the `HttpTransport` protocol.
