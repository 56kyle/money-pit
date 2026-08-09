"""Module containing bounded, replayable research contracts."""

import hashlib
import json
from enum import StrEnum
from typing import Annotated
from typing import ClassVar
from typing import Literal
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator

from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.runs import ArtifactRecordKind
from money_pit.schemas.runs import bind_artifact_record
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import TrustLevel


class ResearchSessionStatus(StrEnum):
    """Lifecycle state of a bounded research session."""

    ACTIVE = "active"
    COMPLETED = "completed"
    STOPPED = "stopped"


class ResearchStopReason(StrEnum):
    """Deterministic reasons research can terminate."""

    EVIDENCE_STANDARD_SATISFIED = "evidence_standard_satisfied"
    DECISIVE_CONTRADICTION = "decisive_contradiction"
    NO_NEW_INDEPENDENT_PROVENANCE = "no_new_independent_provenance"
    BUDGET_EXPIRED = "budget_expired"
    UNRESOLVED = "unresolved"


class ResearchTaskStatus(StrEnum):
    """Lifecycle state of one proposed provider query."""

    PENDING = "pending"
    SEARCHED = "searched"
    FETCHING = "fetching"
    COMPLETED = "completed"
    FAILED = "failed"


class ResearchFetchStatus(StrEnum):
    """Outcome of fetching and durably acquiring one result."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class ResearchQuery(BaseModel):
    """A validated read-only provider query proposed by an agent."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    provider: str = Field(min_length=1)
    query_text: str = Field(min_length=1)
    candidate_thesis_id: str | None = Field(default=None, min_length=1)
    purpose: str = Field(min_length=1)
    material_claim_keys: tuple[str, ...] = ()
    requested_at: AwareDatetime
    max_results: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_material_claim_keys(self) -> Self:
        """Reject duplicate durable material-anchor identities."""
        if len(self.material_claim_keys) != len(set(self.material_claim_keys)):
            raise ValueError("material_claim_keys must be unique")
        return self


class ResearchDiscoveryResult(BaseModel):
    """Discovery-only metadata that cannot itself verify a claim."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    result_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    canonical_uri: str = Field(min_length=1)
    title: str | None = None
    snippet: str | None = None
    discovered_at: AwareDatetime
    published_at: AwareDatetime | None = None
    updated_at: AwareDatetime | None = None
    available_at: AwareDatetime | None = None
    provenance_group: str | None = Field(default=None, min_length=1)
    fetch_token: str | None = Field(default=None, min_length=1)
    metadata: dict[str, JsonValue] = Field(default_factory=dict)


class ResearchDiscoveryBatch(BaseModel):
    """One bounded provider search response."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    query: ResearchQuery
    results: tuple[ResearchDiscoveryResult, ...]
    searched_at: AwareDatetime


class CandidateThesisResearchScope(BaseModel):
    """Research scoped to one candidate thesis."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["candidate_thesis"] = "candidate_thesis"
    candidate_thesis_id: str = Field(min_length=1)


class CanonicalClaimResearchScope(BaseModel):
    """Research scoped to one resolved canonical claim."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["canonical_claim"] = "canonical_claim"
    canonical_claim_key: str = Field(min_length=1)


class ClaimObservationResearchScope(BaseModel):
    """Research scoped to one unresolved claim observation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["claim_observation"] = "claim_observation"
    observation_id: str = Field(min_length=1)


ResearchScope = Annotated[
    CandidateThesisResearchScope | CanonicalClaimResearchScope | ClaimObservationResearchScope,
    Field(discriminator="kind"),
]


class ResearchSession(BaseModel):
    """Durable budget and state for iterative candidate research."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    scope: ResearchScope
    started_at: AwareDatetime
    deadline_at: AwareDatetime
    maximum_rounds: int = Field(default=3, ge=1)
    maximum_queries: int = Field(default=12, ge=1)
    maximum_fetches: int = Field(default=24, ge=1)
    status: ResearchSessionStatus = ResearchSessionStatus.ACTIVE
    stop_reason: ResearchStopReason | None = None

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        """Require ordered time bounds and an explicit reason for stopped work."""
        if self.deadline_at <= self.started_at:
            raise ValueError("deadline_at must be after started_at.")
        if (self.status is ResearchSessionStatus.STOPPED) != (self.stop_reason is not None):
            raise ValueError("stop_reason must be present exactly when status is stopped.")
        return self


class ResearchTask(BaseModel):
    """One durable query task within a research round."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    task_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    round_number: int = Field(ge=1)
    query: ResearchQuery
    status: ResearchTaskStatus = ResearchTaskStatus.PENDING
    created_at: AwareDatetime


class ResearchFetch(BaseModel):
    """Durable outcome of fetching one discovery result."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    fetch_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    source_item_id: str | None = Field(default=None, min_length=1)
    asset_id: str | None = Field(default=None, min_length=1)
    status: ResearchFetchStatus
    failure_kind: str | None = Field(default=None, min_length=1)
    attempted_at: AwareDatetime

    @model_validator(mode="after")
    def validate_outcome(self) -> Self:
        """Require durable provenance for success and a failure kind for failure."""
        if self.status is ResearchFetchStatus.SUCCEEDED and (self.source_item_id is None or self.asset_id is None):
            raise ValueError("A successful fetch must identify its durable source item and asset.")
        if self.status is ResearchFetchStatus.FAILED and self.failure_kind is None:
            raise ValueError("A failed fetch must identify its failure kind.")
        return self


class EvidenceAliasBinding(BaseModel):
    """Durable resolution and source authorization for one model alias."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    alias: str = Field(pattern=r"^[EFT][0-9]{6}$")
    fragment_ids: tuple[str, ...] = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    provenance_group: str = Field(min_length=1)
    allowed_uses: tuple[AllowedUse, ...] = Field(min_length=1)
    trust_level: TrustLevel


class ResearchEvidenceRecord(BaseModel):
    """Bounded research evidence visible to planning and synthesis agents."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    alias: str = Field(pattern=r"^[EFT][0-9]{6}$")
    kind: str = Field(min_length=1)
    text: str = Field(min_length=1)
    provenance_group: str = Field(min_length=1)
    trust_level: TrustLevel


class ProvisionalAnchorEvidence(BaseModel):
    """Deterministic pre-verification provenance accumulated for one exact anchor."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    claim_key: str = Field(min_length=1)
    provenance_groups: tuple[str, ...] = ()
    has_authoritative_primary: bool = False


class MaterialAnchorAssessment(BaseModel):
    """Deterministic assessment of candidate factual-anchor coverage."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    material_claim_keys: tuple[str, ...] = ()
    supported_claim_keys: tuple[str, ...] = ()
    provisionally_covered_claim_keys: tuple[str, ...] = ()
    provisional_evidence: tuple[ProvisionalAnchorEvidence, ...] = ()
    contradicted_claim_keys: tuple[str, ...] = ()
    unresolved_claim_keys: tuple[str, ...] = ()
    independent_provenance_groups: tuple[str, ...] = ()
    has_authoritative_primary: bool = False
    evidence_standard_satisfied: bool = False
    decisive_contradiction: bool = False


class ResearchCumulativeContext(BaseModel):
    """Bounded cumulative durable context supplied to the A3 planner."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    new_observations: tuple[ClaimObservation, ...] = ()
    evidence: tuple[ResearchEvidenceRecord, ...] = ()
    alias_bindings: tuple[EvidenceAliasBinding, ...] = Field(default=(), exclude=True)
    provenance_groups: tuple[str, ...] = ()
    failure_kinds: tuple[str, ...] = ()
    material_anchor_assessment: MaterialAnchorAssessment


class ResearchStageAdmission(BaseModel):
    """Exact staged A3 records admitted by one authoritative artifact."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1)
    known_at: AwareDatetime
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    semantic_context_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    candidate_thesis_ids: tuple[str, ...] = ()
    session_ids: tuple[str, ...] = ()
    summary_ids: tuple[str, ...] = ()
    planned_task_ids: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
    result_ids: tuple[str, ...] = ()
    fetch_ids: tuple[str, ...] = ()
    failure_ids: tuple[str, ...] = ()
    source_item_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    fragment_ids: tuple[str, ...] = ()
    interpretation_attempt_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    stop_event_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_unique_bindings(self) -> Self:
        """Reject repeats within a record namespace while allowing shared durable IDs."""
        bindings: tuple[tuple[str, tuple[str, ...]], ...] = (
            ("candidate_thesis_ids", self.candidate_thesis_ids),
            ("session_ids", self.session_ids),
            ("summary_ids", self.summary_ids),
            ("planned_task_ids", self.planned_task_ids),
            ("task_ids", self.task_ids),
            ("result_ids", self.result_ids),
            ("fetch_ids", self.fetch_ids),
            ("failure_ids", self.failure_ids),
            ("source_item_ids", self.source_item_ids),
            ("asset_ids", self.asset_ids),
            ("document_ids", self.document_ids),
            ("fragment_ids", self.fragment_ids),
            ("interpretation_attempt_ids", self.interpretation_attempt_ids),
            ("observation_ids", self.observation_ids),
            ("stop_event_ids", self.stop_event_ids),
        )
        for field_name, values in bindings:
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must be unique")
        return self

    def output_ids(self) -> tuple[str, ...]:
        """Return globally unambiguous namespace-qualified artifact bindings."""
        bindings: tuple[tuple[ArtifactRecordKind, tuple[str, ...]], ...] = (
            (ArtifactRecordKind.RESEARCH_SESSION, self.session_ids),
            (ArtifactRecordKind.RESEARCH_SUMMARY, self.summary_ids),
            (ArtifactRecordKind.PLANNED_RESEARCH_TASK, self.planned_task_ids),
            (ArtifactRecordKind.RESEARCH_TASK, self.task_ids),
            (ArtifactRecordKind.RESEARCH_RESULT, self.result_ids),
            (ArtifactRecordKind.RESEARCH_FETCH, self.fetch_ids),
            (ArtifactRecordKind.RESEARCH_FAILURE, self.failure_ids),
            (ArtifactRecordKind.SOURCE_ITEM_VERSION, self.source_item_ids),
            (ArtifactRecordKind.ASSET, self.asset_ids),
            (ArtifactRecordKind.DOCUMENT, self.document_ids),
            (ArtifactRecordKind.FRAGMENT, self.fragment_ids),
            (ArtifactRecordKind.INTERPRETATION_ATTEMPT, self.interpretation_attempt_ids),
            (ArtifactRecordKind.OBSERVATION, self.observation_ids),
            (ArtifactRecordKind.RESEARCH_STOP, self.stop_event_ids),
        )
        return tuple(bind_artifact_record(kind, identifier) for kind, values in bindings for identifier in values)

    def input_ids(self) -> tuple[str, ...]:
        """Return namespace-qualified candidate artifact inputs."""
        return tuple(
            bind_artifact_record(ArtifactRecordKind.CANDIDATE_THESIS, identifier)
            for identifier in self.candidate_thesis_ids
        )


class ResearchCandidateSummaryRecord(BaseModel):
    """Canonical per-session A3 summary persisted with terminal research state."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    summary_id: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    known_at: AwareDatetime
    summary_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: JsonValue

    @model_validator(mode="after")
    def validate_summary_hash(self) -> Self:
        """Require the digest of canonical serialized summary content."""
        encoded = json.dumps(
            self.summary,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode()
        if hashlib.sha256(encoded).hexdigest() != self.summary_hash:
            raise ValueError("summary_hash does not match canonical summary content")
        return self


class RecoveredResearchStage(BaseModel):
    """A terminal unadmitted A3 stage reconstructed without provider or model calls."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    admission: ResearchStageAdmission
    payload: JsonValue
