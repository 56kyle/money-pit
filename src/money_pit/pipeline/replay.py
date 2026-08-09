"""Module replaying exact immutable run artifacts without active capabilities."""

from pathlib import Path
from typing import Protocol

from money_pit.execution_control.node import ExecutionArtifactPayload
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_dir
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.chain import PLANNING_CHAIN
from money_pit.pipeline.chain import Stage
from money_pit.pipeline.discovery import DiscoveryArtifactPayload
from money_pit.pipeline.interpretation import InterpretationArtifactPayload
from money_pit.pipeline.research import ResearchArtifactPayload
from money_pit.pipeline.synthesis import SynthesisArtifactPayload
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import RunTerminalEvent
from money_pit.schemas.runs import RunTerminalStatus
from money_pit.schemas.runs import StageArtifactRecord
from money_pit.schemas.snapshots import PortfolioPlanningArtifactPayload


class ReplayStore(Protocol):
    """Read-only exact run and stage-artifact authority."""

    def get_run(self, run_id: str) -> RunRecord:
        """Return the immutable run record."""
        ...

    def artifacts_for_run(self, run_id: str) -> tuple[StageArtifactRecord, ...]:
        """Return immutable artifacts in stage order."""
        ...

    def terminal_event_for_run(self, run_id: str) -> RunTerminalEvent | None:
        """Return the immutable terminal event, or None for an interrupted run."""
        ...


class ReplayTerminalStateError(Exception):
    """Raised when a run did not terminate successfully and cannot be replayed as complete."""


def make_replay_node(store: ReplayStore, *, through: Stage | None = None) -> PipelineNode:
    """Return a node that validates and projects only recorded artifacts."""

    def replay(state: PipelineState) -> PipelineState:
        return _replay_from_store(store, through, state)

    return replay


def _replay_from_store(
    store: ReplayStore,
    expected_terminal: Stage | None,
    state: PipelineState,
) -> PipelineState:
    run_id = require_run_id(state)
    run = store.get_run(run_id)
    recorded_terminal = Stage(run.through_stage)
    if expected_terminal is not None and recorded_terminal is not expected_terminal:
        raise ValueError("replay terminal stage differs from the immutable run record")
    expected_stages = tuple(stage.value for stage in PLANNING_CHAIN[: PLANNING_CHAIN.index(recorded_terminal) + 1])
    terminal_event = _require_completed_run(store, run, expected_stages, state)
    artifacts = _require_complete_artifacts(store, run_id, expected_stages, terminal_event)
    run_dir = require_run_dir(state)
    update: PipelineState = {
        "completed_stages": expected_stages,
        "artifact_ids": tuple(artifact.artifact_id for artifact in artifacts),
        "replay": True,
        "source_config_hash": run.source_config_hash,
    }
    if run.intelligence_config_hash is not None:
        update["intelligence_config_hash"] = run.intelligence_config_hash
    if run.portfolio_config_hash is not None:
        update["portfolio_config_hash"] = run.portfolio_config_hash
    if run.execution_config_hash is not None:
        update["execution_config_hash"] = run.execution_config_hash
    for artifact in artifacts:
        _require_file_binding(run_dir, artifact)
        _restore_artifact(update, artifact)
        if artifact.decision_at is not None:
            update["decision_at"] = artifact.decision_at
    return update


def _require_completed_run(
    store: ReplayStore,
    run: RunRecord,
    expected_stages: tuple[str, ...],
    state: PipelineState,
) -> RunTerminalEvent:
    """Validate immutable run identity, bounds, and successful termination."""
    terminal_event = store.terminal_event_for_run(run.run_id)
    if terminal_event is None:
        raise ReplayTerminalStateError("Interrupted run has no successful terminal event")
    if terminal_event.status is not RunTerminalStatus.COMPLETED:
        raise ReplayTerminalStateError("Failed run cannot be replayed as a completed decision")
    if run.requested_as_of != require_requested_as_of(state):
        raise ValueError("replay cutoff differs from the immutable run record")
    if run.started_at != require_run_started_at(state):
        raise ValueError("replay start time differs from the immutable run record")
    if run.through_stage != expected_stages[-1]:
        raise ValueError("replay terminal stage differs from the immutable run record")
    return terminal_event


def _require_complete_artifacts(
    store: ReplayStore,
    run_id: str,
    expected_stages: tuple[str, ...],
    terminal_event: RunTerminalEvent,
) -> tuple[StageArtifactRecord, ...]:
    """Return exact ordered artifacts bounded by successful completion."""
    artifacts = tuple(
        artifact for artifact in store.artifacts_for_run(run_id) if artifact.stage in frozenset(expected_stages)
    )
    if tuple(artifact.stage for artifact in artifacts) != expected_stages:
        raise ValueError("replay artifacts are incomplete or out of stage order")
    if any(artifact.known_at > terminal_event.completed_at for artifact in artifacts):
        raise ReplayTerminalStateError("Successful terminal event predates a replay artifact")
    return artifacts


def _restore_artifact(update: PipelineState, artifact: StageArtifactRecord) -> None:
    payload = artifact.payload
    if artifact.stage == Stage.A1.value:
        a1 = InterpretationArtifactPayload.model_validate(payload)
        update["observation_ids"] = a1.observation_ids
        update["evidence_fragment_ids"] = a1.fragment_ids
        update["interpretation_attempt_ids"] = a1.attempt_ids
    elif artifact.stage == Stage.A2.value:
        update["candidate_thesis_ids"] = DiscoveryArtifactPayload.model_validate(payload).candidate_thesis_ids
    elif artifact.stage == Stage.A3.value:
        _restore_research_artifact(update, ResearchArtifactPayload.model_validate(payload))
    elif artifact.stage == Stage.A4.value:
        a4 = SynthesisArtifactPayload.model_validate(payload)
        update["claim_resolution_decision_ids"] = a4.resolution_ids
        update["verification_result_ids"] = a4.verification_ids
        update["thesis_revision_ids"] = a4.thesis_revision_ids
        update["signal_contribution_ids"] = a4.contribution_ids
    elif artifact.stage == Stage.A5.value:
        _restore_portfolio_artifact(
            update,
            PortfolioPlanningArtifactPayload.model_validate(payload),
        )
    elif artifact.stage == Stage.A6.value:
        _restore_execution_artifact(update, ExecutionArtifactPayload.model_validate(payload))


def _restore_research_artifact(update: PipelineState, payload: ResearchArtifactPayload) -> None:
    rounds = tuple(execution for summary in payload.candidates for execution in summary.rounds)
    update["research_contexts"] = payload.contexts
    update["observation_ids"] = (
        *update.get("observation_ids", ()),
        *(identifier for execution in rounds for identifier in execution.observation_ids),
    )
    update["evidence_fragment_ids"] = (
        *update.get("evidence_fragment_ids", ()),
        *(identifier for execution in rounds for identifier in execution.fragment_ids),
    )
    update["interpretation_attempt_ids"] = (
        *update.get("interpretation_attempt_ids", ()),
        *(identifier for execution in rounds for identifier in execution.interpretation_attempt_ids),
    )
    update["research_failure_ids"] = tuple(identifier for execution in rounds for identifier in execution.failure_ids)


def _restore_portfolio_artifact(
    update: PipelineState,
    payload: PortfolioPlanningArtifactPayload,
) -> None:
    update["decision_snapshot_id"] = payload.decision_snapshot_id
    update["decision_snapshot_hash"] = payload.decision_snapshot_hash
    update["plan_id"] = payload.plan_id
    update["plan_hash"] = payload.plan_hash
    if payload.report_id is not None:
        update["report_id"] = payload.report_id


def _restore_execution_artifact(
    update: PipelineState,
    payload: ExecutionArtifactPayload,
) -> None:
    """Restore an inert A6 receipt without constructing an execution capability."""
    if payload.receipt is not None:
        update["execution_id"] = payload.receipt.plan_hash


def _require_file_binding(run_dir: Path, artifact: StageArtifactRecord) -> None:
    path = run_dir / f"{artifact.stage.casefold()}.json"
    try:
        stored = StageArtifactRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"replay artifact is absent or malformed: {path}") from error
    if stored != artifact:
        raise ValueError(f"replay artifact differs from durable authority: {path}")
