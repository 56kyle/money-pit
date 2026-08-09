"""Module adapting deterministic portfolio planning to harness stage A5."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime

from money_pit.graph.edges import require_predecessor
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import completed_with
from money_pit.graph.state import require_requested_as_of
from money_pit.graph.state import require_run_dir
from money_pit.graph.state import require_run_id
from money_pit.graph.state import require_run_started_at
from money_pit.pipeline.artifacts import StageArtifact
from money_pit.pipeline.artifacts import StageArtifactStore
from money_pit.pipeline.artifacts import persist_stage_artifact
from money_pit.pipeline.chain import Stage
from money_pit.portfolio.planning import PortfolioPlanningService
from money_pit.portfolio.planning import PortfolioReviewRequest
from money_pit.portfolio.planning import PortfolioReviewResult
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.snapshots import PortfolioPlanningArtifactPayload


def make_portfolio_planning_node(
    *,
    service: PortfolioPlanningService,
    implementation_version: str,
    artifact_store: StageArtifactStore,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> PipelineNode:
    """Return an A5 node that has no broker-write capability."""

    def node(state: PipelineState) -> PipelineState:
        require_predecessor(state, Stage.A5)
        run_id = require_run_id(state)
        requested_as_of = require_requested_as_of(state)
        started_at = require_run_started_at(state)
        result: PortfolioReviewResult = service.review(
            PortfolioReviewRequest(
                run_id=run_id,
                requested_as_of=requested_as_of,
                same_run_observation_ids=state.get("observation_ids", ()),
                same_run_verification_result_ids=state.get("verification_result_ids", ()),
                same_run_resolution_decision_ids=state.get("claim_resolution_decision_ids", ()),
                same_run_thesis_revision_ids=state.get("thesis_revision_ids", ()),
                execution_eligible=not state.get("requested_as_of_explicit", False),
                source_id=state.get("source_id"),
            )
        )
        payload = PortfolioPlanningArtifactPayload(
            decision_snapshot_id=result.decision_snapshot_id,
            decision_snapshot_hash=result.decision_snapshot_hash,
            plan_id=result.plan_id,
            plan_hash=result.plan_hash,
            report_id=result.report_id,
            outcome_schedule_ids=result.outcome_schedule_ids,
        )
        decision_at = result.decision_at
        artifact: StageArtifact = persist_stage_artifact(
            require_run_dir(state),
            run_id=run_id,
            stage=Stage.A5,
            requested_as_of=requested_as_of,
            started_at=started_at,
            known_at=clock(),
            decision_at=decision_at,
            input_ids=(
                *(
                    bind_artifact_record(ArtifactRecordKind.OBSERVATION, item)
                    for item in state.get("observation_ids", ())
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.CLAIM_RESOLUTION, item)
                    for item in state.get("claim_resolution_decision_ids", ())
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.VERIFICATION, item)
                    for item in state.get("verification_result_ids", ())
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.THESIS_REVISION, item)
                    for item in state.get("thesis_revision_ids", ())
                ),
            ),
            output_ids=(
                bind_artifact_record(ArtifactRecordKind.DECISION_SNAPSHOT, result.decision_snapshot_id),
                bind_artifact_record(ArtifactRecordKind.PORTFOLIO_PLAN, result.plan_id),
                *(
                    tuple(
                        ()
                        if result.report_id is None
                        else (bind_artifact_record(ArtifactRecordKind.REPORT, result.report_id),)
                    )
                ),
                *(
                    bind_artifact_record(ArtifactRecordKind.OUTCOME_SCHEDULE, item)
                    for item in result.outcome_schedule_ids
                ),
            ),
            implementation_version=implementation_version,
            payload=payload,
            artifact_store=artifact_store,
        )
        update: PipelineState = {
            "completed_stages": completed_with(state, Stage.A5.value),
            "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
            "decision_snapshot_id": result.decision_snapshot_id,
            "decision_snapshot_hash": result.decision_snapshot_hash,
            "plan_id": result.plan_id,
            "plan_hash": result.plan_hash,
            "decision_at": decision_at,
        }
        if result.report_id is not None:
            update["report_id"] = result.report_id
        return update

    return node
