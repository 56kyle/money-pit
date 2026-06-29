"""A5: manifest parse, jsonschema checks, action_type→tool routing, three-file write."""
from pathlib import Path
from typing import Callable

import jsonschema
from pydantic import TypeAdapter

from money_pit.compute.tool_map import ACTION_TYPE_TO_TOOL
from money_pit.compute.tool_map import COMPENSATING_ACTION
from money_pit.graph.state import PipelineState
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.validation_results import ActionStepsValidation
from money_pit.schemas.validation_results import ToolCall
from money_pit.schemas.validation_results import ValidationStatusReport
from money_pit.schemas.validation_results import ValidationStep


_ALPACA_SCHEMA_PATH: Path = (
    Path(__file__).parent.parent.parent.parent / "mcp" / "alpaca_order_schema.json"
)

_ALPACA_MCP_SERVER: str = "alpaca_mcp"

_action_steps_adapter: TypeAdapter[list[ActionStep]] = TypeAdapter(list[ActionStep])

_alpaca_schema_adapter: TypeAdapter[dict[str, object]] = TypeAdapter(dict[str, object])


def _execution_params_as_dict(params: ExecutionParameters) -> dict[str, object]:
    """Build a JSON-schema-compatible dict from ExecutionParameters, omitting None fields."""
    result: dict[str, object] = {
        "symbol": params.symbol,
        "side": params.side,
        "type": params.type,
        "time_in_force": params.time_in_force,
        "client_order_id": params.client_order_id,
    }
    if params.notional is not None:
        result["notional"] = params.notional
    if params.quantity is not None:
        result["quantity"] = params.quantity
    return result


def _build_tool_calls(step: ActionStep) -> tuple[list[ToolCall], list[ToolCall]]:
    """Return (tool_sequence, compensation_sequence) for a step."""
    primary_tool: str = ACTION_TYPE_TO_TOOL[step.action_type]
    compensating_tool: str = ACTION_TYPE_TO_TOOL[COMPENSATING_ACTION[step.action_type]]
    tool_sequence: list[ToolCall] = [
        ToolCall(
            tool_name=primary_tool,
            server=_ALPACA_MCP_SERVER,
            input_parameters=_execution_params_as_dict(step.execution_parameters),
        )
    ]
    compensation_sequence: list[ToolCall] = [
        ToolCall(
            tool_name=compensating_tool,
            server=_ALPACA_MCP_SERVER,
            input_parameters=None,
        )
    ]
    return tool_sequence, compensation_sequence


def _validate_step(
    step: ActionStep,
    slug: str,
    alpaca_schema: dict[str, object],
    behavioral_match_agent: Callable[[ActionStep, str], bool],
) -> ValidationStep:
    """Run all four checks and return a ValidationStep with MATCHED or UNMATCHED status."""
    tool_sequence, compensation_sequence = _build_tool_calls(step)
    params: dict[str, object] = _execution_params_as_dict(step.execution_parameters)

    try:
        jsonschema.validate(instance=params, schema=alpaca_schema)
    except jsonschema.ValidationError as exc:
        return ValidationStep(
            step_id=step.step_id,
            status=ValidationStatus.UNMATCHED,
            tool_sequence=tool_sequence,
            compensation_sequence=compensation_sequence,
            gap_description=f"Schema validation failed: {exc.message}",
        )

    # Phase 4 stub — tool availability check deferred to Phase 5; assume all tools present.

    if not behavioral_match_agent(step, slug):
        return ValidationStep(
            step_id=step.step_id,
            status=ValidationStatus.UNMATCHED,
            tool_sequence=tool_sequence,
            compensation_sequence=compensation_sequence,
            gap_description="Behavioral match agent returned False",
        )

    expected_id: str = f"{slug}:{step.step_id}"
    if step.execution_parameters.client_order_id != expected_id:
        return ValidationStep(
            step_id=step.step_id,
            status=ValidationStatus.UNMATCHED,
            tool_sequence=tool_sequence,
            compensation_sequence=compensation_sequence,
            gap_description=(
                f"client_order_id '{step.execution_parameters.client_order_id}'"
                f" does not match expected '{expected_id}'"
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
        "VALIDATED" if validation.overall_status == "validated" else "VALIDATION FAILED"
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


def make_validator_node(
    behavioral_match_agent: Callable[[ActionStep, str], bool],
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that validates each action step against schema and tool constraints."""
    alpaca_schema: dict[str, object] = _alpaca_schema_adapter.validate_json(
        _ALPACA_SCHEMA_PATH.read_text(encoding="utf-8")
    )

    def validator_node(state: PipelineState) -> dict[str, object]:
        working_dir_raw: str | None = state.get("working_dir")
        if working_dir_raw is None:
            raise ValueError("PipelineState missing required key 'working_dir'")
        slug: str | None = state.get("slug")
        if slug is None:
            raise ValueError("PipelineState missing required key 'slug'")
        working_dir: Path = Path(working_dir_raw)

        steps: list[ActionStep] = _action_steps_adapter.validate_json(
            (working_dir / "action_steps.json").read_text(encoding="utf-8")
        )

        validation_steps: list[ValidationStep] = [
            _validate_step(step, slug, alpaca_schema, behavioral_match_agent)
            for step in steps
        ]

        unmatched_ids: list[str] = [
            vs.step_id
            for vs in validation_steps
            if vs.status == ValidationStatus.UNMATCHED
        ]
        overall_status: str = "validated" if not unmatched_ids else "validation_failed"

        validation: ActionStepsValidation = ActionStepsValidation(
            slug=slug,
            overall_status=overall_status,
            steps=validation_steps,
        )
        _ = (working_dir / "action_steps_validation.json").write_text(
            validation.model_dump_json(indent=2), encoding="utf-8"
        )
        _ = (working_dir / "action_steps_validation.md").write_text(
            _render_markdown(slug, validation), encoding="utf-8"
        )

        status_report: ValidationStatusReport = ValidationStatusReport(
            status=overall_status,
            validation_performed=True,
            unmatched_steps=unmatched_ids,
            error=None,
        )
        _ = (working_dir / "validation_status.json").write_text(
            status_report.model_dump_json(indent=2), encoding="utf-8"
        )

        result: dict[str, object] = {
            "completed_steps": [*(state.get("completed_steps") or []), "validator"],
            "validation_steps": validation_steps,
        }
        return result

    return validator_node
