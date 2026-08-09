"""Module adapting guarded determination and execution to harness stage A6."""

from collections.abc import Callable
from datetime import UTC
from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.execution_control.errors import PreflightDeniedError
from money_pit.execution_control.models import ExecutionReceipt
from money_pit.execution_control.models import PreflightResult
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
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record


class ExecutionArtifactPayload(BaseModel):
    """Immutable successful receipt or typed preflight denial from A6."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    plan_id: str
    receipt: ExecutionReceipt | None = None
    denial: PreflightResult | None = None


def make_execution_node(
    *,
    execute: Callable[[str], ExecutionReceipt],
    artifact_store: StageArtifactStore,
    implementation_version: str,
    clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> PipelineNode:
    """Return A6 around the sole guarded gateway capability."""

    def node(state: PipelineState) -> PipelineState:
        require_predecessor(state, Stage.A6)
        run_id = require_run_id(state)
        plan_id = state.get("plan_id")
        if not plan_id:
            raise ValueError("A6 requires a durable portfolio plan ID")
        decision_at = clock()
        receipt: ExecutionReceipt | None = None
        denial: PreflightResult | None = None
        try:
            receipt = execute(plan_id)
        except PreflightDeniedError as error:
            denial = error.result
        payload = ExecutionArtifactPayload(plan_id=plan_id, receipt=receipt, denial=denial)
        output_ids = (
            ()
            if receipt is None
            else tuple(
                bind_artifact_record(ArtifactRecordKind.TRADE_IDENTITY, item)
                for item in receipt.submitted_trade_identities
            )
        )
        artifact: StageArtifact = persist_stage_artifact(
            require_run_dir(state),
            run_id=run_id,
            stage=Stage.A6,
            requested_as_of=require_requested_as_of(state),
            started_at=require_run_started_at(state),
            known_at=clock(),
            decision_at=decision_at,
            input_ids=(bind_artifact_record(ArtifactRecordKind.PORTFOLIO_PLAN, plan_id),),
            output_ids=output_ids,
            implementation_version=implementation_version,
            payload=payload,
            artifact_store=artifact_store,
        )
        update: PipelineState = {
            "completed_stages": completed_with(state, Stage.A6.value),
            "artifact_ids": (*state.get("artifact_ids", ()), artifact.artifact_id),
            "decision_at": decision_at,
        }
        if receipt is not None:
            update["execution_id"] = receipt.plan_hash
        return update

    return node
