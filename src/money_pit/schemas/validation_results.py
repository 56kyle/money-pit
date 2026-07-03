"""ValidationStep, ActionStepsValidation, ValidationStatusReport — A5 output contracts."""
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import ValidationStatus


class ToolCall(BaseModel):
    """A single MCP tool invocation record within a validation or compensation sequence."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    server: str
    input_parameters: dict[str, object] | None


class ValidationStep(BaseModel):
    """Per-step validation outcome from A5's manifest check."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    status: ValidationStatus
    tool_sequence: list[ToolCall] | None
    compensation_sequence: list[ToolCall] | None
    gap_description: str | None


class ActionStepsValidation(BaseModel):
    """A5 output object; written to action_steps_validation.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    overall_status: str
    steps: list[ValidationStep]


class ValidationStatusReport(BaseModel):
    """Orchestration-layer completion signal from A5; written to validation_status.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    status: str
    validation_performed: bool
    unmatched_steps: list[str]
    error: str | None
