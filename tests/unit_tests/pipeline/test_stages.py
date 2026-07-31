"""Unit tests for the single-stage runner: its refusal order, its declared prerequisites, and its dependency isolation.

The operator-facing contract of `run_stage` is that it refuses *before* it does anything. Both refusals are
pinned by type, and the "before" half is pinned observably: `determination` is a node that writes
`determination_verdict.json` on every path it takes, orchestration failure included, so a missing prerequisite
leaving no verdict on disk is direct evidence the node was never invoked. The paired control runs the same
stage against a run directory that satisfies the prerequisite and asserts the verdict *is* written, so the
absence assertion cannot pass for the wrong reason.

The dependency-isolation property of ADR 0036 is pinned as a standing test rather than a manual subprocess
experiment: every credential source is scrubbed (OPENAI_API_KEY and the MONEY_PIT__ secrets from the
environment, the OS keyring replaced by a real empty in-memory backend) and `validator` and `determination`
must still build. Its negative control builds the credential-needing stages in the same scrubbed environment
and asserts each fails with its own typed credential error, so the positive half cannot pass vacuously.

The Stage/STAGE_REGISTRY drift guard and the hand-maintained prerequisite table both key off Stage itself, so
a new member added without a definition or without a declared prerequisite fails a test rather than raising a
KeyError in front of an operator.
"""

from pathlib import Path

import openai
import pytest
from pytest import FixtureRequest
from pytest import MonkeyPatch

from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import AGGREGATED_SIGNALS_JSON_FILENAME
from money_pit.constants import DETERMINATION_VERDICT_JSON_FILENAME
from money_pit.constants import INITIAL_ANSWERS_JSON_FILENAME
from money_pit.constants import INITIAL_QUESTIONS_JSON_FILENAME
from money_pit.constants import PORTFOLIO_SNAPSHOT_FILENAME
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.graph.state import PipelineState
from money_pit.pipeline.stages import _MISSING_INPUT_FROM_RUN_SETUP
from money_pit.pipeline.stages import _MISSING_INPUT_FROM_STAGE
from money_pit.pipeline.stages import STAGE_REGISTRY
from money_pit.pipeline.stages import Stage
from money_pit.pipeline.stages import StageDefinition
from money_pit.pipeline.stages import StageInput
from money_pit.pipeline.stages import StagePrerequisiteError
from money_pit.pipeline.stages import StageRunDirectoryError
from money_pit.pipeline.stages import run_stage
from money_pit.schemas.determination import DeterminationVerdict
from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ValidationStep
from tests.unit_tests.conftest import InMemoryKeyring


_SLUG: str = "2026-07-26_09-00-00"


@pytest.fixture
def config(request: FixtureRequest) -> Config:
    """Return a Config carrying only keyring *lookup keys* — no secret of any kind lives on it."""
    return getattr(
        request,
        "param",
        Config(alpaca_service="alpaca-paper", alpaca_username="alpaca-api-key", alpaca_paper=True),
    )


@pytest.fixture
def run_dir(request: FixtureRequest, tmp_path: Path) -> Path:
    """Return an existing, empty run directory named the way run_pipeline names one, since the slug is the name."""
    path: Path = getattr(request, "param", tmp_path / _SLUG)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_run_stage_with_absent_run_dir(config: Config, tmp_path: Path) -> None:
    with pytest.raises(StageRunDirectoryError):
        _ = run_stage(Stage.DETERMINATION, tmp_path / "never-created", config)


def test_run_stage_with_run_dir_that_is_a_file(config: Config, tmp_path: Path) -> None:
    """A path that exists but is a file is refused distinctly from an absent one, because it is a likelier typo."""
    not_a_directory: Path = tmp_path / f"{_SLUG}.json"
    _ = not_a_directory.write_text("{}", encoding="utf-8")

    with pytest.raises(StageRunDirectoryError):
        _ = run_stage(Stage.DETERMINATION, not_a_directory, config)


_EXPECTED_PREREQUISITES: dict[Stage, tuple[tuple[str, Stage | None], ...]] = {
    Stage.SNAPSHOT: (),
    Stage.AGGREGATOR: ((SIGNALS_DIRNAME, None),),
    Stage.QUESTIONS: (
        (AGGREGATED_SIGNALS_JSON_FILENAME, Stage.AGGREGATOR),
        (PORTFOLIO_SNAPSHOT_FILENAME, Stage.SNAPSHOT),
    ),
    Stage.RETRIEVAL: (
        (INITIAL_QUESTIONS_JSON_FILENAME, Stage.QUESTIONS),
        (AGGREGATED_SIGNALS_JSON_FILENAME, Stage.AGGREGATOR),
    ),
    Stage.ANALYSIS: (
        (AGGREGATED_SIGNALS_JSON_FILENAME, Stage.AGGREGATOR),
        (PORTFOLIO_SNAPSHOT_FILENAME, Stage.SNAPSHOT),
        (INITIAL_ANSWERS_JSON_FILENAME, Stage.RETRIEVAL),
    ),
    Stage.VALIDATOR: ((ACTION_STEPS_JSON_FILENAME, Stage.ANALYSIS),),
    Stage.DETERMINATION: ((ACTION_STEPS_VALIDATION_JSON_FILENAME, Stage.VALIDATOR),),
}

_STAGES_WITH_PREREQUISITES: list[Stage] = [stage for stage, inputs in _EXPECTED_PREREQUISITES.items() if inputs]


def _expected_absence_phrase(relative_path: str, producer: Stage | None) -> str:
    if producer is None:
        return _MISSING_INPUT_FROM_RUN_SETUP.format(path=relative_path)
    return _MISSING_INPUT_FROM_STAGE.format(path=relative_path, producer=producer.value)


def test_stage_registry_covers_every_stage() -> None:
    """A Stage added without a definition must fail here, not as a KeyError in front of an operator."""
    assert set(STAGE_REGISTRY) == set(Stage)


def test_every_stage_is_covered_by_the_prerequisite_table() -> None:
    assert set(_EXPECTED_PREREQUISITES) == set(Stage)


@pytest.mark.parametrize("stage", [pytest.param(stage, id=stage.value) for stage in _STAGES_WITH_PREREQUISITES])
def test_run_stage_with_missing_prerequisites(stage: Stage, run_dir: Path, config: Config) -> None:
    """Every absent input is named in one message, alongside the stage an operator must run to produce it."""
    with pytest.raises(StagePrerequisiteError) as exc_info:
        _ = run_stage(stage, run_dir, config)

    message: str = str(exc_info.value)
    for relative_path, producer in _EXPECTED_PREREQUISITES[stage]:
        assert _expected_absence_phrase(relative_path, producer) in message


@pytest.mark.parametrize("stage", [pytest.param(stage, id=stage.value) for stage in _STAGES_WITH_PREREQUISITES])
def test_run_stage_with_missing_prerequisites_names_the_stage(stage: Stage, run_dir: Path, config: Config) -> None:
    with pytest.raises(StagePrerequisiteError) as exc_info:
        _ = run_stage(stage, run_dir, config)

    assert stage.value in str(exc_info.value)


@pytest.fixture
def empty_signals_dir(run_dir: Path) -> Path:
    """Create the signals/ directory with no signal in it — the bend in the directory-input abstraction."""
    signals_dir: Path = run_dir / SIGNALS_DIRNAME
    signals_dir.mkdir()
    return signals_dir


def test_run_stage_with_empty_signals_directory(run_dir: Path, empty_signals_dir: Path, config: Config) -> None:
    """An existing but empty signals/ is refused: the aggregator reads it by glob, so it is not a usable input."""
    with pytest.raises(StagePrerequisiteError) as exc_info:
        _ = run_stage(Stage.AGGREGATOR, run_dir, config)

    assert _expected_absence_phrase(SIGNALS_DIRNAME, None) in str(exc_info.value)


@pytest.fixture
def signals_input() -> StageInput:
    return StageInput(relative_path=Path(SIGNALS_DIRNAME), produced_by=None)


def test_stage_input_is_present_in_with_empty_directory(signals_input: StageInput, run_dir: Path) -> None:
    (run_dir / SIGNALS_DIRNAME).mkdir()

    assert not signals_input.is_present_in(run_dir)


def test_stage_input_is_present_in_with_directory_holding_a_json_file(signals_input: StageInput, run_dir: Path) -> None:
    signals_dir: Path = run_dir / SIGNALS_DIRNAME
    signals_dir.mkdir()
    _ = (signals_dir / "a_signal.json").write_text("{}", encoding="utf-8")

    assert signals_input.is_present_in(run_dir)


def test_stage_input_is_present_in_with_directory_holding_only_non_json(
    signals_input: StageInput, run_dir: Path
) -> None:
    """A directory of non-JSON leftovers is as unreadable to a glob-reading stage as an empty one."""
    signals_dir: Path = run_dir / SIGNALS_DIRNAME
    signals_dir.mkdir()
    _ = (signals_dir / "notes.md").write_text("nothing to aggregate", encoding="utf-8")

    assert not signals_input.is_present_in(run_dir)


def test_stage_input_is_present_in_with_absent_path(signals_input: StageInput, run_dir: Path) -> None:
    assert not signals_input.is_present_in(run_dir)


@pytest.fixture
def action_steps_input() -> StageInput:
    return StageInput(relative_path=Path(ACTION_STEPS_JSON_FILENAME), produced_by=Stage.ANALYSIS)


def test_stage_input_is_present_in_with_file(action_steps_input: StageInput, run_dir: Path) -> None:
    _ = (run_dir / ACTION_STEPS_JSON_FILENAME).write_text("[]", encoding="utf-8")

    assert action_steps_input.is_present_in(run_dir)


def test_stage_input_describe_absence_with_a_producing_stage(action_steps_input: StageInput) -> None:
    assert action_steps_input.describe_absence() == _MISSING_INPUT_FROM_STAGE.format(
        path=Path(ACTION_STEPS_JSON_FILENAME), producer=Stage.ANALYSIS.value
    )


def test_stage_input_describe_absence_without_a_producing_stage(signals_input: StageInput) -> None:
    """signals/ is placed by whoever creates the run, so its absence must not send an operator to a stage."""
    assert signals_input.describe_absence() == _MISSING_INPUT_FROM_RUN_SETUP.format(path=Path(SIGNALS_DIRNAME))


@pytest.fixture
def validation_step(request: FixtureRequest) -> ValidationStep:
    return getattr(
        request,
        "param",
        ValidationStep(
            step_id="step-1",
            status=ValidationStatus.MATCHED,
            tool_sequence=None,
            compensation_sequence=None,
            gap_description=None,
        ),
    )


@pytest.fixture
def action_steps_validation(request: FixtureRequest, run_dir: Path, validation_step: ValidationStep) -> Path:
    """Write the determination stage's one prerequisite, so the node has something to recompute a verdict from."""
    validation: ActionStepsValidation = getattr(
        request,
        "param",
        ActionStepsValidation(
            slug=run_dir.name, overall_status=OverallValidationStatus.VALIDATED, steps=[validation_step]
        ),
    )
    path: Path = run_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME
    _ = path.write_text(validation.model_dump_json(indent=2), encoding="utf-8")
    return path


def _persisted_verdict(run_dir: Path) -> DeterminationVerdict:
    return DeterminationVerdict.model_validate_json(
        (run_dir / DETERMINATION_VERDICT_JSON_FILENAME).read_text(encoding="utf-8")
    )


def test_run_stage_with_satisfied_prerequisites_invokes_the_node(
    run_dir: Path, action_steps_validation: Path, config: Config
) -> None:
    """The control for the refusal test below: with its input present, determination runs and leaves a verdict."""
    result: PipelineState = run_stage(Stage.DETERMINATION, run_dir, config)

    assert result["determination"] == Determination.PROCEED
    assert _persisted_verdict(run_dir).determination == Determination.PROCEED


def test_run_stage_with_missing_prerequisites_does_not_invoke_the_node(run_dir: Path, config: Config) -> None:
    """The refusal happens before the node runs, evidenced by the artifact the node would have written.

    The determination node writes determination_verdict.json on every path it can take — including the
    fail-closed HALT it would take on exactly this missing artifact — so an untouched run directory is
    the observable difference between "refused" and "ran and failed closed".
    """
    with pytest.raises(StagePrerequisiteError):
        _ = run_stage(Stage.DETERMINATION, run_dir, config)

    assert not (run_dir / DETERMINATION_VERDICT_JSON_FILENAME).exists()
    assert list(run_dir.iterdir()) == []


def test_run_stage_takes_the_slug_from_the_run_directory_name(
    run_dir: Path, action_steps_validation: Path, config: Config
) -> None:
    """A standalone stage has no run record to read a slug from, so the directory name is the run's identity."""
    _ = run_stage(Stage.DETERMINATION, run_dir, config)

    assert _persisted_verdict(run_dir).slug == run_dir.name


_EXCLUDED_STAGE_VALUES: list[str] = ["recovery", "notification", "execution"]


@pytest.mark.parametrize("excluded", _EXCLUDED_STAGE_VALUES)
def test_stage_excludes_the_capital_moving_and_owner_contacting_nodes(excluded: str) -> None:
    """Recovery and notification email the owner and execution places orders (ADR 0036).

    Their absence from Stage is the safety property that keeps them reachable only through a full run, where
    the determination gate decides whether they run at all. Adding one here must be a deliberate act that
    overrides this test, not an oversight that makes a one-line registry entry look harmless.
    """
    assert excluded not in {stage.value for stage in Stage}
    assert excluded.upper() not in Stage.__members__


_OPENAI_API_KEY_ENV: str = "OPENAI_API_KEY"
_SECRET_ENV_VARS: list[str] = [
    _OPENAI_API_KEY_ENV,
    "MONEY_PIT__FRED_API_KEY",
    "MONEY_PIT__BRAVE_API_KEY",
    "MONEY_PIT__ALPACA_SERVICE",
    "MONEY_PIT__ALPACA_USERNAME",
]


@pytest.fixture
def scrubbed_credentials(monkeypatch: MonkeyPatch, in_memory_keyring: InMemoryKeyring) -> None:
    """Remove every credential source a stage builder can read: the secret env vars and the OS keyring's contents.

    in_memory_keyring is a real, empty KeyringBackend rather than a fake of the credential resolver, so the
    Alpaca path fails the way it would on a machine where nothing has been stored.
    """
    for name in _SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


_CREDENTIAL_FREE_STAGES: list[Stage] = [Stage.VALIDATOR, Stage.DETERMINATION]


@pytest.mark.parametrize("stage", [pytest.param(stage, id=stage.value) for stage in _CREDENTIAL_FREE_STAGES])
def test_stage_definition_build_node_with_no_credentials_at_all(
    stage: Stage, config: Config, scrubbed_credentials: None
) -> None:
    """ADR 0036's whole point: the two verdict-bearing stages are re-runnable on a machine holding no secrets."""
    definition: StageDefinition = STAGE_REGISTRY[stage]

    assert callable(definition.build_node(config))


_CREDENTIAL_NEEDING_STAGES: list[tuple[Stage, type[Exception]]] = [
    (Stage.SNAPSHOT, CredentialResolutionError),
    (Stage.QUESTIONS, openai.OpenAIError),
    (Stage.RETRIEVAL, openai.OpenAIError),
    (Stage.ANALYSIS, openai.OpenAIError),
]


@pytest.mark.parametrize(
    ("stage", "expected_error"),
    [pytest.param(stage, expected_error, id=stage.value) for stage, expected_error in _CREDENTIAL_NEEDING_STAGES],
)
def test_stage_definition_build_node_with_no_credentials_for_a_credential_needing_stage(
    stage: Stage, expected_error: type[Exception], config: Config, scrubbed_credentials: None
) -> None:
    """The control that makes the isolation test above mean something: the same scrubbed environment fails here.

    Each stage fails with its own credential error at *build* time, so a stage whose dependencies cannot be
    resolved never reaches a run directory.
    """
    definition: StageDefinition = STAGE_REGISTRY[stage]

    with pytest.raises(expected_error):
        _ = definition.build_node(config)
