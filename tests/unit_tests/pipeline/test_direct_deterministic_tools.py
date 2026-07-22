"""Unit tests for _DirectDeterministicTools, pinning ADR 0032's credential redaction offline.

The network seam is the `get` attribute of the third-party `requests` module object itself — orchestration holds
no local alias, so the monkeypatch mutates global state belonging to `requests` for the duration of each test.
That is accepted rather than preferred: a mounted transport adapter, the cleaner seam, is unreachable because
requests.get builds its own Session internally and never exposes it.

The fake is never handed the plaintext key by a test: it reads params["api_key"] — the value production code
put there — assembles the real request URL through requests' own preparation machinery, and raises the
connection error requests itself would raise. The plaintext therefore appears in the failure text only if
production put it there, and no assertion path holds it as a literal. _RecordedFredGet.raised_texts keeps the
raw pre-redaction message so the control test can prove the fake is still emitting the key; without that
control, neutering the fake would turn every redaction assertion trivially green instead of red.

Non-error arms return a real requests.Response with real JSON bytes, so response.json() runs real
deserialization, and the ValueError arm is obtained honestly from undecodable bytes rather than a raised stub.

Scope is the fetch_fred_series public boundary only, since these tests are the safety net for the queued
orchestration refactor; fetch_ticker_price has an obvious future home here but is out of scope today.
"""

import json

import pytest
import requests
from pydantic import SecretStr
from pytest import MonkeyPatch

from money_pit.pipeline import orchestration
from money_pit.pipeline.orchestration import _REDACTED_SECRET_PLACEHOLDER
from money_pit.pipeline.orchestration import _DirectDeterministicTools
from money_pit.pipeline.orchestration import _error_text_with_secret_redacted
from money_pit.schemas.fetch_result import FetchError
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue
from money_pit.schemas.fetch_result import NoData


_SENTINEL_FRED_API_KEY: str = "fred-key-Zq7Xn4tVp2"
"""A key that collides with no other substring of the request URL or the error text.

Redaction is a substring str.replace, so a key like "json" or "1" would rewrite unrelated parts of the
message and make the "plaintext absent" assertions pass for the wrong reason.
"""

_SERIES_ID: str = "CPILFESL"
_MISSING_KEY_REASON_PREFIX: str = "FRED API key not configured"
_FETCH_FAILED_REASON_PREFIX: str = "FRED fetch failed"
_UNPARSEABLE_REASON_PREFIX: str = "FRED value unparseable"

_OBSERVATION_VALUE: float = 313.245
_WELL_FORMED_BODY: bytes = json.dumps({"observations": [{"value": str(_OBSERVATION_VALUE)}]}).encode()
_UNDECODABLE_BODY: bytes = b"not json"

_EXPECTED_REQUEST_PARAMS: dict[str, str] = {
    "series_id": _SERIES_ID,
    "api_key": _SENTINEL_FRED_API_KEY,
    "file_type": "json",
    "sort_order": "desc",
    "limit": "1",
}


class _RecordedFredGet:
    """A stand-in for requests.get that records its calls and either raises a real ConnectionError or answers 200."""

    calls: list[dict[str, str]]
    raised_texts: list[str]
    body: bytes | None

    def __init__(self, body: bytes | None) -> None:
        self.calls = []
        self.raised_texts = []
        self.body = body

    def __call__(self, url: str, *, params: dict[str, str], timeout: int) -> requests.Response:
        self.calls.append(params)
        if self.body is None:
            prepared_url: str | None = requests.Request("GET", url, params=params).prepare().url
            error = requests.ConnectionError(
                f"HTTPSConnectionPool(host='api.stlouisfed.org', port=443): "
                f"Max retries exceeded with url: {prepared_url}"
            )
            self.raised_texts.append(str(error))
            raise error
        response = requests.Response()
        response.status_code = 200
        response.encoding = "utf-8"
        # Assigning _content is the standard way to build a Response with a body; requests exposes no public setter.
        response._content = self.body
        return response


@pytest.fixture
def fred_api_key() -> SecretStr:
    return SecretStr(_SENTINEL_FRED_API_KEY)


@pytest.fixture
def fred_get__body(request: pytest.FixtureRequest) -> bytes | None:
    return getattr(request, "param", _WELL_FORMED_BODY)


@pytest.fixture
def fred_get(monkeypatch: MonkeyPatch, fred_get__body: bytes | None) -> _RecordedFredGet:
    recorded = _RecordedFredGet(body=fred_get__body)
    monkeypatch.setattr(orchestration.requests, "get", recorded)
    return recorded


@pytest.fixture
def deterministic_tools(fred_api_key: SecretStr) -> _DirectDeterministicTools:
    return _DirectDeterministicTools(fred_api_key=fred_api_key)


@pytest.mark.parametrize("fred_get__body", [None], indirect=True)
def test_fetch_fred_series_with_connection_error_redacts_the_key_from_the_reason(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_FETCH_FAILED_REASON_PREFIX)
    assert _REDACTED_SECRET_PLACEHOLDER in result.reason
    assert _SENTINEL_FRED_API_KEY not in result.reason


@pytest.mark.parametrize("fred_get__body", [None], indirect=True)
def test_fetch_fred_series_with_connection_error_redacts_the_key_from_the_log(
    deterministic_tools: _DirectDeterministicTools,
    fred_get: _RecordedFredGet,
    loguru_warnings: list[str],
) -> None:
    _ = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert len(loguru_warnings) == 1
    assert _REDACTED_SECRET_PLACEHOLDER in loguru_warnings[0]
    assert _SENTINEL_FRED_API_KEY not in loguru_warnings[0]


@pytest.mark.parametrize("fred_get__body", [None], indirect=True)
def test_fetch_fred_series_with_connection_error_raises_key_bearing_text_before_redaction(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """Control: the seam really does emit the plaintext key, so the redaction assertions above cannot go vacuous.

    If a future edit stops the fake from putting params["api_key"] into the exception, this goes red first and
    names the cause, instead of the redaction tests silently passing against a message that never held a key.
    """
    _ = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert len(fred_get.raised_texts) == 1
    assert _SENTINEL_FRED_API_KEY in fred_get.raised_texts[0]


@pytest.mark.parametrize("fred_get__body", [_UNDECODABLE_BODY], indirect=True)
def test_fetch_fred_series_with_undecodable_body_returns_fetch_error(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """A body response.json() cannot decode takes the ValueError arm and returns FetchError with the fetch prefix.

    That is the whole claim: a 200 with an unusable payload is an error, not NoData. This does NOT pin the
    redaction — a real JSON-decode message reports an offending character position and never holds the key, so
    deleting the redactor leaves this green. The stronger property, that both members of the
    `except (RequestException, ValueError)` tuple leave through the redacted string, only becomes assertable once
    raise_for_status() lands and routes a key-bearing HTTPError into this same handler.
    """
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_FETCH_FAILED_REASON_PREFIX)
    assert _SENTINEL_FRED_API_KEY not in result.reason


def test_fetch_fred_series_with_no_key_configured_never_reaches_the_network(fred_get: _RecordedFredGet) -> None:
    tools = _DirectDeterministicTools(fred_api_key=None)

    result: FetchResult = tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_MISSING_KEY_REASON_PREFIX)
    assert fred_get.calls == []


@pytest.mark.parametrize(
    "fred_get__body",
    [
        pytest.param(b"[]", id="non_dict_body"),
        pytest.param(json.dumps({"error_message": "Bad Request."}).encode(), id="missing_observations"),
        pytest.param(json.dumps({"observations": []}).encode(), id="empty_observations"),
        pytest.param(json.dumps({"observations": ["not-a-mapping"]}).encode(), id="non_dict_observation"),
        pytest.param(json.dumps({"observations": [{"value": "."}]}).encode(), id="missing_value_sentinel"),
    ],
    indirect=True,
)
def test_fetch_fred_series_with_unusable_body_returns_no_data(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """The five arms are the parse-rejection axis: every shape the observation walk refuses maps to NoData."""
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, NoData)


def test_fetch_fred_series_with_well_formed_body_returns_the_parsed_value(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert result == FetchValue(value=_OBSERVATION_VALUE)


def test_fetch_fred_series_with_well_formed_body_sends_the_latest_observation_query(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """Pin the full request params, because a wrong-but-valid query fails silently with wrong data.

    sort_order="desc" with limit="1" is what makes the single returned observation the LATEST one. Drop
    sort_order and FRED answers with the OLDEST observation in the series: a well-formed 200, a parseable
    float, no error anywhere — a decades-stale macro reading feeding a capital decision. limit="1" carries the
    same weight in reverse. Pinning api_key to the sentinel also holds ADR 0032 decision #1: the SecretStr is
    unwrapped in the expression that transmits it, so the plaintext is what reaches the wire.
    """
    _ = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert fred_get.calls[0] == _EXPECTED_REQUEST_PARAMS


def test__DirectDeterministicTools_never_holds_the_key_as_a_run_lifetime_plaintext_attribute(
    deterministic_tools: _DirectDeterministicTools,
) -> None:
    """Pin ADR 0032 decision #1: the credential lives on the instance as SecretStr, never as a plain str.

    SecretStr's repr masks the value, so this goes red the moment a refactor stores an unwrapped copy on the
    object — the exposure the ADR closed, which redaction of the error text does not cover.
    """
    assert _SENTINEL_FRED_API_KEY not in repr(deterministic_tools)
    assert _SENTINEL_FRED_API_KEY not in str(vars(deterministic_tools))


@pytest.mark.parametrize(
    "fred_get__body", [json.dumps({"observations": [{"value": "abc"}]}).encode()], indirect=True
)
def test_fetch_fred_series_with_unparseable_value_returns_fetch_error(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_UNPARSEABLE_REASON_PREFIX)


def test__error_text_with_secret_redacted_with_one_occurrence() -> None:
    error = ValueError(f"failed for url: ?api_key={_SENTINEL_FRED_API_KEY}&file_type=json")

    redacted: str = _error_text_with_secret_redacted(error, SecretStr(_SENTINEL_FRED_API_KEY))

    assert redacted == f"failed for url: ?api_key={_REDACTED_SECRET_PLACEHOLDER}&file_type=json"


def test__error_text_with_secret_redacted_with_repeated_occurrences() -> None:
    error = ValueError(f"retry 1: {_SENTINEL_FRED_API_KEY}; retry 2: {_SENTINEL_FRED_API_KEY}")

    redacted: str = _error_text_with_secret_redacted(error, SecretStr(_SENTINEL_FRED_API_KEY))

    assert redacted == (
        f"retry 1: {_REDACTED_SECRET_PLACEHOLDER}; retry 2: {_REDACTED_SECRET_PLACEHOLDER}"
    )
    assert _SENTINEL_FRED_API_KEY not in redacted


def test__error_text_with_secret_redacted_with_absent_secret() -> None:
    """The common real case: a failure whose text never held the credential is passed through untouched."""
    error = ValueError("connection reset by peer")

    redacted: str = _error_text_with_secret_redacted(error, SecretStr(_SENTINEL_FRED_API_KEY))

    assert redacted == "connection reset by peer"


def test__error_text_with_secret_redacted_with_empty_secret() -> None:
    """An empty secret returns the text unchanged: "".replace would splice the placeholder between every character.

    An empty key is a config defect rather than a leak, so ADR 0032 fails it toward a readable diagnostic.
    """
    error = ValueError("connection reset by peer")

    redacted: str = _error_text_with_secret_redacted(error, SecretStr(""))

    assert redacted == "connection reset by peer"
