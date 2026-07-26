"""Opt-in live plan-only run: the full production pipeline pauses before execution so an operator can inspect it.

Marked `live` (deselected by default via addopts `-m 'not live'`) and additionally skipped unless
MONEY_PIT_LIVE=1, so a bare `-m live` run without an operator opt-in skips cleanly rather than moving capital.
Reuses the tier's live_config/live_credentials fixtures from this directory's conftest.py.

Emails are deliberately not suppressed: this tier runs at full production fidelity. Owner mail is reachable
pre-execution by two routes only — the notification node on the ANALYSIS_HALT / VALIDATION_ERROR paths, and the
recovery node, which sends directly rather than routing through the notification node, on HALT and
PROCEED_WITH_NOTICE. A recovery HALT is precisely the case the early-halt assertions below guard against.
"""

import datetime
import os
from pathlib import Path

import pytest

from money_pit.adapters.text import TextAdapter
from money_pit.adapters.text_llm import TextPayload
from money_pit.adapters.text_llm import make_text_llm_agent
from money_pit.adapters.text_source import build_text_payload
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import EXECUTION_JOURNAL_FILENAME
from money_pit.constants import source_id_to_dirname
from money_pit.graph.state import PipelineState
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.orchestration import production_deps
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.schemas.signals import SignalSet


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE") != "1",
        reason="live tier is opt-in; set MONEY_PIT_LIVE=1 (with real Alpaca paper / Gmail creds) to run",
    ),
]

_PLAN_ONLY_RUN_DIR_MESSAGE: str = "live plan-only run artifacts written to {run_dir}"

_DETERMINATION_STEP: str = "determination"

_EARLY_HALT_MESSAGE: str = (
    "the live run never reached {step}, so plan-only mode was never exercised; "
    "terminal_state={terminal_state}, recovery_decision={recovery_decision}, completed_steps={completed_steps}"
)


def test_plan_only_live_run_plans_then_places_no_order(
    live_credentials: AlpacaCredentials,
    live_config: Config,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Run a minimal thesis through the real production deps stopping after determination, and pin that no order ran.

    The absence of an execution journal is only meaningful once the run has demonstrably produced a plan: a
    recovery HALT against a real prior run, a NO_ACTION terminal, or an ANALYSIS_HALT would all leave the journal
    absent without ever reaching the pause. The determination step and action_steps.json are pinned first so an
    early halt reports itself rather than passing vacuously.

    The run dir is left at its real DAILY_SHOW_ROOT location (no run_dir override) so the operator can inspect
    the planning artifacts afterwards; its path is written straight to the terminal because pytest captures
    ordinary output and this tier registers no loguru file sink (only the CLI entrypoint calls
    configure_file_logging), so a logged path would be invisible on a passing run.
    """
    assert live_credentials.paper, "refusing to run a live thesis: credentials are not paper-routed"

    retrieved_at: str = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    payload: TextPayload = build_text_payload(
        "NVDA remains well-positioned for continued AI infrastructure demand.",
        slug="2026-01-01_00-00-00",
        title="live-tier plan-only smoke",
        retrieved_at=retrieved_at,
    )

    agent = make_text_llm_agent(live_config)
    signal_set: SignalSet = TextAdapter(agent=agent, cache_dir=tmp_path).process(payload)

    signals_dir: Path = tmp_path / "signals"
    signals_dir.mkdir()
    signal_path: Path = signals_dir / f"{source_id_to_dirname(signal_set.source_ref.source_id)}.json"
    _ = signal_path.write_text(signal_set.model_dump_json(indent=2), encoding="utf-8")

    state: PipelineState = run_pipeline(
        signals_dir, overrides=production_deps(live_config), through=Stage.DETERMINATION
    )

    working_dir: str | None = state.get("working_dir")
    assert working_dir is not None
    run_dir: Path = Path(working_dir)
    with capsys.disabled():
        print(_PLAN_ONLY_RUN_DIR_MESSAGE.format(run_dir=run_dir))

    assert run_dir.is_dir()

    early_halt_detail: str = _EARLY_HALT_MESSAGE.format(
        step=_DETERMINATION_STEP,
        terminal_state=state.get("terminal_state"),
        recovery_decision=state.get("recovery_decision"),
        completed_steps=state.get("completed_steps"),
    )
    assert _DETERMINATION_STEP in (state.get("completed_steps") or []), early_halt_detail
    assert (run_dir / ACTION_STEPS_JSON_FILENAME).is_file(), early_halt_detail

    assert not (run_dir / EXECUTION_JOURNAL_FILENAME).exists()
