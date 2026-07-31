"""Tests for the typed broker-environment bridge over the legacy paper flag."""

import pytest
from pydantic import SecretStr

from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode


def test_alpaca_credentials_with_no_environment_or_legacy_flag_raises() -> None:
    with pytest.raises(TypeError):
        _ = AlpacaCredentials(api_key="key", secret_key=SecretStr("secret"))


@pytest.mark.parametrize(
    ("paper", "expected"),
    [
        (True, BrokerEnvironment.PAPER),
        (False, BrokerEnvironment.LIVE),
    ],
)
def test_alpaca_credentials_with_legacy_flag_maps_environment(
    paper: bool,
    expected: BrokerEnvironment,
) -> None:
    credentials = AlpacaCredentials(
        api_key="key",
        secret_key=SecretStr("secret"),
        paper=paper,
    )

    assert credentials.broker_environment is expected


def test_alpaca_credentials_with_agreeing_typed_and_legacy_forms_succeeds() -> None:
    credentials = AlpacaCredentials(
        api_key="key",
        secret_key=SecretStr("secret"),
        broker_environment=BrokerEnvironment.PAPER,
        paper=True,
    )

    assert credentials.paper is True


def test_alpaca_credentials_with_conflicting_typed_and_legacy_forms_raises() -> None:
    with pytest.raises(ValueError, match="paper conflicts"):
        _ = AlpacaCredentials(
            api_key="key",
            secret_key=SecretStr("secret"),
            broker_environment=BrokerEnvironment.LIVE,
            paper=True,
        )


def test_config_defaults_to_approval_required_authority() -> None:
    config = Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=True,
    )

    assert config.execution_mode is ExecutionMode.APPROVAL_REQUIRED


@pytest.mark.parametrize(
    ("alpaca_paper", "expected"),
    [
        (True, BrokerEnvironment.PAPER),
        (False, BrokerEnvironment.LIVE),
    ],
)
def test_config_broker_environment_maps_legacy_boundary(
    alpaca_paper: bool,
    expected: BrokerEnvironment,
) -> None:
    config = Config(
        alpaca_service="alpaca",
        alpaca_username="key",
        alpaca_paper=alpaca_paper,
    )

    assert config.broker_environment is expected
