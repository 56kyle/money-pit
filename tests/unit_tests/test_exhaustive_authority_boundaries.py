"""Fail-closed tests for exhaustive typed authority boundaries."""

from typing import TYPE_CHECKING
from typing import cast

import pytest

from money_pit.graph.graph import _authority_aware_determination_router
from money_pit.graph.state import PipelineState
from money_pit.mcp.clients import _paper_flag


if TYPE_CHECKING:
    from money_pit.schemas.execution_policy import BrokerEnvironment
    from money_pit.schemas.execution_policy import ExecutionMode


def test__authority_aware_determination_router_with_invalid_mode_fails_closed() -> None:
    invalid_mode = cast("ExecutionMode", object())

    with pytest.raises(AssertionError):
        _ = _authority_aware_determination_router(
            PipelineState(),
            invalid_mode,
            legacy_injected_write_authority=False,
        )


def test__paper_flag_with_invalid_environment_fails_closed() -> None:
    invalid_environment = cast("BrokerEnvironment", object())

    with pytest.raises(AssertionError):
        _ = _paper_flag(invalid_environment)
