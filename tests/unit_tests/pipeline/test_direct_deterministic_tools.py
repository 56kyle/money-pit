"""Unit tests for _DirectDeterministicTools, pinning ADR 0032 decisions #1 and #2 offline.

Decision #2 is the invariant these tests exist for: no upstream exception text reaches FetchError.reason,
which flows into Answer.answer and from there to disk, the LLM provider, and the owner email. The reason
names the subject and the failure class only. The full exception is not discarded — it is relocated to the
local logger.warning, which is trusted because it is local — so the log half is pinned alongside the reason
half. Asserting only the reason half would leave "relocated, not discarded" unasserted, and a change that
simply deleted the logging would read as correct.

The network seam is the `get` attribute of the third-party `requests` module object itself — orchestration holds
no local alias, so the monkeypatch mutates global state belonging to `requests` for the duration of each test.
That is accepted rather than preferred: a mounted transport adapter, the cleaner seam, is unreachable because
requests.get builds its own Session internally and never exposes it.

The fake is never handed the plaintext key by a test: it reads params["api_key"] — the value production code
put there — assembles the real request URL through requests' own preparation machinery, and raises the
connection error requests itself would raise. The plaintext therefore appears in the failure text only if
production put it there, and no assertion path holds it as a literal. _RecordedFredGet.raised_texts keeps the
raised message so the control test can prove the fake is still emitting the key; without that control,
neutering the fake would turn every "absent from the reason" assertion trivially green instead of red.

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
from money_pit.pipeline.orchestration import _DirectDeterministicTools
from money_pit.schemas.fetch_result import FetchError
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue
from money_pit.schemas.fetch_result import NoData


_SENTINEL_FRED_API_KEY: str = "fred-key-Zq7Xn4tVp2"
"""A key that collides with no other substring of the request URL, the error text, or the reason.

A key like "json" or "1" would appear in the reason for reasons unrelated to the credential and make the
"plaintext absent" assertions fail for the wrong reason — or, worse, pass for it.
"""

_SERIES_ID: str = "CPILFESL"
_MISSING_KEY_REASON_PREFIX: str = "FRED API key not configured"
_FETCH_FAILED_REASON_PREFIX: str = "FRED fetch failed"
_UNPARSEABLE_REASON_PREFIX: str = "FRED value unparseable"

_CONNECTION_ERROR_TYPE_NAME: str = requests.ConnectionError.__name__

_UNPARSEABLE_VALUE: str = "abc"
"""An observation float() rejects, distinctive enough that its repr cannot appear in the reason by accident."""

_CONNECTION_ERROR_FRAGMENT: str = "Max retries exceeded with url"
"""A distinctive substring of the fake's exception text, present nowhere in a compliant reason.

This is what discriminates the new contract from the old one: a reason built with `{error}` carries this
fragment (and the key-bearing URL following it), a reason built with `type(error).__name__` cannot.
"""

_FLOAT_ERROR_FRAGMENT: str = "could not convert string to float"
_JSON_ERROR_FRAGMENT: str = "Expecting value"

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
def test_fetch_fred_series_with_connection_error_keeps_upstream_text_out_of_the_reason(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """ADR 0032 decision #2: the reason names the series and the failure class, and nothing from the exception.

    The positive half matters as much as the negative one. Asserting only that the key is absent would stay
    green under a reason that named some other part of the untrusted text; asserting the reason holds the
    series id and the exception's type name pins the shape the handler actually promises.
    """
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_FETCH_FAILED_REASON_PREFIX)
    assert _SERIES_ID in result.reason
    assert _CONNECTION_ERROR_TYPE_NAME in result.reason
    assert _SENTINEL_FRED_API_KEY not in result.reason
    assert _CONNECTION_ERROR_FRAGMENT not in result.reason


@pytest.mark.parametrize("fred_get__body", [None], indirect=True)
def test_fetch_fred_series_with_connection_error_relocates_the_detail_to_the_log(
    deterministic_tools: _DirectDeterministicTools,
    fred_get: _RecordedFredGet,
    loguru_warnings: list[str],
) -> None:
    """Both halves of "relocated, not discarded" in one place: the log keeps the exception text, the reason keeps none.

    Pinning the halves together is what makes a future change that drops the logging fail: with only the
    reason-side assertion, deleting the logger.warning would look like an improvement rather than the loss of
    the operator's only diagnostic. What the log does with the key-bearing URL is deliberately left unpinned —
    ADR 0032 accepts that exposure as residual risk rather than promising it, and a test asserting the
    plaintext is present would turn a later tightening of the local log into a red test defending the leak.
    """
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert len(loguru_warnings) == 1
    assert _CONNECTION_ERROR_FRAGMENT in loguru_warnings[0]
    assert isinstance(result, FetchError)
    assert _CONNECTION_ERROR_FRAGMENT not in result.reason
    assert _SENTINEL_FRED_API_KEY not in result.reason


@pytest.mark.parametrize("fred_get__body", [None], indirect=True)
def test_fetch_fred_series_with_connection_error_raises_key_bearing_text_at_the_seam(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """Control: the seam really does emit the plaintext key, so the "absent from the reason" assertions cannot go vacuous.

    If a future edit stops the fake from putting params["api_key"] into the exception, this goes red first and
    names the cause, instead of the invariant tests silently passing against a message that never held a key.
    """
    _ = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert len(fred_get.raised_texts) == 1
    assert _SENTINEL_FRED_API_KEY in fred_get.raised_texts[0]


@pytest.mark.parametrize("fred_get__body", [_UNDECODABLE_BODY], indirect=True)
def test_fetch_fred_series_with_undecodable_body_returns_fetch_error(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """A body response.json() cannot decode takes the ValueError arm and returns FetchError with the fetch prefix.

    The second member of the `except (RequestException, ValueError)` tuple witnesses the same invariant as the
    first: the handler discards the text unconditionally, so the JSON decoder's own message is absent too. That
    the decode message happens to hold no credential is beside the point — the rule is about the text, not the
    secret.
    """
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_FETCH_FAILED_REASON_PREFIX)
    assert _SENTINEL_FRED_API_KEY not in result.reason
    assert _JSON_ERROR_FRAGMENT not in result.reason


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
    object — a run-lifetime exposure independent of, and uncovered by, the decision #2 rule about error text.
    """
    assert _SENTINEL_FRED_API_KEY not in repr(deterministic_tools)
    assert _SENTINEL_FRED_API_KEY not in str(vars(deterministic_tools))


@pytest.mark.parametrize(
    "fred_get__body", [json.dumps({"observations": [{"value": _UNPARSEABLE_VALUE}]}).encode()], indirect=True
)
def test_fetch_fred_series_with_unparseable_value_returns_fetch_error(
    deterministic_tools: _DirectDeterministicTools, fred_get: _RecordedFredGet
) -> None:
    """The float() handler names the rejected observation itself, and still carries no exception text.

    The observation comes from the response body, never the request, so it cannot hold the credential; naming
    it says strictly more than the exception's type name, which this handler's single-member `except` had
    already fixed to "ValueError".
    """
    result: FetchResult = deterministic_tools.fetch_fred_series(_SERIES_ID)

    assert isinstance(result, FetchError)
    assert result.reason.startswith(_UNPARSEABLE_REASON_PREFIX)
    assert _SERIES_ID in result.reason
    assert repr(_UNPARSEABLE_VALUE) in result.reason
    assert _FLOAT_ERROR_FRAGMENT not in result.reason
