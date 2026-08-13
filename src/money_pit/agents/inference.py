"""Typed inference results and sanitized usage recording."""

from __future__ import annotations

import hashlib
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import TYPE_CHECKING
from typing import Generic
from typing import Literal
from typing import Protocol
from typing import TypeVar


if TYPE_CHECKING:
    from collections.abc import Generator

    from pydantic_ai.usage import RunUsage


OutputT = TypeVar("OutputT")
InferenceStage = Literal["A1", "A2", "A3", "A4"]
InferenceCallStatus = Literal["succeeded", "failed"]


@dataclass(frozen=True)
class InferenceUsage:
    """Provider-reported token and request usage for one logical invocation."""

    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    request_count: int = 0

    @classmethod
    def from_pydantic_ai(cls, usage: RunUsage) -> InferenceUsage:
        """Copy the supported PydanticAI usage fields into a stable contract."""
        return cls(
            input_tokens=usage.input_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
            request_count=usage.requests,
        )

    def __add__(self, other: InferenceUsage) -> InferenceUsage:
        """Aggregate usage without mutating either operand."""
        return InferenceUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            request_count=self.request_count + other.request_count,
        )


@dataclass(frozen=True)
class InferenceResult(Generic[OutputT]):
    """Typed model output paired with its provider-reported usage."""

    output: OutputT
    usage: InferenceUsage
    request_hash: str


@dataclass(frozen=True)
class InferenceCorrelation:
    """Durable workflow identity active while one model call is made."""

    run_id: str
    work_unit_id: str


@dataclass(frozen=True)
class InferenceCallRecord:
    """Sanitized durable account of one logical inference invocation."""

    stage: InferenceStage
    purpose: str
    model: str
    request_hash: str
    correlation: InferenceCorrelation | None
    status: InferenceCallStatus
    usage: InferenceUsage | None
    started_at: datetime
    completed_at: datetime
    elapsed_milliseconds: int
    failure_kind: str | None = None


class InferenceUsageSink(Protocol):
    """Persist sanitized inference accounting without receiving model content."""

    def record_inference_call(self, record: InferenceCallRecord) -> None:
        """Record one completed or failed logical provider invocation."""
        ...


class _DiscardInferenceUsage:
    def record_inference_call(self, record: InferenceCallRecord) -> None:
        del record


class InferenceTracking:
    """Bind workflow identity to inference records in the current execution context."""

    def __init__(self, sink: InferenceUsageSink | None = None) -> None:
        """Use the supplied sink or discard records explicitly."""
        self._sink: InferenceUsageSink = sink or _DiscardInferenceUsage()
        self._correlation: ContextVar[InferenceCorrelation | None] = ContextVar(
            f"money_pit_inference_correlation_{id(self)}",
            default=None,
        )

    @contextmanager
    def scope(self, *, run_id: str, work_unit_id: str) -> Generator[None]:
        """Associate calls in this context with one durable workflow work unit."""
        correlation = InferenceCorrelation(run_id=run_id, work_unit_id=work_unit_id)
        token = self._correlation.set(correlation)
        try:
            yield
        finally:
            self._correlation.reset(token)

    @property
    def correlation(self) -> InferenceCorrelation | None:
        """Return the correlation active in the current execution context."""
        return self._correlation.get()

    def record(self, record: InferenceCallRecord) -> None:
        """Send one content-free record to the configured durable sink."""
        self._sink.record_inference_call(record)


class ProviderInferenceError(Exception):
    """Retain known usage when output parsing fails after provider completion."""

    def __init__(self, *, usage: InferenceUsage, failure_kind: str) -> None:
        """Retain usage and the exception type without its message or content."""
        self.usage: InferenceUsage = usage
        self.failure_kind: str = failure_kind
        super().__init__("Inference provider output could not be accepted")


class InferenceInvocationError(Exception):
    """Expose a sanitized failure while retaining the provider error as its cause."""

    def __init__(self, *, stage: InferenceStage, failure_kind: str) -> None:
        """Construct a sanitized stage failure."""
        self.stage: InferenceStage = stage
        self.failure_kind: str = failure_kind
        super().__init__(f"{stage} inference failed ({failure_kind})")


def deterministic_request_hash(rendered_request: str) -> str:
    """Return a stable bounded identity without retaining the rendered request."""
    return hashlib.sha256(rendered_request.encode("utf-8")).hexdigest()


def monotonic_milliseconds_since(started: float) -> int:
    """Return non-negative elapsed milliseconds from a monotonic start point."""
    return max(0, round((time.monotonic() - started) * 1_000))


def utc_now() -> datetime:
    """Return an aware UTC timestamp for an inference record boundary."""
    return datetime.now(tz=timezone.utc)
