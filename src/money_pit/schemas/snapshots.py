"""Module containing immutable decision-input snapshot contracts."""

import hashlib
import json
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator


class SnapshotBinding(BaseModel):
    """Identity and capture time of one immutable input snapshot."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    snapshot_id: str = Field(min_length=1)
    captured_at: AwareDatetime


class PortfolioPlanningArtifactPayload(BaseModel):
    """Dependency-neutral exact references emitted by portfolio planning."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    decision_snapshot_id: str = Field(min_length=1)
    decision_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_id: str | None = Field(default=None, min_length=1)
    outcome_schedule_ids: tuple[str, ...] = ()


class DecisionSnapshotPayload(BaseModel):
    """Complete point-in-time inputs bound to a portfolio decision."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1)
    requested_as_of: AwareDatetime
    decision_at: AwareDatetime
    known_at: AwareDatetime
    evidence_fragment_ids: tuple[str, ...]
    claim_observation_ids: tuple[str, ...]
    verification_result_ids: tuple[str, ...]
    thesis_revision_ids: tuple[str, ...]
    canonical_projection_hashes: dict[str, str]
    portfolio_snapshot: SnapshotBinding
    market_snapshot: SnapshotBinding
    risk_snapshot: SnapshotBinding
    liquidity_snapshot: SnapshotBinding
    tax_snapshot: SnapshotBinding
    source_config_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    strategy_config_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    execution_config_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    policy_version: str = Field(min_length=1)
    claim_freshness_policy_version: str = Field(min_length=1)
    universe_fingerprint: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    processor_versions: dict[str, str] = Field(default_factory=dict)
    calibration_version: str = Field(min_length=1)
    optimizer_version: str = Field(min_length=1)
    trade_generation_version: str = Field(min_length=1)
    execution_eligible: bool
    model_versions: dict[str, str] = Field(default_factory=dict)
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    additional_inputs: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_actual_times(self) -> Self:
        """Require availability to follow the actual capital-decision instant."""
        if self.known_at < self.decision_at:
            raise ValueError("known_at must not precede decision_at.")
        return self

    def canonical_bytes(self) -> bytes:
        """Serialize the payload deterministically."""
        value: str = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return value.encode("utf-8")


class DecisionSnapshot(BaseModel):
    """A decision snapshot protected by its canonical digest."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    decision_snapshot_id: str = Field(min_length=1)
    payload: DecisionSnapshotPayload
    decision_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    @classmethod
    def from_payload(cls, decision_snapshot_id: str, payload: DecisionSnapshotPayload) -> Self:
        """Create a snapshot carrying the digest of its canonical payload."""
        return cls(
            decision_snapshot_id=decision_snapshot_id,
            payload=payload,
            decision_hash=hashlib.sha256(payload.canonical_bytes()).hexdigest(),
        )

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        """Reject a decision snapshot whose payload has changed."""
        expected: str = hashlib.sha256(self.payload.canonical_bytes()).hexdigest()
        if self.decision_hash != expected:
            raise ValueError("decision_hash does not match the canonical payload.")
        return self
