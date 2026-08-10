import pytest

from money_pit.secrets import ExecutionCredentialResolver
from money_pit.secrets import InferenceCredentialResolver
from money_pit.secrets import PortfolioCredentialResolver
from money_pit.secrets import ResearchCredentialResolver
from money_pit.secrets import SecretSpecExecutionResolver
from money_pit.secrets import SecretSpecInferenceResolver
from money_pit.secrets import SecretSpecPortfolioResolver
from money_pit.secrets import SecretSpecResearchResolver
from money_pit.secrets import SecretSpecResolver
from money_pit.secrets import SecretSpecSourceResolver
from money_pit.secrets import SourceCredentialResolver


_CREDENTIAL_METHODS = {
    "alpaca_execution",
    "alpaca_portfolio",
    "brave",
    "edgar",
    "fred",
    "imap",
    "openai",
    "youtube_media",
}


@pytest.mark.parametrize(
    ("protocol", "expected_methods"),
    [
        pytest.param(InferenceCredentialResolver, {"openai"}, id="inference"),
        pytest.param(SourceCredentialResolver, {"imap", "youtube_media"}, id="source"),
        pytest.param(ResearchCredentialResolver, {"brave", "edgar", "fred"}, id="research"),
        pytest.param(PortfolioCredentialResolver, {"alpaca_portfolio"}, id="portfolio"),
        pytest.param(ExecutionCredentialResolver, {"alpaca_execution"}, id="execution"),
    ],
)
def test_credential_resolver_protocol_exposes_only_its_capability(
    protocol: type[object],
    expected_methods: set[str],
) -> None:
    public_methods = _CREDENTIAL_METHODS.intersection(vars(protocol))

    assert public_methods == expected_methods


@pytest.mark.parametrize(
    ("resolver_type", "expected_methods"),
    [
        pytest.param(SecretSpecResolver, set[str](), id="base"),
        pytest.param(SecretSpecInferenceResolver, {"openai"}, id="inference"),
        pytest.param(SecretSpecSourceResolver, {"imap", "youtube_media"}, id="source"),
        pytest.param(SecretSpecResearchResolver, {"brave", "edgar", "fred"}, id="research"),
        pytest.param(SecretSpecPortfolioResolver, {"alpaca_portfolio"}, id="portfolio"),
        pytest.param(SecretSpecExecutionResolver, {"alpaca_execution"}, id="execution"),
    ],
)
def test_secret_spec_resolver_type_exposes_only_its_capability(
    resolver_type: type[SecretSpecResolver],
    expected_methods: set[str],
) -> None:
    public_methods = _CREDENTIAL_METHODS.intersection(vars(resolver_type))

    assert public_methods == expected_methods
