---
status: accepted
date: 2026-08-09
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Resolve credentials through SecretSpec

## Context and Problem Statement

money-pit loaded secrets from dotenv, Pydantic settings, direct environment reads, and a separate
Alpaca keyring convention. Those paths made credential authority depend on ambient process state.
They also made paper and live account separation depend on values outside the declared application
contract. The configured model name was incorrectly stored beside secrets even though it affects
replay and decision identity.

## Decision Drivers

- Deliver only the credentials required by one runtime capability.
- Keep paper and live account state unable to satisfy each other's workflow.
- Keep credentials out of configuration hashes, artifacts, logs, and exception messages.
- Make local and automated provider selection explicit.
- Keep behavioral model configuration versioned with the strategy.

## Considered Options

- Keep the existing dotenv and direct-keyring loaders.
- Run the application under `secretspec run` and consume exported environment variables.
- Use the SecretSpec Python SDK and convert scoped results into project-owned types.

## Decision Outcome

Use the SecretSpec Python SDK directly. The tracked `secretspec.toml` declares a local keyring-backed
`development` profile and an explicit environment-backed `ci` profile. Each declaration names its provider; there
is no provider fallback. Named scopes allow only the credentials for inference, source acquisition,
research, portfolio reads, or execution. Paper and live Alpaca credentials have distinct names and
distinct scopes.

YouTube playlist enumeration uses the `youtube_discovery` scope, which contains only
`YOUTUBE_API_KEY`. Media processing remains under the independent `inference` scope. Constructing a
YouTube connector does not resolve either credential. Playlist discovery resolves its credential
lazily, while direct ingestion of a caller-provided video URL bypasses playlist discovery.

The `required` flag applies when SecretSpec resolves a scope that contains the declaration. It does
not make the complete profile a startup credential bundle. Each scope keeps its atomic credentials
required. Disabled or uninvoked capabilities do not resolve their scopes. If an enabled capability
is invoked, missing or blank values fail through the sanitized typed error contract.

The application passes a scope, profile, manifest path, and audit reason to SecretSpec. It converts
the result into a frozen typed credential and closes the result immediately. It never calls
`set_as_env`. SDK manifest, profile, and provider failures become configuration failures. Missing or
blank required values become credential-resolution failures. Neither error contract includes a
credential value.

The `SECRETSPEC_PROFILE` environment variable is the only bootstrap selector. An unset selector uses
`development`; automation must explicitly select `ci`. A blank selector is invalid. Source listing and replay resolve no credentials.
The configured OpenAI model moves to `strategy.toml`, where existing configuration hashes and model
bindings cover it.

### Consequences

- Good, because credential capability is declared and testable.
- Good, because SecretSpec manages local storage without a money-pit-specific keyring convention.
- Good, because paper and live account credentials cannot cross by sharing one username field.
- Good, because replay and non-network inspection do not require credentials.
- Bad, because operators must install the SecretSpec CLI to administer local values.
- Neutral, because existing ignored credential material is left untouched and is not migrated.

## Pros and Cons of the Options

### Existing loaders

- Good, because they require no operator migration.
- Bad, because multiple ambient resolution paths remain authoritative.

### `secretspec run`

- Good, because application code would not depend on the SDK.
- Bad, because all resolved values would enter shared process environment state.

### Direct SDK resolution

- Good, because the application requests and validates one capability scope.
- Good, because provider results can be converted without exporting them.
- Bad, because credential resolution becomes an explicit application dependency.

## More Information

This decision supersedes the credential portions of ADR 0050 and ADR 0062. It retains their
persistence and evidence-led allocation decisions.
