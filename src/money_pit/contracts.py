"""Typed boundaries for agents and persistent harness services."""

from __future__ import annotations

# Pydantic resolves these annotations at runtime; they cannot be TYPE_CHECKING-only.
# ruff: noqa: TC001
from collections.abc import Callable
from datetime import datetime
from typing import ClassVar
from typing import Protocol
from typing import Self
from typing import TypeAlias

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator

from money_pit.agents.inference import InferenceResult
from money_pit.evidence.work import EvidenceInterpretationWork
from money_pit.evidence.work import ReusableInterpretation
from money_pit.portfolio.universe import LayeredUniverse
from money_pit.schemas.claims import CanonicalClaim
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import ClaimKind
from money_pit.schemas.claims import ClaimObservation
from money_pit.schemas.claims import ClaimResolutionDecision
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.claims import UnresolvedObservationCursor
from money_pit.schemas.claims import UnresolvedObservationPage
from money_pit.schemas.claims import VerificationResult
from money_pit.schemas.research import MaterialAnchorAssessment
from money_pit.schemas.research import RecoveredResearchStage
from money_pit.schemas.research import ResearchCumulativeContext
from money_pit.schemas.research import ResearchStageAdmission
from money_pit.schemas.research import ResearchStopReason
from money_pit.schemas.temporal import CausalBridge
from money_pit.schemas.temporal import SignalContribution
from money_pit.schemas.temporal import SignalContributionKind
from money_pit.schemas.theses import CandidateThesis
from money_pit.schemas.theses import ScenarioOutcome
from money_pit.schemas.theses import ThesisDirection
from money_pit.schemas.theses import ThesisRevision
from money_pit.schemas.universe import DiscoveryBasis


class EvidencePromptRecord(BaseModel):
    """One bounded model-visible alias, never a durable evidence identifier."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    alias: str = Field(pattern=r"^[EFT][0-9]{6}$")
    kind: str = Field(min_length=1)
    text: str = Field(min_length=1)
    core: bool


class InterpretationRequest(BaseModel):
    """A1 input containing one source item and a bounded evidence projection."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    source_item_id: str = Field(min_length=1)
    evidence: tuple[EvidencePromptRecord, ...] = Field(min_length=1)
    requested_as_of: AwareDatetime
    context_known_at: AwareDatetime


class ClaimObservationDraft(BaseModel):
    """Agent-authored claim semantics before deterministic identity and alias resolution."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_text: str = Field(min_length=1)
    claim_kind: ClaimKind
    category: ClaimCategory
    evidence_aliases: tuple[str, ...] = Field(min_length=1)
    asserted_at: AwareDatetime = Field(
        description=(
            "Claim assertion time in RFC 3339 format with an explicit UTC offset. "
            "Use the source-supported assertion time and offset; when the source does "
            "not supply both, conservatively use the request requested_as_of."
        ),
    )
    effective_from: AwareDatetime | None = Field(
        default=None,
        description=(
            "Economic effective time in RFC 3339 format with an explicit UTC offset. "
            "Use null unless the evidence supplies the time and its timezone."
        ),
    )
    event_at: AwareDatetime | None = Field(
        default=None,
        description=(
            "Economic event time in RFC 3339 format with an explicit UTC offset. "
            "Use null unless the evidence supplies the time and its timezone."
        ),
    )
    review_at: AwareDatetime | None = Field(
        default=None,
        description=(
            "Economic review time in RFC 3339 format with an explicit UTC offset. "
            "Use null unless the evidence supplies the time and its timezone."
        ),
    )
    valid_until: AwareDatetime | None = Field(
        default=None,
        description=(
            "Economic validity end in RFC 3339 format with an explicit UTC offset. "
            "Use null unless the evidence supplies the time and its timezone."
        ),
    )
    horizon_class: HorizonClass
    instruments: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    causal_mechanisms: tuple[str, ...] = ()
    regime_assumptions: tuple[str, ...] = ()
    supersedes_observation_id: str | None = None


class InterpretationDraft(BaseModel):
    """Complete typed A1 agent output for one evidence document."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    observations: tuple[ClaimObservationDraft, ...]


class CandidateThesisDraft(BaseModel):
    """Agent-authored candidate before deterministic identifiers are assigned."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    direction: ThesisDirection
    instrument_reference: str | None = Field(default=None, min_length=1)
    theme: str | None = Field(default=None, min_length=1)
    horizon_class: HorizonClass
    discovery_basis: DiscoveryBasis
    causal_mechanisms: tuple[str, ...] = ()
    regime_assumptions: tuple[str, ...] = ()


class ResearchTaskDraft(BaseModel):
    """A typed, read-only research request emitted by A2 or A3."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidate_thesis_id: str | None = Field(default=None, min_length=1)
    candidate_subject: str | None = Field(default=None, min_length=1)
    provider: str = Field(min_length=1)
    query: str = Field(min_length=1)
    purpose: str = Field(min_length=1)
    material_claim_keys: tuple[str, ...] = ()
    maximum_results: int = Field(default=5, ge=1, le=20)


class DiscoveryRequest(BaseModel):
    """A2 point-in-time discovery input."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)

    universe: LayeredUniverse
    claims: tuple[CanonicalClaim, ...]
    observations: tuple[ClaimObservation, ...]
    requested_as_of: AwareDatetime
    context_known_at: AwareDatetime
    allowed_provider_names: tuple[str, ...]
    signals: tuple["DiscoverySignal", ...] = ()


class DiscoverySignal(BaseModel):
    """Compact material change supplied to candidate discovery."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    signal_id: str = Field(min_length=1)
    claim_key: str = Field(min_length=1)
    status: str = Field(min_length=1)
    instruments: tuple[str, ...] = ()
    themes: tuple[str, ...] = ()
    claim_texts: tuple[str, ...] = ()


class DiscoveryDraft(BaseModel):
    """Complete typed A2 output."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[CandidateThesisDraft, ...]
    research_tasks: tuple[ResearchTaskDraft, ...]


class ResearchRoundPlan(BaseModel):
    """Agent decision to continue bounded research or stop explicitly."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    tasks: tuple[ResearchTaskDraft, ...] = ()
    stop_reason: ResearchStopReason | None = None


class ResearchRoundExecution(BaseModel):
    """Durable result summary returned by the read-only research service."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    task_count: int = Field(ge=0)
    query_count: int = Field(ge=0)
    fetch_count: int = Field(ge=0)
    source_item_ids: tuple[str, ...] = ()
    asset_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    fragment_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    interpretation_attempt_ids: tuple[str, ...] = ()
    failure_ids: tuple[str, ...] = ()
    independent_provenance_groups: tuple[str, ...] = ()
    failure_kinds: tuple[str, ...] = ()
    context: ResearchCumulativeContext | None = Field(default=None, exclude=True)


class ResearchPlanningRequest(BaseModel):
    """A3 context after a completed round."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidate: CandidateThesis
    progress: "ResearchProgressDigest"
    remaining_queries: int = Field(ge=0)
    remaining_fetches: int = Field(ge=0)
    deadline: AwareDatetime
    requested_as_of: AwareDatetime
    context_known_at: AwareDatetime
    allowed_provider_names: tuple[str, ...]


class ResearchProgressDigest(BaseModel):
    """Compact durable state supplied to the A3 planner."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    material_anchor_assessment: MaterialAnchorAssessment
    provenance_groups: tuple[str, ...] = ()
    normalized_prior_queries: tuple[str, ...] = ()
    accepted_fetch_count: int = Field(ge=0)
    rejected_result_count: int = Field(ge=0)
    completed_wave_count: int = Field(ge=0)


class ResolutionDraft(BaseModel):
    """Agent-authored relation between two immutable observations."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    subject_observation_id: str = Field(min_length=1)
    object_observation_id: str | None = Field(default=None, min_length=1)
    relation: str = Field(pattern=r"^(same|contradicts|distinct|updates)$")
    rationale: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_relation_shape(self) -> Self:
        """Require unary distinct and binary comparison relations."""
        if self.relation == "distinct":
            if self.object_observation_id is not None:
                raise ValueError("A distinct resolution must be unary")
        elif self.object_observation_id is None:
            raise ValueError("A non-distinct resolution requires an object observation")
        return self


class VerificationDraft(BaseModel):
    """Agent-authored verification using durable, already persisted evidence aliases."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    status: str = Field(pattern=r"^(supported|contradicted|mixed|unresolved)$")
    supporting_evidence_aliases: tuple[str, ...] = ()
    contradicting_evidence_aliases: tuple[str, ...] = ()
    valid_until: AwareDatetime | None = None
    limitations: tuple[str, ...] = ()


class ThesisRevisionDraft(BaseModel):
    """Complete A4 thesis semantics before version identity is assigned."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    promoted_from_candidate_id: str | None = Field(default=None, min_length=1)
    revises_revision_id: str | None = Field(default=None, min_length=1)
    subject: str = Field(min_length=1)
    instrument: str | None = Field(default=None, min_length=1)
    theme: str | None = Field(default=None, min_length=1)
    direction: ThesisDirection
    horizon_class: HorizonClass
    effective_from: AwareDatetime
    event_at: AwareDatetime | None = None
    review_at: AwareDatetime
    valid_until: AwareDatetime | None = None
    scenario_distribution: tuple[ScenarioOutcome, ...] = Field(min_length=1)
    invalidation_rules: tuple[str, ...] = Field(min_length=1)
    supporting_observation_ids: tuple[str, ...] = ()
    contradicting_observation_ids: tuple[str, ...] = ()
    triggered_invalidation_rules: tuple[str, ...] = ()
    causal_mechanisms: tuple[str, ...] = Field(min_length=1)
    regime_assumptions: tuple[str, ...] = ()
    confidence: float = Field(ge=0, le=1)
    reasoning: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_revision_target(self) -> Self:
        """Require exactly one deterministic new-or-existing thesis target."""
        references = (
            self.promoted_from_candidate_id is not None,
            self.revises_revision_id is not None,
        )
        if sum(references) != 1:
            raise ValueError(
                "A revision draft must promote one candidate or revise one prior revision",
            )
        return self


class ContributionDraft(BaseModel):
    """Agent judgment applied only after deterministic temporal comparison."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    observation_id: str = Field(min_length=1)
    thesis_subject: str = Field(min_length=1)
    relation: SignalContributionKind
    causal_bridge: CausalBridge | None = None


class ResolutionCandidateSet(BaseModel):
    """One unresolved subject and its bounded deterministic match candidates."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    subject_observation_id: str = Field(min_length=1)
    candidate_observation_ids: tuple[str, ...]


class SynthesisRequest(BaseModel):
    """A4 bounded point-in-time synthesis input."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    candidates: tuple[CandidateThesis, ...]
    claims: tuple[CanonicalClaim, ...]
    observations: tuple[ClaimObservation, ...]
    resolution_candidate_sets: tuple[ResolutionCandidateSet, ...]
    prior_revisions: tuple[ThesisRevision, ...]
    requested_as_of: AwareDatetime
    context_known_at: AwareDatetime
    research_context: ResearchCumulativeContext


class SynthesisDraft(BaseModel):
    """Complete typed A4 agent output."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    resolutions: tuple[ResolutionDraft, ...]
    verifications: tuple[VerificationDraft, ...]
    revisions: tuple[ThesisRevisionDraft, ...]
    contributions: tuple[ContributionDraft, ...]


InterpretationAgent: TypeAlias = Callable[[InterpretationRequest], InferenceResult[InterpretationDraft]]
DiscoveryAgent: TypeAlias = Callable[[DiscoveryRequest], InferenceResult[DiscoveryDraft]]
ResearchPlanningAgent: TypeAlias = Callable[[ResearchPlanningRequest], InferenceResult[ResearchRoundPlan]]
SynthesisAgent: TypeAlias = Callable[[SynthesisRequest], InferenceResult[SynthesisDraft]]
UniverseLoader: TypeAlias = Callable[[datetime], LayeredUniverse]


class EvidenceWorkRepository(Protocol):
    """Point-in-time read access to unprocessed durable evidence."""

    def list_pending_documents(
        self,
        *,
        as_of: datetime,
        source_id: str | None,
        limit: int,
        interpreter_version: str,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Return uninterpreted evidence visible at the boundary."""
        ...

    def documents_for_bundle(
        self,
        *,
        source_item_id: str,
        content_version: str,
        processor_name: str | None = None,
        processor_version: str | None = None,
    ) -> tuple[EvidenceInterpretationWork, ...]:
        """Return every processed document for one exact source-item version."""
        ...

    def reusable_interpretation(
        self,
        work: EvidenceInterpretationWork,
        *,
        interpreter_version: str,
    ) -> ReusableInterpretation | None:
        """Return one exact completed interpretation without model execution."""
        ...

    def preserved_legacy_interpretation(
        self,
        work: EvidenceInterpretationWork,
    ) -> ReusableInterpretation | None:
        """Return migrated predecessor work without claiming policy equivalence."""
        ...

    def begin_interpretation(
        self,
        work: EvidenceInterpretationWork,
        *,
        run_id: str,
        interpreter_version: str,
        started_at: datetime,
    ) -> str:
        """Begin one document-version interpretation attempt."""
        ...

    def complete_interpretation(
        self,
        attempt_id: str,
        *,
        observation_ids: tuple[str, ...],
        known_at: datetime,
        completed_at: datetime,
    ) -> None:
        """Complete an attempt, including a successful zero-observation result."""
        ...

    def fail_interpretation(
        self,
        attempt_id: str,
        *,
        failure_kind: str,
        completed_at: datetime,
    ) -> None:
        """Record one failed interpretation attempt."""
        ...


class ClaimMemory(Protocol):
    """Append-only claim persistence required by A1 and A4."""

    def append_observation(self, observation: ClaimObservation) -> None:
        """Append one immutable observation."""
        ...

    def append_resolution(self, decision: ClaimResolutionDecision) -> None:
        """Append one immutable resolution decision."""
        ...

    def append_verification(self, verification: VerificationResult) -> None:
        """Append one immutable verification."""
        ...

    def observations_as_of(self, *, as_of: datetime) -> tuple[ClaimObservation, ...]:
        """Return observations visible at the boundary."""
        ...

    def projections_as_of(self, *, as_of: datetime) -> tuple[CanonicalClaim, ...]:
        """Return canonical projections visible at the boundary."""
        ...

    def observations_by_ids(self, ids: tuple[str, ...]) -> tuple[ClaimObservation, ...]:
        """Return exact same-run observations without widening visibility."""
        ...

    def unresolved_observation_page(
        self,
        *,
        requested_as_of: datetime,
        after: UnresolvedObservationCursor | None = None,
        limit: int = 50,
    ) -> UnresolvedObservationPage:
        """Return one bounded keyset page before per-subject candidate lookup."""
        ...

    def latest_verifications_for_observations(
        self,
        observation_ids: tuple[str, ...],
        *,
        requested_as_of: datetime,
        same_run_verification_ids: tuple[str, ...] = (),
    ) -> dict[str, VerificationResult]:
        """Return latest baseline plus exact same-run verification per observation."""
        ...

    def resolution_candidates(
        self,
        subject_observation_id: str,
        *,
        requested_as_of: datetime,
        same_run_observation_ids: tuple[str, ...] = (),
        limit: int = 20,
    ) -> tuple[ClaimObservation, ...]:
        """Return bounded deterministic FTS candidates for one unresolved observation."""
        ...

    def resolved_claim_keys(
        self,
        observation_ids: tuple[str, ...],
        *,
        requested_as_of: datetime,
        pending_resolutions: tuple[ClaimResolutionDecision, ...] = (),
    ) -> dict[str, str]:
        """Resolve exact observation membership without caller-authored claim keys."""
        ...

    def projections_with_deltas(
        self,
        *,
        requested_as_of: datetime,
        observation_ids: tuple[str, ...],
        resolution_ids: tuple[str, ...],
        verification_ids: tuple[str, ...],
    ) -> tuple[CanonicalClaim, ...]:
        """Project the baseline plus exact same-run intelligence deltas."""
        ...


class ThesisMemory(Protocol):
    """Append-only candidate, revision, and contribution persistence."""

    def append_candidate(self, candidate: CandidateThesis) -> None:
        """Append one sourced candidate."""
        ...

    def append_revision(self, revision: ThesisRevision) -> None:
        """Append one complete thesis revision."""
        ...

    def append_contribution(self, contribution: SignalContribution) -> None:
        """Append one traceable signal contribution."""
        ...

    def candidates_as_of(self, *, as_of: datetime) -> tuple[CandidateThesis, ...]:
        """Return candidates visible at the boundary."""
        ...

    def candidates_due_for_research(
        self,
        *,
        as_of: datetime,
        exact_candidate_ids: tuple[str, ...] = (),
    ) -> tuple[CandidateThesis, ...]:
        """Return deterministic due work, restricted to exact identities when supplied."""
        ...

    def revisions_as_of(self, *, as_of: datetime) -> tuple[ThesisRevision, ...]:
        """Return revisions visible at the boundary."""
        ...

    def candidates_by_ids(self, candidate_ids: tuple[str, ...]) -> tuple[CandidateThesis, ...]:
        """Return exact candidates attributed to the current run."""
        ...

    def revisions_by_ids(self, revision_ids: tuple[str, ...]) -> tuple[ThesisRevision, ...]:
        """Return exact revisions attributed to the current run."""
        ...


class ResearchRoundRunner(Protocol):
    """Capability-scoped adapter over durable read-only research providers."""

    def start_session(
        self,
        *,
        run_id: str,
        candidate: CandidateThesis,
        started_at: datetime,
        deadline: datetime,
        maximum_rounds: int,
        maximum_queries: int,
        maximum_fetches: int,
    ) -> str:
        """Create the candidate session at A3's actual start time."""
        ...

    def resume_or_start_session(
        self,
        *,
        run_id: str,
        candidate: CandidateThesis,
        started_at: datetime,
        deadline: datetime,
        maximum_rounds: int,
        maximum_queries: int,
        maximum_fetches: int,
    ) -> str:
        """Resume interrupted durable candidate work or create a new wave session."""
        ...

    def pending_tasks_for_session(self, session_id: str) -> tuple[ResearchTaskDraft, ...]:
        """Return durable unfinished tasks for a resumed wave."""
        ...

    def run_round(
        self,
        *,
        session_id: str,
        job_id: str,
        run_id: str,
        candidate: CandidateThesis,
        round_number: int,
        tasks: tuple[ResearchTaskDraft, ...],
        requested_as_of: datetime,
        decision_at: datetime,
        historical_explicit: bool,
        query_budget: int,
        fetch_budget: int,
        on_completed: Callable[[ResearchRoundExecution], None] | None = None,
    ) -> ResearchRoundExecution:
        """Execute and persist one budgeted read-only research round."""
        ...

    def finalize_session(
        self,
        session_id: str,
        *,
        run_id: str,
        reason: ResearchStopReason,
        stopped_at: datetime,
        known_at: datetime,
        summary_payload: JsonValue,
    ) -> str:
        """Atomically persist one terminal stop and its semantic summary."""
        ...

    def stage_admission(
        self,
        *,
        run_id: str,
        session_ids: tuple[str, ...],
        known_at: datetime,
    ) -> ResearchStageAdmission:
        """Reconstruct exact terminal descendants for atomic A3 admission."""
        ...

    def recover_stage_admission(
        self,
        *,
        run_id: str,
        known_at: datetime,
    ) -> RecoveredResearchStage | None:
        """Recover complete terminal staged work without provider or model calls."""
        ...


class ResearchTaskMemory(Protocol):
    """Durable research-task queue shared by A2 and A3."""

    def append_task(
        self,
        task: ResearchTaskDraft,
        *,
        run_id: str,
        known_at: datetime,
        origin_unit_ids: tuple[str, ...] = (),
    ) -> str:
        """Append and return one immutable research-task identity."""
        ...

    def checkpoint_planner_tasks(
        self,
        tasks: tuple[ResearchTaskDraft, ...],
        *,
        run_id: str,
        known_at: datetime,
    ) -> tuple[str, ...]:
        """Atomically persist one paid planner result before execution."""
        ...

    def planner_result(
        self,
        *,
        job_id: str,
        wave_number: int,
    ) -> ResearchRoundPlan | None:
        """Return one previously paid planner result, including empty results."""
        ...

    def checkpoint_planner_result(
        self,
        *,
        job_id: str,
        wave_number: int,
        run_id: str,
        known_at: datetime,
        request: ResearchPlanningRequest,
        result: ResearchRoundPlan,
    ) -> None:
        """Persist the complete validated planner result before downstream work."""
        ...

    def pending_for_candidate(
        self,
        candidate_thesis_id: str,
        *,
        run_id: str,
        as_of: datetime,
        origin_unit_ids: tuple[str, ...] = (),
    ) -> tuple[ResearchTaskDraft, ...]:
        """Return pending tasks for one candidate."""
        ...
