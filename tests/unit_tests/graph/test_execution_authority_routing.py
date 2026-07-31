"""Tests for routing legacy determinations under explicit execution authority."""

import pytest

from money_pit.graph.edges import EXECUTE
from money_pit.graph.edges import FINALIZE
from money_pit.graph.edges import NOTIFY
from money_pit.graph.graph import _authority_aware_determination_router
from money_pit.graph.state import PipelineState
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.execution_policy import ExecutionMode


def test__authority_aware_determination_router_preserves_non_execution_route() -> None:
    state = PipelineState(terminal_state=TerminalState.VALIDATION_ERROR)

    route = _authority_aware_determination_router(
        state,
        ExecutionMode.OBSERVE,
        legacy_injected_write_authority=False,
    )

    assert route == NOTIFY


@pytest.mark.parametrize(
    ("execution_mode", "legacy_authority", "expected"),
    [
        pytest.param(ExecutionMode.OBSERVE, False, FINALIZE, id="observe"),
        pytest.param(ExecutionMode.OBSERVE, True, FINALIZE, id="observe-with-legacy-writer"),
        pytest.param(ExecutionMode.APPROVAL_REQUIRED, False, FINALIZE, id="approval-without-injected-writer"),
        pytest.param(ExecutionMode.APPROVAL_REQUIRED, True, EXECUTE, id="approval-with-injected-writer"),
        pytest.param(ExecutionMode.AUTONOMOUS, False, FINALIZE, id="autonomous"),
    ],
)
def test__authority_aware_determination_router_with_executable_determination(
    execution_mode: ExecutionMode,
    legacy_authority: bool,
    expected: str,
) -> None:
    route = _authority_aware_determination_router(
        PipelineState(),
        execution_mode,
        legacy_injected_write_authority=legacy_authority,
    )

    assert route == expected
