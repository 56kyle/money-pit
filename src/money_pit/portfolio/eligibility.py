"""Module containing fail-closed portfolio action eligibility."""

from datetime import datetime
from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.portfolio.calibration import ScenarioDistribution
from money_pit.schemas.execution_policy import TradableAssetClass


class ActionTier(StrEnum):
    """Maximum capital authority granted to a candidate."""

    OBSERVATION_ONLY = "observation_only"
    HOLD_OR_REDUCE = "hold_or_reduce"
    NEW_EXPOSURE = "new_exposure"


class VerificationState(StrEnum):
    """Deterministic verification state of one material factual anchor."""

    SUPPORTED = "supported"
    CONTRADICTED = "contradicted"
    MIXED = "mixed"
    UNRESOLVED = "unresolved"


class SupportedInstrumentKind(StrEnum):
    """Instrument resolution result, including an explicit unsupported state."""

    US_EQUITY = TradableAssetClass.US_EQUITY
    US_ETF = TradableAssetClass.US_ETF
    UNSUPPORTED = "unsupported"


class MaterialEvidenceAnchor(BaseModel):
    """Portfolio-admission facts and provenance for one material anchor."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim_key: str = Field(min_length=1)
    status: VerificationState
    known_at: AwareDatetime
    valid_until: AwareDatetime | None
    allowed_for_portfolio: bool
    authoritative_primary: bool
    independent_provenance_groups: tuple[str, ...]

    @model_validator(mode="after")
    def require_unique_provenance_groups(self) -> Self:
        """Reject duplicated groups that could overstate corroboration."""
        if len(self.independent_provenance_groups) != len(set(self.independent_provenance_groups)):
            raise ValueError("independent provenance groups must be unique")
        return self


class CandidateAdmissionInput(BaseModel):
    """Complete point-in-time inputs for one capital-admission decision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    instrument_kind: SupportedInstrumentKind
    held_weight: float = Field(ge=0, le=1)
    intelligence_available: bool = False
    synthesis_evidence_sufficient: bool = False
    thesis_active: bool
    thesis_is_bearish: bool
    thesis_valid_until: AwareDatetime | None
    invalidation_rules: tuple[str, ...]
    scenarios: ScenarioDistribution | None
    material_anchors: tuple[MaterialEvidenceAnchor, ...]
    contradicting_anchors: tuple[MaterialEvidenceAnchor, ...] = ()
    market_data_current: bool
    risk_data_current: bool
    liquidity_data_current: bool
    tradable: bool


class EligibilityDecision(BaseModel):
    """Stable action tier and every deterministic exclusion reason."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    instrument: str = Field(min_length=1)
    action_tier: ActionTier
    reasons: tuple[str, ...]


def evaluate_candidate_eligibility(
    candidate: CandidateAdmissionInput,
    *,
    as_of: datetime,
) -> EligibilityDecision:
    """Grant new exposure only when every material capital gate passes."""
    reasons: list[str] = _candidate_gate_reasons(candidate, as_of=as_of)
    for anchor in candidate.material_anchors:
        reasons.extend(_anchor_gate_reasons(anchor, as_of=as_of))
    for anchor in candidate.contradicting_anchors:
        reasons.extend(_contradiction_gate_reasons(anchor, as_of=as_of))

    canonical_reasons: tuple[str, ...] = tuple(sorted(set(reasons)))
    if not canonical_reasons and not candidate.thesis_is_bearish:
        tier: ActionTier = ActionTier.NEW_EXPOSURE
    elif candidate.held_weight > 0:
        tier = ActionTier.HOLD_OR_REDUCE
    else:
        tier = ActionTier.OBSERVATION_ONLY
    return EligibilityDecision(
        instrument=candidate.instrument.strip().upper(),
        action_tier=tier,
        reasons=canonical_reasons,
    )


def _candidate_gate_reasons(candidate: CandidateAdmissionInput, *, as_of: datetime) -> list[str]:
    reasons: list[str] = _semantic_intelligence_gate_reasons(candidate)
    if candidate.instrument_kind is SupportedInstrumentKind.UNSUPPORTED:
        reasons.append("instrument is not a supported US equity or ETF")
    if not candidate.tradable:
        reasons.append("instrument is not currently tradable")
    if not candidate.thesis_active:
        reasons.append("thesis is not active")
    if candidate.thesis_is_bearish:
        reasons.append("bearish thesis cannot create short exposure")
    if candidate.thesis_valid_until is not None and candidate.thesis_valid_until <= as_of:
        reasons.append("thesis is expired")
    if not candidate.invalidation_rules:
        reasons.append("thesis has no explicit invalidation rules")
    if candidate.scenarios is None:
        reasons.append("thesis has no normalized scenario distribution")
    if not candidate.material_anchors:
        reasons.append("thesis has no material factual anchors")
    reasons.extend(_coverage_gate_reasons(candidate))
    return reasons


def _semantic_intelligence_gate_reasons(candidate: CandidateAdmissionInput) -> list[str]:
    """Reject capital authority while semantic or evidence review is incomplete."""
    reasons: list[str] = []
    if not candidate.intelligence_available:
        reasons.append("hypothesis is unavailable pending semantic review")
    if not candidate.synthesis_evidence_sufficient:
        reasons.append("hypothesis has insufficient evidence for synthesis")
    return reasons


def _coverage_gate_reasons(candidate: CandidateAdmissionInput) -> list[str]:
    reasons: list[str] = []
    if not candidate.market_data_current:
        reasons.append("current market data is missing")
    if not candidate.risk_data_current:
        reasons.append("current risk data is missing")
    if not candidate.liquidity_data_current:
        reasons.append("current liquidity data is missing")
    return reasons


def _anchor_gate_reasons(anchor: MaterialEvidenceAnchor, *, as_of: datetime) -> list[str]:
    reasons: list[str] = []
    if anchor.known_at > as_of:
        reasons.append(f"claim {anchor.claim_key} was not known as of the decision")
    if anchor.valid_until is not None and anchor.valid_until <= as_of:
        reasons.append(f"claim {anchor.claim_key} is expired")
    if not anchor.allowed_for_portfolio:
        reasons.append(f"claim {anchor.claim_key} is unauthorized for portfolio use")
    if anchor.status is not VerificationState.SUPPORTED:
        reasons.append(f"claim {anchor.claim_key} is {anchor.status.value}")
    if not anchor.authoritative_primary and len(anchor.independent_provenance_groups) < 2:
        reasons.append(f"claim {anchor.claim_key} lacks independent corroboration")
    return reasons


def _contradiction_gate_reasons(anchor: MaterialEvidenceAnchor, *, as_of: datetime) -> list[str]:
    """Treat a contradiction as harmless only when currently and authoritatively disproved."""
    reasons: list[str] = []
    if anchor.status is not VerificationState.CONTRADICTED:
        reasons.append(f"claim {anchor.claim_key} is an unresolved material contradiction")
    if anchor.known_at > as_of:
        reasons.append(f"contradiction {anchor.claim_key} was not known as of the decision")
    if anchor.valid_until is not None and anchor.valid_until <= as_of:
        reasons.append(f"contradiction {anchor.claim_key} resolution is expired")
    if not anchor.allowed_for_portfolio:
        reasons.append(f"contradiction {anchor.claim_key} is unauthorized for portfolio use")
    if not anchor.authoritative_primary and len(anchor.independent_provenance_groups) < 2:
        reasons.append(f"contradiction {anchor.claim_key} lacks independent corroboration")
    return reasons
