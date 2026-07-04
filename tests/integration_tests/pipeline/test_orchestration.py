"""Integration tests for run_pipeline's fail-closed contract on capital-critical dependencies."""

from pathlib import Path

import pytest

from money_pit.pipeline.orchestration import MissingPipelineDependencyError
from money_pit.pipeline.orchestration import phase4_overrides
from money_pit.pipeline.orchestration import run_pipeline


_CAPITAL_CRITICAL_DEPS: list[str] = ["fetch_portfolio", "place_order", "send_email"]


@pytest.mark.parametrize("missing_dep", _CAPITAL_CRITICAL_DEPS)
def test_run_pipeline_with_missing_capital_dep(tmp_path: Path, pipeline_signals_dir: Path, missing_dep: str) -> None:
    overrides = phase4_overrides()
    setattr(overrides, missing_dep, None)
    with pytest.raises(MissingPipelineDependencyError):
        _ = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=tmp_path / "run", overrides=overrides)


def test_run_pipeline_with_no_overrides(tmp_path: Path, pipeline_signals_dir: Path) -> None:
    with pytest.raises(MissingPipelineDependencyError):
        _ = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=tmp_path / "run")


def test_run_pipeline_with_full_phase4_overrides(tmp_path: Path, pipeline_signals_dir: Path) -> None:
    final_state = run_pipeline(signals_dir=pipeline_signals_dir, run_dir=tmp_path / "run", overrides=phase4_overrides())
    assert isinstance(final_state, dict)
    assert "slug" in final_state
    assert "completed_steps" in final_state
