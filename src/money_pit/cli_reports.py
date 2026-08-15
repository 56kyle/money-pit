"""Module containing typed operator-facing CLI reports."""

from datetime import datetime
from typing import ClassVar

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.agents.inference import InferenceUsage
from money_pit.pipeline.orchestration import IntelligenceUpdateReport
from money_pit.storage.intelligence_work import FilteredInferenceUsage
from money_pit.storage.intelligence_work import IntelligenceWorkCompletionCounts
from money_pit.storage.intelligence_work import IntelligenceWorkStatus
from money_pit.storage.semantic_intelligence import SemanticWorkStatus


_FROZEN_CONFIG = ConfigDict(frozen=True, extra="forbid")


class IntelligenceUpdateBatchReport(BaseModel):
    """Aggregate of separately durable incremental intelligence runs."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    requested_iterations: int = Field(ge=1)
    performed_iterations: int = Field(ge=1)
    stopped_because_no_transition: bool
    run_ids: tuple[str, ...]
    durable_transition_count: int = Field(ge=0)
    completed: IntelligenceWorkCompletionCounts
    usage: InferenceUsage
    failed_call_count: int = Field(ge=0)
    unavailable_usage_call_count: int = Field(ge=0)
    remaining: IntelligenceWorkStatus
    runs: tuple[IntelligenceUpdateReport, ...]


class IntelligenceUsageReport(BaseModel):
    """Filtered token usage plus non-blocking efficiency diagnostics."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    usage: FilteredInferenceUsage
    diagnostics: tuple[str, ...] = ()


class IntelligenceStatusReport(BaseModel):
    """Categorized queue state with one explicit operator action."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    source_id: str | None
    next_action: str
    categories: dict[str, int]
    durable: IntelligenceWorkStatus
    semantic: SemanticWorkStatus


class ReplayReport(BaseModel):
    """Typed read-only replay result."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    run_id: str
    requested_as_of: AwareDatetime
    started_at: AwareDatetime
    terminal_status: str
    completed_at: AwareDatetime
    completed_stages: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    decision_at: AwareDatetime | None = None
    plan_id: str | None = None
    plan_hash: str | None = None
    report_id: str | None = None


class SourceStatusReport(BaseModel):
    """Read-only durable cursor status for one configured source."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    source_id: str
    configured: bool
    enabled: bool
    sync_cursor_present: bool
    backfill_cursor_present: bool
    sync_cursor_updated_at: AwareDatetime | None = None
    backfill_cursor_updated_at: AwareDatetime | None = None
    last_activity_at: AwareDatetime | None = None
    pending_work_count: int = Field(default=0, ge=0)


class DoctorCheck(BaseModel):
    """One provider-free diagnostic check."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    category: str
    status: str
    detail: str


class DoctorReport(BaseModel):
    """Provider-free configuration and storage diagnostic report."""

    model_config: ClassVar[ConfigDict] = _FROZEN_CONFIG
    checked_at: datetime
    healthy: bool
    project_root: str
    configuration_paths: dict[str, str]
    data_path: str
    database_path: str
    asset_path: str
    run_path: str
    report_path: str
    application_version: str
    database_release: str | None
    migration_required: bool
    secretspec_profile: str
    secretspec_manifest_path: str
    configured_model: str | None
    enabled_sources: tuple[str, ...]
    enabled_providers: tuple[str, ...]
    portfolio_environment: str | None
    execution_environment: str | None
    checks: tuple[DoctorCheck, ...]
