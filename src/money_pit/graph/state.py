"""Module containing the point-in-time harness state."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 - runtime validation uses datetime values
from pathlib import Path
from typing import Protocol
from typing import TypedDict

from money_pit.schemas.research import ResearchCumulativeContext  # noqa: TC001 - LangGraph resolves hints at runtime


class PipelineState(TypedDict, total=False):
    """Incremental references shared by the persistent A1-A6 graph."""

    run_id: str
    run_dir: str
    requested_as_of: datetime
    run_started_at: datetime
    decision_at: datetime
    requested_as_of_explicit: bool
    source_config_hash: str
    intelligence_config_hash: str
    portfolio_config_hash: str
    execution_config_hash: str
    source_id: str | None
    replay: bool
    completed_stages: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    observation_ids: tuple[str, ...]
    evidence_fragment_ids: tuple[str, ...]
    interpretation_attempt_ids: tuple[str, ...]
    research_failure_ids: tuple[str, ...]
    research_contexts: tuple[ResearchCumulativeContext, ...]
    candidate_thesis_ids: tuple[str, ...]
    thesis_revision_ids: tuple[str, ...]
    claim_resolution_decision_ids: tuple[str, ...]
    verification_result_ids: tuple[str, ...]
    signal_contribution_ids: tuple[str, ...]
    decision_snapshot_id: str
    decision_snapshot_hash: str
    plan_id: str
    plan_hash: str
    report_id: str
    execution_id: str


class PipelineStateError(Exception):
    """Raised when a node receives incomplete or invalid harness state."""


def require_run_id(state: PipelineState) -> str:
    """Return the durable run identifier or raise ``PipelineStateError``."""
    run_id: str | None = state.get("run_id")
    if not run_id:
        raise PipelineStateError("PipelineState missing required key 'run_id'")
    return run_id


def require_run_dir(state: PipelineState) -> Path:
    """Return the run artifact directory or raise ``PipelineStateError``."""
    run_dir: str | None = state.get("run_dir")
    if not run_dir:
        raise PipelineStateError("PipelineState missing required key 'run_dir'")
    return Path(run_dir)


def require_requested_as_of(state: PipelineState) -> datetime:
    """Return the aware baseline visibility boundary."""
    return _require_aware_clock(state, "requested_as_of")


def require_run_started_at(state: PipelineState) -> datetime:
    """Return the actual instant at which the run began."""
    return _require_aware_clock(state, "run_started_at")


def require_decision_at(state: PipelineState) -> datetime:
    """Return the actual instant at which this run makes decisions."""
    decision_at: datetime = _require_aware_clock(state, "decision_at")
    if decision_at < require_run_started_at(state):
        raise PipelineStateError("PipelineState 'decision_at' cannot precede 'run_started_at'")
    return decision_at


def _require_aware_clock(state: PipelineState, key: str) -> datetime:
    value: datetime | None = state.get(key)  # type: ignore[literal-required]
    if value is None:
        raise PipelineStateError(f"PipelineState missing required key {key!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise PipelineStateError(f"PipelineState {key!r} must be timezone-aware")
    return value


def completed_with(state: PipelineState, stage: str) -> tuple[str, ...]:
    """Return completed stages with one idempotent stage appended."""
    completed: tuple[str, ...] = state.get("completed_stages", ())
    return completed if stage in completed else (*completed, stage)


class PipelineNode(Protocol):
    """A capability-scoped LangGraph node returning a partial state update."""

    def __call__(self, state: PipelineState) -> PipelineState:
        """Map accumulated references to a partial state update."""
        ...
