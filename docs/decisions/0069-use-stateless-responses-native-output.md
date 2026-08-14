---
status: accepted
date: 2026-08-13
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Use stateless Responses native output for intelligence inference

## Context and Problem Statement

The A1 through A4 inference agents sent a manually rendered text envelope through Chat Completions. That envelope repeated the packaged instructions, output schema, model metadata, and typed request inside one user message. Production code then parsed model text as JSON, with retry and usage accounting implemented separately from the SDK's structured-output lifecycle. This obscured provider message roles, transmitted the schema twice when structured output was enabled, and made prompt caching sensitive to request-specific content.

The intelligence stages need one explicit provider boundary that preserves their existing semantic policy identities while using OpenAI's current stateless Responses and structured-output capabilities.

## Decision Drivers

- Keep packaged prompts in the provider instruction role.
- Validate A1 through A4 outputs against their Pydantic draft types at the provider boundary.
- Send only compact canonical request JSON as user input.
- Keep inference stateless and disable provider response storage.
- Use explicit medium reasoning for every intelligence stage.
- Make the prompt cache hint stable across runs and requests with the same prompt policy.
- Retain all usage that PydanticAI reports across native-output retries and failures.
- Reject oversized requests locally while counting static instructions and schema exactly once.
- Preserve existing A1 through A4 stage and purpose identities.

## Considered Options

- Retain Chat Completions and the manual text envelope.
- Use Responses but continue parsing untyped JSON text in application code.
- Use stateless Responses with strict native structured output.

## Decision Outcome

Chosen option: use `OpenAIResponsesModel` and strict PydanticAI `NativeOutput` for A1 through A4. Each agent receives its packaged prompt through `instructions`, its draft model through strict native output, and compact `model_dump_json()` request content as the sole user input. Each logical invocation permits one native-output retry. PydanticAI owns schema validation and retry composition; application code does not add another parsing or retry layer.

Responses settings set `openai_store=False` and `openai_reasoning_effort="medium"`. They omit `openai_previous_response_id`, so no request chains to provider-side response state. The prompt cache key is a SHA-256 digest of the stage, model, exact packaged prompt content, application prompt version, and canonical output schema. It excludes request content, run identity, and work-unit identity. This key is a transport optimization only and does not change durable semantic work identities.

One `RunUsage` accumulator is passed into the complete PydanticAI run. A successful logical invocation records its aggregate request and token counts. When validation or a later retry fails, the invocation records the aggregate usage if the SDK exposed at least one request or token count. Usage remains unavailable rather than becoming a synthetic zero when the SDK exposed no accounting.

The local character gate counts compact user JSON plus the exact packaged instructions and canonical native-output schema, with the configured response reserve. The schema remains part of preflight accounting but is not embedded in the user message, so it is not transmitted manually in addition to the provider-native schema.

### Consequences

- Good, because the provider receives instructions, schema, and user data through their intended channels.
- Good, because strict typed drafts replace application-level JSON parsing.
- Good, because one logical usage record covers all SDK-managed output retries.
- Good, because prompt cache identity is stable across equivalent work without coupling to durable request identity.
- Good, because response storage and previous-response chaining are disabled by construction.
- Bad, because inference now depends on model support for strict Responses structured output.
- Neutral, because character budgets remain deterministic projections rather than provider token counts.

## Pros and Cons of the Options

### Retain Chat Completions and the manual envelope

- Good, because it preserves the existing provider call shape.
- Bad, because instructions and schemas are flattened into user text and schema validation remains duplicated.
- Bad, because stable prompt-prefix caching cannot be isolated cleanly from request content.

### Use Responses with untyped JSON text

- Good, because it adopts the current provider API and stateless settings.
- Bad, because application parsing and retry behavior still duplicate SDK capabilities.
- Bad, because malformed output remains an avoidable application boundary state.

### Use stateless Responses with strict native output

- Good, because provider roles and structured-output validation are explicit.
- Good, because SDK-managed retries expose one aggregatable usage lifecycle.
- Bad, because models without strict Responses support cannot serve these stages.

## Confirmation

Tests must pin compact user input, packaged instructions, strict native output, Responses model construction, one retry, stable cache-key inputs, stateless settings, aggregate success and failure usage, unavailable usage, and preflight accounting without a duplicated manual schema. Scoped Ruff, basedpyright, and inference-agent tests must pass before merge.
