"""Module containing the single-stage runner and its registry of standalone-runnable nodes for the money_pit package."""

from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from money_pit.config import Config
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import AGGREGATED_SIGNALS_JSON_FILENAME
from money_pit.constants import INITIAL_ANSWERS_JSON_FILENAME
from money_pit.constants import INITIAL_QUESTIONS_JSON_FILENAME
from money_pit.constants import PORTFOLIO_SNAPSHOT_FILENAME
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.pipeline.aggregator import make_aggregator_node
from money_pit.pipeline.analysis import make_analysis_node
from money_pit.pipeline.determination import make_determination_node
from money_pit.pipeline.orchestration import PipelineOverrides
from money_pit.pipeline.orchestration import answer_synthesis_agent_or_default
from money_pit.pipeline.orchestration import claim_questions_agent_or_default
from money_pit.pipeline.orchestration import corroboration_agent_or_default
from money_pit.pipeline.orchestration import deterministic_research_tools_or_default
from money_pit.pipeline.orchestration import instrument_facts_resolver_or_default
from money_pit.pipeline.orchestration import portfolio_fetcher_or_default
from money_pit.pipeline.orchestration import thesis_agent_or_default
from money_pit.pipeline.questions import make_questions_node
from money_pit.pipeline.retrieval import make_retrieval_node
from money_pit.pipeline.snapshot import make_snapshot_node
from money_pit.pipeline.validator import make_validator_node


_JSON_GLOB: str = "*.json"

_RUN_DIR_ABSENT_MESSAGE: str = "Run directory {run_dir} does not exist; a stage runs only against an existing run."
_RUN_DIR_NOT_A_DIRECTORY_MESSAGE: str = "Run directory {run_dir} is not a directory."
_MISSING_INPUTS_MESSAGE: str = "Stage '{stage}' cannot run against {run_dir}: {missing}."
_MISSING_INPUT_FROM_STAGE: str = "{path} is missing (run stage '{producer}' first)"
_MISSING_INPUT_FROM_RUN_SETUP: str = "{path} is missing (supplied when the run directory is created)"


class StageError(Exception):
    """Base class for the reasons a single-stage run is refused before its node is invoked."""


class StageRunDirectoryError(StageError):
    """Raised when the target run directory does not exist or is not a directory."""


class StagePrerequisiteError(StageError):
    """Raised when the run directory lacks an input artifact the stage reads."""


class Stage(str, Enum):
    """A pipeline node that may be run standalone against an existing run directory.

    The set is deliberately confined to nodes that neither move capital nor contact the
    owner: `recovery` and `notification` send email and `execution` places orders, so
    those stay reachable only through a full run (see ADR 0036).
    """

    SNAPSHOT = "snapshot"
    AGGREGATOR = "aggregator"
    QUESTIONS = "questions"
    RETRIEVAL = "retrieval"
    ANALYSIS = "analysis"
    VALIDATOR = "validator"
    DETERMINATION = "determination"


@dataclass(frozen=True)
class StageInput:
    """A run-directory artifact a stage reads, named alongside whatever produces it.

    A directory input counts as present only when it holds at least one JSON file, because
    the stages that read a directory read it by glob.
    """

    relative_path: Path
    produced_by: Stage | None

    def is_present_in(self, run_dir: Path) -> bool:
        """Return whether this input is readable in run_dir."""
        path: Path = run_dir / self.relative_path
        if path.is_dir():
            return any(path.glob(_JSON_GLOB))
        return path.is_file()

    def describe_absence(self) -> str:
        """Return the operator-facing phrase naming this input and what produces it."""
        if self.produced_by is None:
            return _MISSING_INPUT_FROM_RUN_SETUP.format(path=self.relative_path)
        return _MISSING_INPUT_FROM_STAGE.format(path=self.relative_path, producer=self.produced_by.value)


@dataclass(frozen=True)
class StageDefinition:
    """How to construct one stage's node and what that node needs to already be on disk."""

    build_node: Callable[[Config], PipelineNode]
    inputs: tuple[StageInput, ...]


def _no_overrides() -> PipelineOverrides:
    """Return the empty override set every stage run composes from, so defaults match a production run."""
    return PipelineOverrides()


def _build_snapshot_node(config: Config) -> PipelineNode:
    """Return the snapshot node wired to a read-only Alpaca portfolio fetcher and nothing else."""
    return make_snapshot_node(fetch_portfolio=portfolio_fetcher_or_default(_no_overrides(), config))


def _build_aggregator_node(config: Config) -> PipelineNode:
    """Return the aggregator node wired to the corroboration agent."""
    _ = config
    return make_aggregator_node(corroboration_agent=corroboration_agent_or_default(_no_overrides()))


def _build_questions_node(config: Config) -> PipelineNode:
    """Return the questions node wired to the claim-questions agent."""
    return make_questions_node(
        claim_questions_agent=claim_questions_agent_or_default(_no_overrides(), config),
        current_events_lookback_days=config.current_events_lookback_days,
    )


def _build_retrieval_node(config: Config) -> PipelineNode:
    """Return the retrieval node wired to the answer-synthesis agent and the deterministic research tools."""
    overrides: PipelineOverrides = _no_overrides()
    return make_retrieval_node(
        answer_synthesis_agent=answer_synthesis_agent_or_default(overrides, config),
        deterministic_tools=deterministic_research_tools_or_default(overrides, config),
    )


def _build_analysis_node(config: Config) -> PipelineNode:
    """Return the analysis node wired to the thesis agent and the instrument-facts resolver."""
    overrides: PipelineOverrides = _no_overrides()
    return make_analysis_node(
        config=config,
        thesis_agent=thesis_agent_or_default(overrides, config),
        resolve_instrument_facts=instrument_facts_resolver_or_default(overrides),
    )


def _build_validator_node(config: Config) -> PipelineNode:
    """Return the validator node over the pinned tool manifest, which needs no credentials."""
    _ = config
    return make_validator_node()


def _build_determination_node(config: Config) -> PipelineNode:
    """Return the determination node, which needs no dependencies at all."""
    _ = config
    return make_determination_node()


_SIGNALS_INPUT: StageInput = StageInput(relative_path=Path(SIGNALS_DIRNAME), produced_by=None)
_PORTFOLIO_SNAPSHOT_INPUT: StageInput = StageInput(
    relative_path=Path(PORTFOLIO_SNAPSHOT_FILENAME), produced_by=Stage.SNAPSHOT
)
_AGGREGATED_SIGNALS_INPUT: StageInput = StageInput(
    relative_path=Path(AGGREGATED_SIGNALS_JSON_FILENAME), produced_by=Stage.AGGREGATOR
)
_INITIAL_QUESTIONS_INPUT: StageInput = StageInput(
    relative_path=Path(INITIAL_QUESTIONS_JSON_FILENAME), produced_by=Stage.QUESTIONS
)
_INITIAL_ANSWERS_INPUT: StageInput = StageInput(
    relative_path=Path(INITIAL_ANSWERS_JSON_FILENAME), produced_by=Stage.RETRIEVAL
)
_ACTION_STEPS_INPUT: StageInput = StageInput(relative_path=Path(ACTION_STEPS_JSON_FILENAME), produced_by=Stage.ANALYSIS)
_ACTION_STEPS_VALIDATION_INPUT: StageInput = StageInput(
    relative_path=Path(ACTION_STEPS_VALIDATION_JSON_FILENAME), produced_by=Stage.VALIDATOR
)


STAGE_REGISTRY: Mapping[Stage, StageDefinition] = {
    Stage.SNAPSHOT: StageDefinition(build_node=_build_snapshot_node, inputs=()),
    Stage.AGGREGATOR: StageDefinition(build_node=_build_aggregator_node, inputs=(_SIGNALS_INPUT,)),
    Stage.QUESTIONS: StageDefinition(
        build_node=_build_questions_node,
        inputs=(_AGGREGATED_SIGNALS_INPUT, _PORTFOLIO_SNAPSHOT_INPUT),
    ),
    Stage.RETRIEVAL: StageDefinition(
        build_node=_build_retrieval_node,
        inputs=(_INITIAL_QUESTIONS_INPUT, _AGGREGATED_SIGNALS_INPUT),
    ),
    Stage.ANALYSIS: StageDefinition(
        build_node=_build_analysis_node,
        inputs=(_AGGREGATED_SIGNALS_INPUT, _PORTFOLIO_SNAPSHOT_INPUT, _INITIAL_ANSWERS_INPUT),
    ),
    Stage.VALIDATOR: StageDefinition(build_node=_build_validator_node, inputs=(_ACTION_STEPS_INPUT,)),
    Stage.DETERMINATION: StageDefinition(
        build_node=_build_determination_node, inputs=(_ACTION_STEPS_VALIDATION_INPUT,)
    ),
}


def _require_run_directory(run_dir: Path) -> None:
    """Raise StageRunDirectoryError unless run_dir is an existing directory."""
    if not run_dir.exists():
        raise StageRunDirectoryError(_RUN_DIR_ABSENT_MESSAGE.format(run_dir=run_dir))
    if not run_dir.is_dir():
        raise StageRunDirectoryError(_RUN_DIR_NOT_A_DIRECTORY_MESSAGE.format(run_dir=run_dir))


def _require_inputs(stage: Stage, inputs: tuple[StageInput, ...], run_dir: Path) -> None:
    """Raise StagePrerequisiteError naming every absent input and the stage that produces it."""
    missing: list[StageInput] = [stage_input for stage_input in inputs if not stage_input.is_present_in(run_dir)]
    if not missing:
        return
    raise StagePrerequisiteError(
        _MISSING_INPUTS_MESSAGE.format(
            stage=stage.value,
            run_dir=run_dir,
            missing="; ".join(stage_input.describe_absence() for stage_input in missing),
        )
    )


def run_stage(stage: Stage, run_dir: Path, config: Config) -> PipelineState:
    """Run one pipeline node against an existing run directory, raising StageRunDirectoryError when run_dir is unusable and StagePrerequisiteError when an input artifact is absent.

    Only the named stage's dependencies are constructed, so `validator` and `determination`
    run without credentials and `snapshot` needs only a read-only Alpaca client (ADR 0036).
    The run's slug is the directory name, which is the convention `run_pipeline` writes and
    the staleness guard of ADR 0037 checks against. No graph, checkpointer or routing is
    involved: the node is invoked directly and its partial state update returned.
    """
    definition: StageDefinition = STAGE_REGISTRY[stage]
    _require_run_directory(run_dir)
    _require_inputs(stage, definition.inputs, run_dir)
    node: PipelineNode = definition.build_node(config)
    initial_state: PipelineState = {
        "slug": run_dir.name,
        "working_dir": str(run_dir),
        "completed_steps": [],
    }
    return node(initial_state)
