"""Module containing durable run and stage-artifact timing contracts."""

import hashlib
import json
import uuid
from enum import StrEnum
from typing import ClassVar
from typing import Self

from pydantic import AwareDatetime
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator


class RunRecord(BaseModel):
    """One immutable run start with requested and actual availability timestamps."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1)
    requested_as_of: AwareDatetime
    started_at: AwareDatetime
    known_at: AwareDatetime
    through_stage: str = Field(pattern=r"^A[1-6]$")
    source_config_hash: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    intelligence_config_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    portfolio_config_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    execution_config_hash: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    manifest: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_actual_times(self) -> Self:
        """Prevent run metadata from backdating actual availability."""
        _ = _canonical_uuid4(self.run_id)
        if self.known_at < self.started_at:
            raise ValueError("known_at must not precede started_at.")
        stage_number = int(self.through_stage[1])
        if (self.intelligence_config_hash is not None) != (stage_number >= 2):
            raise ValueError("intelligence_config_hash is required exactly for runs through A2 or later.")
        if (self.portfolio_config_hash is not None) != (stage_number >= 5):
            raise ValueError("portfolio_config_hash is required exactly for runs through A5 or later.")
        if (self.execution_config_hash is not None) != (stage_number >= 6):
            raise ValueError("execution_config_hash is required exactly for runs through A6.")
        return self

    def canonical_json(self) -> str:
        """Serialize the start record deterministically for both durable stores."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


class RunTerminalStatus(StrEnum):
    """Possible terminal outcomes for a started run."""

    COMPLETED = "completed"
    FAILED = "failed"


class ArtifactRecordKind(StrEnum):
    """Durable record namespaces used by stage artifact bindings."""

    SOURCE_ITEM_VERSION = "source_item_version"
    ASSET = "asset"
    DOCUMENT = "document"
    FRAGMENT = "fragment"
    INTERPRETATION_ATTEMPT = "interpretation_attempt"
    OBSERVATION = "observation"
    CANONICAL_CLAIM = "canonical_claim"
    CANDIDATE_THESIS = "candidate_thesis"
    PLANNED_RESEARCH_TASK = "planned_research_task"
    RESEARCH_SESSION = "research_session"
    RESEARCH_SUMMARY = "research_summary"
    RESEARCH_TASK = "research_task"
    RESEARCH_RESULT = "research_result"
    RESEARCH_FETCH = "research_fetch"
    RESEARCH_FAILURE = "research_failure"
    RESEARCH_STOP = "research_stop"
    CLAIM_RESOLUTION = "claim_resolution"
    VERIFICATION = "verification"
    THESIS_REVISION = "thesis_revision"
    SIGNAL_CONTRIBUTION = "signal_contribution"
    DECISION_SNAPSHOT = "decision_snapshot"
    PORTFOLIO_PLAN = "portfolio_plan"
    REPORT = "report"
    OUTCOME_SCHEDULE = "outcome_schedule"
    TRADE_IDENTITY = "trade_identity"


class ArtifactRecordBinding(BaseModel):
    """One parsed namespace-qualified durable record identity."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    kind: ArtifactRecordKind
    record_id: str = Field(min_length=1)

    def encode(self) -> str:
        """Encode the binding without constraining punctuation in the durable ID."""
        return f"{self.kind.value}:{self.record_id}"


def bind_artifact_record(kind: ArtifactRecordKind, record_id: str) -> str:
    """Return one validated namespace-qualified artifact binding."""
    return ArtifactRecordBinding(kind=kind, record_id=record_id).encode()


def parse_artifact_record_binding(value: str) -> ArtifactRecordBinding:
    """Parse a namespace-qualified artifact binding or raise ValueError."""
    namespace, separator, record_id = value.partition(":")
    if not separator:
        raise ValueError("artifact record binding must contain a namespace")
    return ArtifactRecordBinding(kind=ArtifactRecordKind(namespace), record_id=record_id)


class RunFailureDetail(BaseModel):
    """Bounded non-sensitive context required to recover a failed run."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    stage: str | None = Field(default=None, pattern=r"^A[1-6]$")
    durable_record_ids: tuple[str, ...] = Field(default=(), max_length=1_000)
    retryable: bool


class RunTerminalEvent(BaseModel):
    """The single immutable terminal event for one started run."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    run_id: str = Field(min_length=1)
    status: RunTerminalStatus
    completed_at: AwareDatetime
    known_at: AwareDatetime
    failure_kind: str | None = Field(default=None, min_length=1, max_length=128)
    failure_detail: RunFailureDetail | None = None

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        """Require exact identity, actual time order, and status-specific failure data."""
        _ = _canonical_uuid4(self.run_id)
        if self.known_at < self.completed_at:
            raise ValueError("known_at must not precede completed_at.")
        if self.status is RunTerminalStatus.COMPLETED:
            if self.failure_kind is not None or self.failure_detail is not None:
                raise ValueError("A completed run must not contain failure data.")
        elif self.failure_kind is None or self.failure_detail is None:
            raise ValueError("A failed run requires failure_kind and failure_detail.")
        return self

    def canonical_json(self) -> str:
        """Serialize the terminal event deterministically."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )


def validate_run_id(run_id: str) -> str:
    """Return a canonical UUID4 run ID or raise ValueError."""
    return _canonical_uuid4(run_id)


def _canonical_uuid4(run_id: str) -> str:
    try:
        parsed = uuid.UUID(run_id)
    except ValueError as error:
        raise ValueError("run_id must be a UUID4 string") from error
    if parsed.version != 4 or str(parsed) != run_id:
        raise ValueError("run_id must be a canonical UUID4 string")
    return run_id


class StageArtifactRecord(BaseModel):
    """Immutable durable stage envelope with authoritative run-delta IDs."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    artifact_id: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    run_id: str = Field(min_length=1)
    stage: str = Field(pattern=r"^A[1-6]$")
    requested_as_of: AwareDatetime
    started_at: AwareDatetime
    decision_at: AwareDatetime | None = None
    known_at: AwareDatetime
    input_ids: tuple[str, ...]
    output_ids: tuple[str, ...]
    implementation_version: str = Field(min_length=1)
    payload: JsonValue

    @model_validator(mode="after")
    def validate_identity_and_times(self) -> Self:
        """Require actual times and the canonical artifact digest to agree."""
        if self.known_at < self.started_at:
            raise ValueError("known_at must not precede started_at.")
        if self.decision_at is not None and not (self.started_at <= self.decision_at <= self.known_at):
            raise ValueError("decision_at must fall between started_at and known_at.")
        if len(self.input_ids) != len(set(self.input_ids)):
            raise ValueError("artifact input bindings must be unique")
        if len(self.output_ids) != len(set(self.output_ids)):
            raise ValueError("artifact output bindings must be unique")
        for binding in (*self.input_ids, *self.output_ids):
            _ = parse_artifact_record_binding(binding)
        if self.artifact_id != self.canonical_hash():
            raise ValueError("artifact_id must equal the canonical artifact digest.")
        return self

    def canonical_hash(self) -> str:
        """Return the digest covering timing, bindings, version, and payload."""
        values: dict[str, JsonValue] = self.model_dump(mode="json", exclude={"artifact_id"})
        encoded: bytes = json.dumps(values, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def payload_hash(self) -> str:
        """Return the digest of the canonical stage payload only."""
        encoded: bytes = json.dumps(self.payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def from_payload(
        cls,
        *,
        run_id: str,
        stage: str,
        requested_as_of: AwareDatetime,
        started_at: AwareDatetime,
        decision_at: AwareDatetime | None,
        known_at: AwareDatetime,
        input_ids: tuple[str, ...],
        output_ids: tuple[str, ...],
        implementation_version: str,
        payload: JsonValue,
    ) -> Self:
        """Build an artifact and its canonical identifier from actual timestamps."""
        provisional: StageArtifactRecord = cls.model_construct(
            artifact_id="0" * 64,
            run_id=run_id,
            stage=stage,
            requested_as_of=requested_as_of,
            started_at=started_at,
            decision_at=decision_at,
            known_at=known_at,
            input_ids=input_ids,
            output_ids=output_ids,
            implementation_version=implementation_version,
            payload=payload,
        )
        return cls(
            artifact_id=provisional.canonical_hash(),
            run_id=run_id,
            stage=stage,
            requested_as_of=requested_as_of,
            started_at=started_at,
            decision_at=decision_at,
            known_at=known_at,
            input_ids=input_ids,
            output_ids=output_ids,
            implementation_version=implementation_version,
            payload=payload,
        )
