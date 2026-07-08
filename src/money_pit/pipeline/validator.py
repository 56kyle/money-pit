"""Module containing the A5 node handling manifest-existence and jsonschema checks, action_type-to-tool routing, and the three-file write for the money_pit package."""

from pathlib import Path

import jsonschema
from pydantic import TypeAdapter

from money_pit.compute.tool_map import ACTION_TYPE_TO_TOOL
from money_pit.compute.tool_map import COMPENSATING_ACTION
from money_pit.constants import ACTION_STEPS_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_VALIDATION_JSON_FILENAME
from money_pit.constants import ACTION_STEPS_VALIDATION_MD_FILENAME
from money_pit.constants import VALIDATION_STATUS_FILENAME
from money_pit.contracts import ToolManifest
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.mcp.manifest import pinned_manifest
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.enums import DataSourceToken
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ToolCall
from money_pit.schemas.validation_results import ValidationStatusReport
from money_pit.schemas.validation_results import ValidationStep


_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])


def _build_tool_calls(step: ActionStep) -> tuple[list[ToolCall], list[ToolCall]]:
    """Return (tool_sequence, compensation_sequence) for a step."""
    primary_tool: str = ACTION_TYPE_TO_TOOL[step.action_type]
    compensating_tool: str = ACTION_TYPE_TO_TOOL[COMPENSATING_ACTION[step.action_type]]
    tool_sequence: list[ToolCall] = [
        ToolCall(
            tool_name=primary_tool,
            server=DataSourceToken.ALPACA_MCP.value,
            input_parameters=step.execution_parameters.to_order_payload(),
        )
    ]
    compensation_sequence: list[ToolCall] = [
        ToolCall(
            tool_name=compensating_tool,
            server=DataSourceToken.ALPACA_MCP.value,
            input_parameters=None,
        )
    ]
    return tool_sequence, compensation_sequence


def _validate_step(
    step: ActionStep,
    slug: str,
    manifest: ToolManifest,
) -> ValidationStep:
    """Run existence, schema, and client_order_id checks and return a MATCHED or UNMATCHED ValidationStep."""
    tool_sequence, compensation_sequence = _build_tool_calls(step)
    params: dict[str, object] = step.execution_parameters.to_order_payload()

    tool: str = ACTION_TYPE_TO_TOOL[step.action_type]
    if tool not in manifest:
        return ValidationStep(
            step_id=step.step_id,
            status=ValidationStatus.UNMATCHED,
            tool_sequence=tool_sequence,
            compensation_sequence=compensation_sequence,
            gap_description=f"Tool '{tool}' not present in manifest",
        )

    try:
        jsonschema.validate(instance=params, schema=manifest[tool])
    except jsonschema.ValidationError as exc:
        return ValidationStep(
            step_id=step.step_id,
            status=ValidationStatus.UNMATCHED,
            tool_sequence=tool_sequence,
            compensation_sequence=compensation_sequence,
            gap_description=f"Schema validation failed: {exc.message}",
        )

    expected_id: str = f"{slug}:{step.step_id}"
    if step.execution_parameters.client_order_id != expected_id:
        return ValidationStep(
            step_id=step.step_id,
            status=ValidationStatus.UNMATCHED,
            tool_sequence=tool_sequence,
            compensation_sequence=compensation_sequence,
            gap_description=(
                f"client_order_id '{step.execution_parameters.client_order_id}' does not match expected '{expected_id}'"
            ),
        )

    return ValidationStep(
        step_id=step.step_id,
        status=ValidationStatus.MATCHED,
        tool_sequence=tool_sequence,
        compensation_sequence=compensation_sequence,
        gap_description=None,
    )


def _render_markdown(slug: str, validation: ActionStepsValidation) -> str:
    """Return a human-readable markdown summary of an ActionStepsValidation."""
    status_label: str = (
        "VALIDATED" if validation.overall_status == OverallValidationStatus.VALIDATED else "VALIDATION FAILED"
    )
    lines: list[str] = [
        f"# Action Steps Validation — {slug}",
        "",
        f"Status: {status_label}",
    ]
    for step in validation.steps:
        tool_names: str = ", ".join(tc.tool_name for tc in (step.tool_sequence or []))
        lines += [
            "",
            f"## {step.step_id} — {step.status.value}",
            f"Tools: {tool_names}",
        ]
        if step.gap_description is not None:
            lines.append(step.gap_description)
    return "\n".join(lines) + "\n"


def _overall_status(validation_steps: list[ValidationStep]) -> OverallValidationStatus:
    """Return VALIDATED when no steps are UNMATCHED, otherwise VALIDATION_FAILED."""
    return (
        OverallValidationStatus.VALIDATED
        if not any(vs.status == ValidationStatus.UNMATCHED for vs in validation_steps)
        else OverallValidationStatus.VALIDATION_FAILED
    )


def _write_validation_artifacts(
    working_dir: Path,
    slug: str,
    validation: ActionStepsValidation,
) -> None:
    """Write the validation JSON, markdown summary, and status report to the working directory."""
    _ = (working_dir / ACTION_STEPS_VALIDATION_JSON_FILENAME).write_text(
        validation.model_dump_json(indent=2), encoding="utf-8"
    )
    _ = (working_dir / ACTION_STEPS_VALIDATION_MD_FILENAME).write_text(
        _render_markdown(slug, validation), encoding="utf-8"
    )
    unmatched_ids: list[str] = [s.step_id for s in validation.steps if s.status == ValidationStatus.UNMATCHED]
    status_report: ValidationStatusReport = ValidationStatusReport(
        status=validation.overall_status,
        validation_performed=True,
        unmatched_steps=unmatched_ids,
        error=None,
    )
    _ = (working_dir / VALIDATION_STATUS_FILENAME).write_text(
        status_report.model_dump_json(indent=2), encoding="utf-8"
    )


def make_validator_node(
    manifest: ToolManifest | None = None,
) -> PipelineNode:
    """Return a LangGraph node that validates each action step against schema and tool constraints.

    With the default `manifest=None`, `pinned_manifest()` is resolved eagerly at
    construction and can therefore raise `ManifestUnavailableError` at graph-build time.
    """
    resolved_manifest: ToolManifest = manifest if manifest is not None else pinned_manifest()

    def validator_node(state: PipelineState) -> PipelineState:
        working_dir: Path = require_working_dir(state)
        slug: str = require_slug(state)

        steps: list[ActionStep] = _action_steps_adapter.validate_json(
            (working_dir / ACTION_STEPS_JSON_FILENAME).read_text(encoding="utf-8")
        )

        validation_steps: list[ValidationStep] = [_validate_step(step, slug, resolved_manifest) for step in steps]
        overall_status: OverallValidationStatus = _overall_status(validation_steps)

        validation: ActionStepsValidation = ActionStepsValidation(
            slug=slug,
            overall_status=overall_status,
            steps=validation_steps,
        )
        _write_validation_artifacts(working_dir, slug, validation)

        result: PipelineState = {
            "completed_steps": with_completed_step(state, "validator"),
            "validation_steps": validation_steps,
        }
        return result

    return validator_node
