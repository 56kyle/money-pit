"""Tests for money_pit.compute.tool_map — the ACTION_TYPE_TO_TOOL routing invariant.

Pins the map-totality invariant (ADR 0004): because ACTION_TYPE_TO_TOOL is total over
ActionType, the per-step tool lookup in A5 cannot KeyError for a validly-parsed step, so
the reachable capability-failure paths are a manifest that omits the tool or an unavailable
manifest — never a failed lookup. This is why A5's tool-lookup path is not faked in a test.
"""

import pytest

from money_pit.compute.execution_params import _BUY_SIDES
from money_pit.compute.tool_map import ACTION_TYPE_TO_TOOL
from money_pit.compute.tool_map import COMPENSATING_ACTION
from money_pit.schemas.enums import ActionType


def test_action_type_to_tool_is_total() -> None:
    assert set(ACTION_TYPE_TO_TOOL) == set(ActionType)


def test_compensating_action_is_total() -> None:
    assert set(COMPENSATING_ACTION) == set(ActionType)


@pytest.mark.parametrize("action_type", list(ActionType))
def test_compensating_action_opposes_side(action_type: ActionType) -> None:
    compensating: ActionType = COMPENSATING_ACTION[action_type]

    assert (action_type in _BUY_SIDES) != (compensating in _BUY_SIDES)
