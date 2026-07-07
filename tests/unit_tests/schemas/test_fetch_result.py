"""Tests for money_pit.schemas.fetch_result — the closed deterministic-fetch union (wave 1).

Pins the three distinct outcomes a deterministic fetch may report — a concrete value, a clean
empty response, and an upstream failure — as separate frozen types that a caller can dispatch on.
The point of the union is that FetchError is never conflated with NoData, so the round-trip test
asserts a match statement routes each variant to its own arm.
"""

import pytest
from pydantic import ValidationError

from money_pit.schemas.fetch_result import FetchError
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue
from money_pit.schemas.fetch_result import NoData


def test_fetch_value_with_valid() -> None:
    assert FetchValue(value=1.23).value == 1.23


def test_fetch_error_with_valid() -> None:
    assert FetchError(reason="upstream 500").reason == "upstream 500"


def test_no_data_with_valid() -> None:
    assert isinstance(NoData(), NoData)


def test_fetch_value_with_mutation_raises() -> None:
    value = FetchValue(value=1.23)
    with pytest.raises(ValidationError):
        value.value = 4.56  # type: ignore[misc]


def test_fetch_error_with_mutation_raises() -> None:
    error = FetchError(reason="upstream 500")
    with pytest.raises(ValidationError):
        error.reason = "different"  # type: ignore[misc]


def test_fetch_value_with_extra_field_raises() -> None:
    with pytest.raises(ValidationError):
        _ = FetchValue(value=1.23, reason="not allowed")  # type: ignore[call-arg]


def test_fetch_error_with_missing_reason_raises() -> None:
    with pytest.raises(ValidationError):
        _ = FetchError()  # type: ignore[call-arg]


_VALUE_ARM = "value"
_ERROR_ARM = "error"
_NO_DATA_ARM = "no_data"


def _dispatch(result: FetchResult) -> str:
    match result:
        case FetchValue():
            return _VALUE_ARM
        case FetchError():
            return _ERROR_ARM
        case NoData():
            return _NO_DATA_ARM


@pytest.mark.parametrize(
    ("result", "expected_arm"),
    [
        (FetchValue(value=1.23), _VALUE_ARM),
        (FetchError(reason="upstream 500"), _ERROR_ARM),
        (NoData(), _NO_DATA_ARM),
    ],
)
def test_fetch_result_with_variant_dispatches_distinctly(result: FetchResult, expected_arm: str) -> None:
    assert _dispatch(result) == expected_arm
