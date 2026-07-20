# Swap the LLM provider from Anthropic (claude-sonnet-5) to OpenAI (gpt-5)

- Status: accepted
- Date: 2026-07-20
- Deciders: owner, Claude Code

## Context and Problem Statement

Every LLM agent in the pipeline (A1 video/text classifier, on-screen vision extractor, claim-question,
thesis-judgment, and answer-synthesis agents) is constructed via a pydantic-ai model string of the form
`f"{PREFIX}{config.llm_model}"`, where the provider is inferred from `PREFIX` and the API key is read from
the process env. The prefix was `"anthropic:"` and the default model `claude-sonnet-5`, so runs authenticated
against the Anthropic Messages API using an `x-api-key` credential.

The owner authenticates Claude Code with a subscription OAuth token (`claude setup-token`), not a Console API
key. That token cannot authenticate raw Messages API calls — the API rejects it with `401 invalid x-api-key` —
because Anthropic firewalls the flat-rate subscription (Claude Code / Claude.ai) from the metered Platform API,
which bills a separate Console account. The owner is cancelling the subscription and declines to fund a
separate Anthropic Console billing account, but already holds an OpenAI API credential.

Question: how does the pipeline keep making LLM calls without an Anthropic Console key?

## Decision Drivers

- The subscription↔API billing firewall is a policy wall, not a bug — no Anthropic setting routes API usage to
  the subscription's payment.
- The owner already has a working OpenAI credential and no OpenAI billing objection.
- pydantic-ai infers the provider from the model-string prefix and reads the provider key from the env, so the
  provider is a one-constant swap with no new dependency (`openai` is already a transitive dependency).
- A single `llm_model` drives all agents, so the chosen model must support **both** JSON-schema structured
  output and image input (the on-screen-text step feeds keyframe images).

## Considered Options

- **Anthropic Console credits** — keep the provider, add a metered Console account. Rejected: the owner
  explicitly declines a second Anthropic billing relationship.
- **Route API calls through the subscription OAuth flow** — send the OAuth token as a `Bearer` header with the
  OAuth beta header, impersonating Claude Code. Rejected: it requires prepending the Claude Code identity to
  every agent's system prompt (corrupting the tuned A1 contract), is fragile against beta rotation, and
  violates the subscription's terms.
- **Swap the provider to OpenAI** — change the prefix to `"openai:"` and the default model to an OpenAI model.

## Decision Outcome

Chosen: **swap to OpenAI**, default model `gpt-5`. `ANTHROPIC_MODEL_PREFIX = "anthropic:"` becomes
`OPENAI_MODEL_PREFIX = "openai:"`; `_DEFAULT_LLM_MODEL` becomes `gpt-5`. Authentication moves from
`ANTHROPIC_API_KEY` to `OPENAI_API_KEY`, both read from the process env by pydantic-ai — no change to how the
key is resolved, only which env var. `gpt-5` was chosen over `gpt-4.1`/`gpt-4o`/`o4-mini` for maximum
reasoning fidelity on the judgment-heavy agents, cost being secondary for the owner; all four support the
required structured-output + vision combination, so the default is a one-line change if the cost/quality
tradeoff is revisited.

### Consequences

- Good: uses the owner's existing OpenAI credential; the change surface is one constant, one default, and
  their usage sites — no schema, prompt-logic, or dependency changes.
- Neutral: the model is now selected purely by `config.llm_model`; pointing at any valid OpenAI model id is a
  config change, not a code change.
- Bad: the A1 prompts (`agent_1.md`, `agent_1_text.md`) were tuned against Claude's forced-tool-call output
  path; on OpenAI the same `output_type` contracts are satisfied through OpenAI's native json-schema mode, so
  classification quality and structured-output adherence are not guaranteed identical and may need prompt
  retuning. This is why the swap is gated on an end-to-end confirmation run rather than the offline suite alone.
- Bad: `prompts/*.md` and ADR 0029's sync-obligation note still name "Anthropic" in HTML comments. These are
  documentation strings, not behavior, and were left intact; they now describe history, not the live provider.

### Confirmation

Offline: the unit suite is green with the renamed constant and `gpt-5` default (`test_config.py` pins the new
default; no `ANTHROPIC_MODEL_PREFIX` remains in `src/`). End-to-end: a real
`money-pit ingest <youtube-url>` run against `gpt-5`, which exercises both the A1 structured-output path and
the vision on-screen-text path, must reach a written boundary-0 signal file without a structured-output
validation error. That run is the confirmation that OpenAI honors the `SignalSetDraft` / on-screen contracts;
a failure there is an implementation follow-up (retune prompts or pick a different OpenAI model), not a
reversal of this decision.
