"""Tests for money_pit.pipeline.validator — A5's deterministic, LLM-free capability gate.

Pins wave S2 / ADR 0004: the static pinned manifest, the fail-closed
ManifestUnavailableError, and _validate_step's manifest-based existence, schema-acceptance,
and client_order_id checks (the injected behavioral_match_agent predicate is gone). No mocks:
real ActionStep / ExecutionParameters values and real dicts throughout. Assertions target
exception TYPES and ValidationStatus enum values, never gap-message text.
"""

from pathlib import Path

import pytest
from pytest import FixtureRequest

from money_pit.compute.tool_map import ACTION_TYPE_TO_TOOL
from money_pit.mcp import manifest as mcp_manifest
from money_pit.mcp.order_schema import load_order_schema
from money_pit.pipeline.validator import _overall_status
from money_pit.pipeline.validator import _validate_step
from money_pit.schemas.action_steps import ActionStep
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.analysis_draft import Scenario
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import OverallValidationStatus
from money_pit.schemas.enums import RegimeTag
from money_pit.schemas.enums import ValidationStatus
from money_pit.schemas.validation_results import ValidationStep


_SLUG = "2026-01-01_00-00-00"


def _make_validation_step(step_id: str, status: ValidationStatus) -> ValidationStep:
    return ValidationStep(
        step_id=step_id,
        status=status,
        tool_sequence=None,
        compensation_sequence=None,
        gap_description=None if status == ValidationStatus.MATCHED else "gap",
    )


@pytest.fixture
def slug(request: FixtureRequest) -> str:
    return getattr(request, "param", _SLUG)


@pytest.fixture
def action_step__step_id(request: FixtureRequest) -> str:
    return getattr(request, "param", "A001")


@pytest.fixture
def action_step__client_order_id(request: FixtureRequest, slug: str, action_step__step_id: str) -> str:
    return getattr(request, "param", f"{slug}:{action_step__step_id}")


@pytest.fixture
def action_step__execution_parameters(
    request: FixtureRequest, action_step__client_order_id: str
) -> ExecutionParameters:
    return getattr(
        request,
        "param",
        ExecutionParameters(
            symbol="NVDA",
            notional="1500.00",
            qty=None,
            side="buy",
            type="market",
            time_in_force="day",
            client_order_id=action_step__client_order_id,
        ),
    )


@pytest.fixture
def action_step__scenario_table() -> ScenarioTable:
    scenario = Scenario(
        probability=33,
        return_pct=0.1,
        timeframe=None,
        confirming_metric=None,
        mechanism=None,
        max_drawdown=None,
    )
    return ScenarioTable(bull=scenario, base=scenario, bear=scenario)


@pytest.fixture
def action_step(
    request: FixtureRequest,
    action_step__step_id: str,
    action_step__execution_parameters: ExecutionParameters,
    action_step__scenario_table: ScenarioTable,
) -> ActionStep:
    return getattr(
        request,
        "param",
        ActionStep(
            step_id=action_step__step_id,
            instrument="NVDA",
            action_type=ActionType.BUY,
            description="Establish a starter position.",
            group_id=None,
            execution_parameters=action_step__execution_parameters,
            one_sentence_thesis="The thesis still holds on current data.",
            regime_tag=RegimeTag.UNCERTAIN,
            expected_value=1.0,
            scenario_table=action_step__scenario_table,
            invalidation_conditions=[],
            sizing_rationale="Sized to conviction and account risk budget.",
            conviction=ConvictionLevel.MEDIUM,
        ),
    )


@pytest.fixture
def stub_free_order_schema(stub_free_order_schema_path: Path) -> dict[str, object]:
    return load_order_schema(stub_free_order_schema_path)


@pytest.fixture
def matched_manifest(stub_free_order_schema: dict[str, object]) -> dict[str, dict[str, object]]:
    return {"place_stock_order": stub_free_order_schema}


def test_pinned_manifest_with_available_schema(stub_free_order_schema_path: Path) -> None:
    result = mcp_manifest.pinned_manifest(schema_path=stub_free_order_schema_path)

    assert "place_stock_order" in result
    assert isinstance(result["place_stock_order"], dict)
    for tool in set(ACTION_TYPE_TO_TOOL.values()):
        assert tool in result


def test_pinned_manifest_with_unavailable_schema(tmp_path: Path) -> None:
    missing_schema: Path = tmp_path / "not_yet_pinned.json"

    with pytest.raises(mcp_manifest.ManifestUnavailableError):
        _ = mcp_manifest.pinned_manifest(schema_path=missing_schema)


def test__validate_step_with_matching_step(
    action_step: ActionStep, slug: str, matched_manifest: dict[str, dict[str, object]]
) -> None:
    result = _validate_step(action_step, slug, matched_manifest)

    assert result.status == ValidationStatus.MATCHED


def test__validate_step_with_missing_tool(action_step: ActionStep, slug: str) -> None:
    result = _validate_step(action_step, slug, {})

    assert result.status == ValidationStatus.UNMATCHED
    assert result.gap_description is not None


def test__validate_step_with_schema_mismatch(action_step: ActionStep, slug: str) -> None:
    over_constrained_manifest: dict[str, dict[str, object]] = {
        "place_stock_order": {
            "type": "object",
            "properties": {"limit_price": {"type": "number"}},
            "required": ["limit_price"],
        }
    }

    result = _validate_step(action_step, slug, over_constrained_manifest)

    assert result.status == ValidationStatus.UNMATCHED
    assert result.gap_description is not None


@pytest.mark.parametrize("action_step__client_order_id", ["not-the-expected-id"], indirect=True)
def test__validate_step_with_client_order_id_mismatch(
    action_step: ActionStep, slug: str, matched_manifest: dict[str, dict[str, object]]
) -> None:
    result = _validate_step(action_step, slug, matched_manifest)

    assert result.status == ValidationStatus.UNMATCHED
    assert result.gap_description is not None


def test__overall_status_with_all_matched() -> None:
    steps = [
        _make_validation_step("A001", ValidationStatus.MATCHED),
        _make_validation_step("A002", ValidationStatus.MATCHED),
    ]

    assert _overall_status(steps) == OverallValidationStatus.VALIDATED


def test__overall_status_with_one_unmatched() -> None:
    steps = [
        _make_validation_step("A001", ValidationStatus.MATCHED),
        _make_validation_step("A002", ValidationStatus.UNMATCHED),
    ]

    assert _overall_status(steps) == OverallValidationStatus.VALIDATION_FAILED


def test__overall_status_with_empty_steps() -> None:
    assert _overall_status([]) == OverallValidationStatus.VALIDATED
