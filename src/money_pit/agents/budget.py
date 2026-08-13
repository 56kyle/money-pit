"""Deterministic complete-envelope budgets for model inference."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar
from typing import Generic
from typing import Protocol
from typing import TypeVar
from typing import runtime_checkable

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.agents.inference import InferenceCallRecord
from money_pit.agents.inference import InferenceCallStatus
from money_pit.agents.inference import InferenceInvocationError
from money_pit.agents.inference import InferenceResult
from money_pit.agents.inference import InferenceStage
from money_pit.agents.inference import InferenceTracking
from money_pit.agents.inference import InferenceUsage
from money_pit.agents.inference import ProviderInferenceError
from money_pit.agents.inference import deterministic_request_hash
from money_pit.agents.inference import monotonic_milliseconds_since
from money_pit.agents.inference import utc_now


RequestT = TypeVar("RequestT", bound=BaseModel)
ResponseT = TypeVar("ResponseT")


class InferenceBudgetLimits(BaseModel):
    """Configured character ceiling and reserved model-response capacity."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    maximum_characters: int = Field(default=120_000, gt=0)
    response_reserve_characters: int = Field(default=16_000, ge=0)


class InferenceBudgetBreakdown(BaseModel):
    """Exact deterministic components counted before provider invocation."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    maximum_characters: int
    system_prompt_characters: int
    output_schema_characters: int
    metadata_characters: int
    user_message_characters: int
    serialized_request_characters: int
    response_reserve_characters: int

    @property
    def rendered_total(self) -> int:
        """Return the complete request plus reserved response envelope."""
        return self.serialized_request_characters + self.response_reserve_characters


class InferenceBudgetExceededError(Exception):
    """Raised locally before a provider sees an oversized inference."""

    def __init__(self, breakdown: InferenceBudgetBreakdown) -> None:
        """Retain the typed component diagnostic for durable reporting."""
        self.breakdown: InferenceBudgetBreakdown = breakdown
        message = f"Rendered inference requires {breakdown.rendered_total} characters; limit is {breakdown.maximum_characters}"
        super().__init__(message)


@runtime_checkable
class RequestAllowanceProvider(Protocol):
    """Expose only the model-visible request allowance to projection code."""

    @property
    def request_character_allowance(self) -> int:
        """Return available characters after fixed envelope and response reserve."""
        ...


@runtime_checkable
class InferenceRequestBudget(Protocol):
    """Test an exact typed request against the provider-visible envelope."""

    def fits_request(self, request: BaseModel) -> bool:
        """Return whether the exact rendered request fits without invoking I/O."""
        ...


@dataclass(frozen=True)
class BoundedInferenceAgent(Generic[RequestT, ResponseT]):
    """Serialize, account for, and invoke one typed model boundary."""

    _invoke: Callable[[str], tuple[ResponseT, InferenceUsage]]
    _system_prompt: str
    _output_schema: str
    _metadata: str
    _limits: InferenceBudgetLimits
    _stage: InferenceStage
    _purpose: str
    _model_name: str
    _tracking: InferenceTracking

    @classmethod
    def create(
        cls,
        *,
        invoke: Callable[[str], tuple[ResponseT, InferenceUsage]],
        system_prompt: str,
        output_type: type[BaseModel],
        model_name: str,
        limits: InferenceBudgetLimits | None = None,
        stage: InferenceStage,
        purpose: str,
        tracking: InferenceTracking | None = None,
    ) -> "BoundedInferenceAgent[RequestT, ResponseT]":
        """Build one boundary from the exact fixed provider-visible components."""
        return cls(
            _invoke=invoke,
            _system_prompt=system_prompt,
            _output_schema=json.dumps(
                output_type.model_json_schema(),
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ),
            _metadata=json.dumps(
                {"model": model_name},
                sort_keys=True,
                separators=(",", ":"),
            ),
            _limits=limits or InferenceBudgetLimits(),
            _stage=stage,
            _purpose=purpose,
            _model_name=model_name,
            _tracking=tracking or InferenceTracking(),
        )

    @property
    def request_character_allowance(self) -> int:
        """Return the remaining exact user-message allowance."""
        fixed_request = self._render("")
        allowance = self._limits.maximum_characters - len(fixed_request) - self._limits.response_reserve_characters
        if allowance <= 0:
            raise InferenceBudgetExceededError(self._breakdown(user_message_characters=0))
        return allowance

    def __call__(self, request: RequestT) -> InferenceResult[ResponseT]:
        """Reject an oversized rendered envelope before invoking the provider."""
        user_message = serialize_inference_request(request)
        message = self._render(user_message)
        breakdown = self._breakdown(
            user_message_characters=len(user_message),
            serialized_request_characters=len(message),
        )
        if breakdown.rendered_total > breakdown.maximum_characters:
            raise InferenceBudgetExceededError(breakdown)
        request_hash = deterministic_request_hash(message)
        started_at = utc_now()
        monotonic_started = time.monotonic()
        try:
            output, usage = self._invoke(message)
        except ProviderInferenceError as error:
            usage = error.usage
            self._record(
                request_hash=request_hash,
                status="failed",
                usage=usage,
                started_at=started_at,
                monotonic_started=monotonic_started,
                failure_kind=error.failure_kind,
            )
            raise InferenceInvocationError(stage=self._stage, failure_kind=error.failure_kind) from error
        except Exception as error:
            failure_kind = type(error).__name__
            self._record(
                request_hash=request_hash,
                status="failed",
                usage=None,
                started_at=started_at,
                monotonic_started=monotonic_started,
                failure_kind=failure_kind,
            )
            raise InferenceInvocationError(stage=self._stage, failure_kind=failure_kind) from error
        self._record(
            request_hash=request_hash,
            status="succeeded",
            usage=usage,
            started_at=started_at,
            monotonic_started=monotonic_started,
            failure_kind=None,
        )
        return InferenceResult(output=output, usage=usage, request_hash=request_hash)

    def _record(
        self,
        *,
        request_hash: str,
        status: InferenceCallStatus,
        usage: InferenceUsage | None,
        started_at: datetime,
        monotonic_started: float,
        failure_kind: str | None,
    ) -> None:
        """Record content-free usage at the provider boundary."""
        completed_at = utc_now()
        self._tracking.record(
            InferenceCallRecord(
                stage=self._stage,
                purpose=self._purpose,
                model=self._model_name,
                request_hash=request_hash,
                correlation=self._tracking.correlation,
                status=status,
                usage=usage,
                started_at=started_at,
                completed_at=completed_at,
                elapsed_milliseconds=monotonic_milliseconds_since(monotonic_started),
                failure_kind=failure_kind,
            )
        )

    def fits_request(self, request: BaseModel) -> bool:
        """Test the exact rendered message and response reserve against the limit."""
        user_message = request.model_dump_json(indent=2)
        return (
            len(self._render(user_message)) + self._limits.response_reserve_characters
            <= self._limits.maximum_characters
        )

    def _render(self, user_message: str) -> str:
        """Render the exact single message passed to the provider agent."""
        return "\n".join(
            (
                "SYSTEM",
                self._system_prompt,
                "OUTPUT_SCHEMA",
                self._output_schema,
                "METADATA",
                self._metadata,
                "USER",
                user_message,
            )
        )

    def _breakdown(
        self,
        *,
        user_message_characters: int,
        serialized_request_characters: int | None = None,
    ) -> InferenceBudgetBreakdown:
        request_characters = (
            len(self._render("")) if serialized_request_characters is None else serialized_request_characters
        )
        return InferenceBudgetBreakdown(
            maximum_characters=self._limits.maximum_characters,
            system_prompt_characters=len(self._system_prompt),
            output_schema_characters=len(self._output_schema),
            metadata_characters=len(self._metadata),
            user_message_characters=user_message_characters,
            serialized_request_characters=request_characters,
            response_reserve_characters=self._limits.response_reserve_characters,
        )


def request_character_allowance(agent: object, *, fallback: int) -> int:
    """Return a bounded agent allowance or the explicit non-provider fallback."""
    if isinstance(agent, RequestAllowanceProvider):
        return agent.request_character_allowance
    return fallback


def inference_request_fits(agent: object, request: BaseModel, *, fallback: int) -> bool:
    """Check the production renderer or a deterministic non-provider test boundary."""
    if isinstance(agent, InferenceRequestBudget):
        return agent.fits_request(request)
    return len(request.model_dump_json(indent=2)) <= fallback


def serialize_inference_request(request: BaseModel) -> str:
    """Render the exact typed request passed across the provider boundary."""
    return request.model_dump_json(indent=2)


def serialized_inference_request_size(request: BaseModel) -> int:
    """Return the exact character count used by inference projection gates."""
    return len(serialize_inference_request(request))
