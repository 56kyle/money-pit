---
status: accepted
date: 2026-07-06
decision-makers: Kyle Oliver
consulted: python-design-questioner (Wave 0 design gate)
informed: python-test-writer, python-reviewer
---

# Import-time I/O removal (lazy accessors) and package-relative agent prompts

## Context and Problem Statement

Importing `money_pit` had disk side effects. `constants.py` called the platformdirs
`user_config_path` / `user_state_path` / `user_log_path` with `ensure_exists=True` at module
import, creating three per-user directories merely because a module was imported. `log.py`
registered a serialized loguru file sink (`logger.add(LOG_PATH, ...)`) at import, opening a
log file as an import side effect. The four LLM factory modules
(`agents/{claim_questions,answer_synthesis,thesis_judgment}.py`, `adapters/video_llm.py`)
read their system prompts at import via a four-level `Path(__file__).parent.parent.parent.parent
/ "data" / "agents" / "agent_N.md"` walk to the repository root.

Two problems follow. First, import-time I/O makes the package hostile to test, tooling, and
any consumer that imports for introspection: importing a constants module should never touch
the filesystem. Second, the repo-root `data/agents/` walk is not wheel-safe — a built/installed
wheel has no repository root, so the prompt files would be absent and the four-level walk would
resolve to a nonexistent path. How should directory creation, log-sink registration, and prompt
loading be deferred off import, and where should the prompt files live so they ship in the wheel?

## Decision Drivers

* Importing `money_pit`, `money_pit.constants`, `money_pit.log`, and the agent/adapter modules
  must not create directories or read prompt files.
* Prompt loading must be wheel-safe — resolvable from an installed distribution, not only an
  editable checkout with a repository root.
* Directory creation should be folded into the accessor that hands out the path, so no consumer
  separately assumes the directory exists.
* Logging is a genuine process-start global side effect; its treatment can legitimately differ
  from the pure per-call laziness applied elsewhere.

## Considered Options

* **Directories:** module-level `Path` constants with import-time creation (status quo) vs.
  `functools.cache`d accessor functions that create on first call vs. plain (uncached) accessor
  functions that compute and create on each call.
* **Log sink:** import-time `logger.add` (status quo) vs. a `@cache`d `_ensure_file_sink()`
  invoked on first structured-log use vs. an explicit `configure_file_logging()` called from
  entrypoints.
* **Prompts:** keep files at repo-root `data/agents/` reached by the parent walk vs. move them
  under `src/money_pit/` and load via `importlib.resources` with a lazy cached reader.

## Decision Outcome

Chosen:

* **Directories → lazy uncached accessors.** `USER_CONFIG_FOLDER` / `USER_STATE_FOLDER` /
  `USER_LOG_FOLDER` / `DEFAULT_CONFIG_PATH` constants become plain functions
  `user_config_folder()` / `user_state_folder()` / `user_log_folder()` / `default_config_path()`.
  Each platformdirs call keeps `ensure_exists=True`, so creation is folded into the accessor and
  happens on every call. `config.load_config`'s signature changed from
  `path: Path = DEFAULT_CONFIG_PATH` to `path: Path | None = None` with
  `resolved_path = path or default_config_path()` in the body — a default argument is evaluated
  at import, so leaving an accessor call there would have reintroduced the import-time I/O.

  These accessors were briefly `@functools.cache`d, but the cache was removed (Wave 5 review):
  it is cosmetic rather than load-bearing. The Paths are deterministic functions of two module
  constants, and platformdirs' `ensure_exists=True` `mkdir` is already idempotent and cheap, so
  recomputing per call costs nothing meaningful. Against that near-zero saving the cache leaks
  process-global state into the test suite — it forced an autouse `cache_clear` fixture around
  every test that touched an accessor, purely to undo memoization the code did not need. Removing
  it deletes that fixture obligation. The `@cache` is retained only where it is genuinely
  load-bearing: `log.configure_file_logging()` (register the sink exactly once per process) and
  `prompt_loader.system_prompt()` (harmless memoization of a deterministic packaged-file read).

* **Log sink → explicit `configure_file_logging()` called from the CLI entrypoint.** `log.py`
  exposes a `@cache`d `log_path()` and a `@cache`d `configure_file_logging()` that registers the
  serialized sink exactly once and returns the path. A typer `@app.callback()` in `__main__.py`
  calls it before any command runs. See "log-sink: explicit configuration over lazy-per-use"
  below for why this diverges from the otherwise-lazy default.

* **Prompts → moved to a neutral package home, loaded via `importlib.resources`, lazily.** The
  four referenced prompt files moved (`git mv`) from `data/agents/agent_{1,2,3,4}.md` to the
  neutral resource subpackage `src/money_pit/prompts/` (a package with an `__init__.py` so it is
  importable for `importlib.resources`). The loader lives at the neutral top level,
  `money_pit.prompt_loader`, not under `agents/`: it is a generic packaged-markdown reader with
  zero agent logic, and homing it under `agents/` was what created the adapters → agents import
  edge (`adapters/video_llm.py` reads the A1 prompt). A single shared
  `prompt_loader.system_prompt(name)` (`@functools.cache`d) reads them via
  `importlib.resources.files("money_pit.prompts") / f"{name}.md"` on first use, raising
  `FileNotFoundError` for an unknown name. The four-level parent walk is deleted; each module
  passes its stem via a `_PROMPT_NAME` constant. `pyproject.toml` gains
  `[tool.setuptools.package-data] "money_pit.prompts" = ["*.md"]` so the wheel ships them.
  `agent_5.md` / `agent_6.md` are referenced only in docs, not code, and stay in `data/agents/`.

This supersedes the repo-root `data/agents/` walk for the code-referenced prompts and matches the
package-relative-resource precedent set for the pinned order schema in ADR 0007 (T6), which loads
`Path(__file__).parent / "alpaca_order_schema.json"` from inside the package.

### Consequences

* Good: importing any `money_pit` module is now free of filesystem side effects; directory
  creation, sink registration, and prompt reads all defer to first genuine use.
* Good: prompts are wheel-safe and resolved through the import system rather than a fragile
  relative walk; a single loader replaces four duplicated path idioms (also closes the Wave 4
  prompt-path duplication item).
* Good: no module in `adapters/` imports from `agents/`. An earlier revision homed the loader at
  `agents/prompt_loader` and accepted the resulting adapters → agents edge as "neutral" because
  the A1 prompt is agent-domain content; Wave 5 review rejected that framing — the loader carries
  no agent logic, so the edge was accidental, not intrinsic. Relocating the loader and prompts to
  the neutral `money_pit.prompt_loader` / `money_pit.prompts` home removes the edge outright.
* Bad / to watch: prompt files are addressed by opaque stems (`agent_1`…`agent_4`) kept to
  preserve the doc cross-references; a rename to role-based names would be clearer but was scoped
  out to avoid churn and stale links.

### Confirmation

Import smoke: `python -c "import money_pit, money_pit.log, money_pit.constants,
money_pit.prompt_loader, money_pit.agents.answer_synthesis, money_pit.adapters.video_llm"` must
complete without creating the per-user directories or reading any prompt file. The existing unit +
pipeline-integration suite stays green. `importlib.resources.files("money_pit.prompts") /
"agent_1.md"` resolves in an editable install; the package-data entry covers the built wheel.

## Pros and Cons of the Options

### Log-sink: explicit configuration over lazy-per-use

Every logging call site uses `from loguru import logger` directly rather than a `money_pit.log`
wrapper, and nothing in the source imports `money_pit.log`. A `@cache`d `_ensure_file_sink()`
"on first structured-log use" therefore has no natural insertion point — it would force every
`logger.<level>(...)` call to route through a wrapper, an invasive change for a single sink.
Registering a file sink is a process-wide, once-per-process global effect that belongs at the
process-start boundary. `configure_file_logging()` called from the `__main__` typer callback
places that effect exactly there, keeps `log.py` import-pure, and stays idempotent via `@cache`.
Wave 0 leaned lazy-over-entrypoint-init generally to avoid ordering hazards; logging is the
documented exception, and there is no ordering hazard here because the callback runs before any
command body and the sink is independent of other state.

## More Information

Related: ADR 0007 (package-relative pinned order schema — the resource precedent), ADR 0010
(config pydantic-settings migration — `load_config` shape), Wave 0 resolutions D1/D2/D3 in the
remediation plan. Touched: `src/money_pit/constants.py`, `src/money_pit/log.py`,
`src/money_pit/config.py`, `src/money_pit/__main__.py`,
`src/money_pit/prompt_loader.py`, `src/money_pit/prompts/__init__.py`,
`src/money_pit/prompts/agent_{1,2,3,4}.md`, the four LLM factory modules, and `pyproject.toml`.
