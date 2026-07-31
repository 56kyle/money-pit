"""Integration tests pinning that a pipeline node is individually executable against a run directory a full run left behind.

The seam under test spans the whole stage runner: a real run directory produced by run_pipeline, the
production wiring STAGE_REGISTRY builds (not the phase4 stubs the run itself used), and the node's own
read-modify-write against that directory. Nothing here is stubbed at the stage-runner boundary; that is the
point, since the operator-facing claim is that a stage re-run does what the graph did.

Only `validator` and `determination` are exercised, because the stage runner uses production wiring: those two
are the pair that needs no credentials and no network. The oracle for each is the artifact the full run
already wrote — it is read, deleted, and expected back byte-for-model after the standalone re-run, so the test
asserts equivalence with the in-graph node rather than a hand-written expectation that could drift from it.

run_pipeline derives the slug from the clock and names the working directory only when it picks one itself,
so a run given an explicit run_dir has a directory name that is not its slug. run_stage reads the slug from
the directory name, so the template directory is renamed to its slug before any stage runs against it.
"""

import shutil
from pathlib import Path

import pytest
from pytest import TempPathFactory

from money_pit.config import Config
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME
from money_pit.graph.state import PipelineState
from money_pit.mcp.manifest import pinned_manifest
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.pipeline.orchestration import phase4_overrides
from money_pit.pipeline.orchestration import run_pipeline
from money_pit.pipeline.stages import Stage
from money_pit.pipeline.stages import run_stage
from money_pit.schemas.determination import DeterminationVerdict
from money_pit.schemas.validation_results import ActionStepsValidation


@pytest.fixture(scope="session")
def stage_config() -> Config:
    """Return the Config a stage run is given: keyring lookup keys only, since no stage here resolves a secret."""
    return Config(alpaca_service="alpaca-paper", alpaca_username="alpaca-api-key", alpaca_paper=True)


@pytest.fixture(scope="session")
def completed_run_template(tmp_path_factory: TempPathFactory, pipeline_signals_dir: Path) -> Path:
    """Run the full pipeline once and return its run directory, renamed to the run's slug.

    The rename is what lets a stage run against it at all: run_stage takes the slug from the directory name,
    which matches run_pipeline's own naming only when run_pipeline chooses the directory.
    """
    overrides: PipelineOverrides = phase4_overrides()
    overrides.manifest = pinned_manifest()
    root: Path = tmp_path_factory.mktemp("completed_run")

    final_state: PipelineState = run_pipeline(
        signals_dir=pipeline_signals_dir, run_dir=root / "run", overrides=overrides
    )

    slug_named: Path = root / final_state["slug"]
    (root / "run").rename(slug_named)
    return slug_named


@pytest.fixture
def run_dir(completed_run_template: Path, tmp_path: Path) -> Path:
    """Return a private copy of the completed run, so each stage re-run mutates a directory it owns."""
    destination: Path = tmp_path / completed_run_template.name
    _ = shutil.copytree(completed_run_template, destination)
    return destination


def test_run_stage_with_validator_reproduces_the_in_graph_validation(run_dir: Path, stage_config: Config) -> None:
    """Re-running validator standalone rewrites action_steps_validation.json to what the graph's validator wrote."""
    artifact: Path = run_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME
    from_full_run: ActionStepsValidation = ActionStepsValidation.model_validate_json(
        artifact.read_text(encoding="utf-8")
    )
    artifact.unlink()

    _ = run_stage(Stage.VALIDATOR, run_dir, stage_config)

    assert ActionStepsValidation.model_validate_json(artifact.read_text(encoding="utf-8")) == from_full_run


def test_run_stage_with_determination_reproduces_the_in_graph_verdict(run_dir: Path, stage_config: Config) -> None:
    """Re-running determination standalone rewrites determination_verdict.json to the graph's go/no-go."""
    artifact: Path = run_dir / DETERMINATION_VERDICT_JSON_FILENAME
    from_full_run: DeterminationVerdict = DeterminationVerdict.model_validate_json(artifact.read_text(encoding="utf-8"))
    artifact.unlink()

    _ = run_stage(Stage.DETERMINATION, run_dir, stage_config)

    assert DeterminationVerdict.model_validate_json(artifact.read_text(encoding="utf-8")) == from_full_run


def test_run_stage_with_determination_returns_the_graphs_determination(run_dir: Path, stage_config: Config) -> None:
    """The returned partial state carries the same verdict as the artifact, so a caller need not re-read disk."""
    from_full_run: DeterminationVerdict = DeterminationVerdict.model_validate_json(
        (run_dir / DETERMINATION_VERDICT_JSON_FILENAME).read_text(encoding="utf-8")
    )

    result: PipelineState = run_stage(Stage.DETERMINATION, run_dir, stage_config)

    assert result["determination"] == from_full_run.determination
    assert result["completed_steps"] == ["determination"]


def test_run_stage_with_validator_then_determination_chains_through_the_directory(
    run_dir: Path, stage_config: Config
) -> None:
    """The two stages compose through disk alone: determination consumes what the standalone validator wrote."""
    validation_artifact: Path = run_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME
    verdict_artifact: Path = run_dir / DETERMINATION_VERDICT_JSON_FILENAME
    from_full_run: DeterminationVerdict = DeterminationVerdict.model_validate_json(
        verdict_artifact.read_text(encoding="utf-8")
    )
    validation_artifact.unlink()
    verdict_artifact.unlink()

    _ = run_stage(Stage.VALIDATOR, run_dir, stage_config)
    _ = run_stage(Stage.DETERMINATION, run_dir, stage_config)

    assert DeterminationVerdict.model_validate_json(verdict_artifact.read_text(encoding="utf-8")) == from_full_run
