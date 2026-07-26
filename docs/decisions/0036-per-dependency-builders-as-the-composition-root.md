---
status: accepted
date: 2026-07-26
decision-makers: [Kyle Oliver]
consulted: []
informed: []
---

# Per-Dependency Builders Own Override-or-Default Precedence

## Context and Problem Statement

The pipeline's nodes are orthogonal. Every factory takes exactly the dependencies it uses and
nothing else:

```
make_snapshot_node(fetch_portfolio)              make_validator_node(manifest=None)
make_aggregator_node(corroboration_agent)        make_determination_node()
make_questions_node(claim_questions_agent, *, current_events_lookback_days)
make_retrieval_node(answer_synthesis_agent, deterministic_tools)
make_analysis_node(config, thesis_agent, resolve_instrument_facts)
```

The composition root did not honor that. `run_pipeline` inlined the construction of every agent and
tool in its own body, and `_require_capital_critical_deps` raises unless `fetch_portfolio`,
`place_order`, `observe_fill` *and* `send_email` are all present. The consequence: running the
`snapshot` node alone — which needs one read-only Alpaca `TradingClient` — required also
constructing an order placer, a fill observer, an email sender, and the live MCP write manifest.

That is what blocks granular execution. Not the node design, which is already right, but a
composition root that offers exactly one door and demands the full dependency set to open it.

How should dependency construction be factored so a caller can obtain one node's dependencies
without the rest?

## Decision Drivers

- Exactly one construction path per dependency. A second place that builds a default will drift from
  the real one, and the drift would be invisible: a stage run would silently use different
  dependencies than a production run.
- A full run must keep failing closed on a missing capital-critical dependency.
- The refactor must be behavior-identical; it enables a capability, it does not add one.

## Considered Options

- Per-dependency builders that own override-or-default precedence
- Per-dependency builders that construct only the default, with callers applying the override
- A single `build_all_deps(config)` returning a fully-populated container
- Leave construction inline and let each caller assemble what it needs

## Decision Outcome

Chosen option: **per-dependency builders owning the precedence policy**. Seven module-level
functions in `pipeline/orchestration.py`:

```
deterministic_research_tools_or_default(overrides, config)   claim_questions_agent_or_default(overrides, config)
open_ended_research_tools_or_default(overrides, config)      answer_synthesis_agent_or_default(overrides, config)
thesis_agent_or_default(overrides, config)                   corroboration_agent_or_default(overrides)
instrument_facts_resolver_or_default(overrides)
```

Each returns `overrides.X` when supplied, else constructs the production default. `run_pipeline`
calls these rather than owning the construction, so there is one path per dependency.

**The builders own the precedence policy rather than just the default.** The alternative — builders
construct only the default, callers apply the override — satisfies the letter of "one construction
path" but not its spirit: every caller would then re-derive the precedence rule per dependency,
which is the same drift hazard moved up one level.

**`_require_capital_critical_deps` is untouched** and still guards `run_pipeline`. The point is not
to weaken the full-run gate but to stop routing single-stage construction through it.

### Naming

`*_or_default` over the initially-written `build_*`. `build_X` mis-advertises an *aliasing*
contract: a reader assumes two calls yield two objects, but when an override is present two calls
yield the same object, and that difference is invisible at the call site. `*_or_default` names the
precedence policy directly, which is the entire reason these functions exist over the bare `make_*`
factories they wrap.

`resolve_*` was considered and rejected: the verb is already claimed twice in this codebase for
different meanings — `ResolveInstrumentFacts` means "fetch over the network",
`resolve_alpaca_credentials` means "obtain from a credential store". A third sense would be worse
than a slightly loose `build_`, and `*_or_default` avoids the collision entirely.

### Precedence is presence, not truthiness

The original inline code read `ov.X or <default>`, making the policy "truthy wins." Every override
today is a function or plain object and therefore truthy, so nothing was broken — but these
functions are now the policy *owner*, and the written policy should be the intended one. Each uses
an explicit `is not None` check.

### Consequences

- Good, because one node's dependencies can now be constructed without the others. `validator` and
  `determination` build with no credentials at all; `snapshot` needs only a read-only client.
- Good, because override precedence is defined once. A future stage runner gets identical semantics
  to `run_pipeline` by calling the same function, not by reimplementing the rule.
- Good, because `answer_synthesis_agent_or_default` resolves its own open-ended tools instead of
  taking them as a parameter — `open_tools` was only ever consumed by that agent, so threading it
  through the caller leaked a construction detail.
- Bad, because three builders now carry an undeclared-until-now failure mode across seven entry
  points instead of one. See below.
- Neutral, because construction *order* changed (it is now argument-evaluation order at the
  `build_graph` call). All constructors are side-effect-free except the three OpenAI-backed ones, so
  the only observable difference is which of them raises first on a keyless machine.

### The OpenAI credential asymmetry

`thesis_agent_or_default`, `claim_questions_agent_or_default`, and
`answer_synthesis_agent_or_default` raise `openai.OpenAIError` when no override is supplied and
`OPENAI_API_KEY` is absent — pydantic-ai resolves the model eagerly (`defer_model_check=False`).
This is now stated in each docstring, because a unit test had to plant a bogus key to get past it,
and a failure mode the tests work around is one that belongs in the contract.

It also exposes a real inconsistency, recorded here rather than fixed: these builders take
`config: Config`, implying `Config` is the source of what they need, but the OpenAI key is read from
the ambient environment. Every other credential in this codebase goes through the DI scheme —
`fred_api_key` and `brave_api_key` as `SecretStr` fields, Alpaca and Gmail via keyring. OpenAI alone
is ambient. The honest fix is an `openai_api_key` field on `Config` threaded into the agent
factories, which would turn an opaque third-party error into a `CredentialResolutionError` at a
known choke point and delete the test's env-var workaround. Deferred deliberately: it changes agent
factories and credential handling, which deserves its own change and its own ADR rather than riding
along in a behavior-identical refactor.

### Confirmation

`tests/unit_tests/pipeline/test_orchestration.py` pins, per builder, that a supplied override is
returned by identity (not equality — so a wrapped or copied override fails), and that each default
constructs offline without raising. A drift guard derives the builder set from the module namespace
via `inspect.getmembers` and asserts every `*_or_default` appears in a precedence table, so an
eighth builder added later cannot silently escape coverage — that guard was verified to fire by
injecting a fake builder. A parametrized test pins the fail-closed-at-construction property of the
three OpenAI-backed builders.

The refactor itself is confirmed behavior-identical by the pre-existing suite passing unchanged.

## Pros and Cons of the Options

### Per-dependency builders owning precedence

- Good, because a caller needing one dependency calls one function and gets production semantics.
- Good, because the precedence rule exists in exactly one place per dependency.
- Bad, because the two builders whose defaults need no `Config` have a different arity, so the
  family is not uniformly iterable.

### Builders construct only the default; callers apply the override

- Good, because each builder is trivially pure — no branch, no policy.
- Bad, because every call site re-derives precedence. That is the original drift hazard relocated,
  and it would be re-derived once per stage in the stage runner. Decisive against.

### A single `build_all_deps(config)` container

- Good, because one call site and no partial-construction questions.
- Bad, because it is the current problem with a nicer name: obtaining one dependency still
  constructs all of them, including the three that need an OpenAI key and the one that needs Alpaca.

### Leave construction inline

- Good, because no change.
- Bad, because granular execution stays impossible, which is the requirement driving this.

## More Information

**Terminus.** These builders have one caller today (`run_pipeline`). Their named purpose is a stage
runner — a `run_stage(stage, run_dir, config)` entry point constructing exactly one node's
dependencies, exposed as `money-pit stage <name> --run-dir <dir>`, covering the seven side-effect-free
observation stages (`snapshot`, `aggregator`, `questions`, `retrieval`, `analysis`, `validator`,
`determination`). `recovery` and `notification` send email and `execution` places orders, so those
are excluded from single-stage invocation. Until that lands, these are seven public functions with a
single caller, and this paragraph is the record of why.

Import-graph constraint worth preserving: `orchestration.py` imports `graph.graph`, which imports
every node module. A **node** can therefore never import a builder. The stage runner must sit above
the nodes — a peer of `orchestration`, or in `__main__.py`. If that boundary becomes uncomfortable,
the fix is a `pipeline/dependencies.py` holding `PipelineOverrides` plus the builders, which needs
only `agents/*`, `market_data`, `contracts`, and `config` — never `graph`.

### Two constraints found while building the stage runner

**Build-time and invocation-time credential-freedom are different properties.** `validator` and
`determination` are credential-free in both senses — they build and run with no secrets at all, which
is what "runs without credentials" means to an operator. `aggregator` *builds* credential-free
(`corroboration_agent_or_default` returns the pure `corroborate` stub) but its node may call an LLM
when invoked. Only the former pair is claimed as credential-free; the tests pin exactly those two,
with `snapshot`, `questions`, `retrieval` and `analysis` as the negative control that keeps the
positive assertion from passing for the wrong reason.

**A run directory is identified by its name.** `run_stage` takes the slug from `run_dir.name`, which
is the pipeline's own convention (`run_pipeline` writes to `DAILY_SHOW_ROOT / slug`). But
`run_pipeline(run_dir=...)` with an explicit directory derives its slug from the clock and does *not*
rename the directory, so the two diverge. A stage then run against that directory sees a different
slug, which surfaces as validator UNMATCHED (the `client_order_id == f"{slug}:{step_id}"` check) and
would trip ADR 0037's staleness guard.

This does not arise from the CLI, whose commands use the slug-named default path. It is confined to
programmatic callers passing `run_dir`, and the integration fixture works around it by renaming the
produced directory. The convention is therefore load-bearing but only enforced by habit. The two
candidate fixes — have `run_pipeline` derive its slug from an explicit `run_dir.name`, or have
`run_stage` read the slug from an artifact rather than the path — both change a capital-path
function, so neither is taken here; this paragraph records the constraint until that decision is
made deliberately.

Plan-only runs are [ADR 0035](0035-plan-only-runs-pause-before-execution.md); the stage runner is
the granular counterpart to that mode's whole-chain pause.
