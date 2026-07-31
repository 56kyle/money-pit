"""Tests for direct Brave-search result validation and failure handling."""

from dataclasses import dataclass

import pytest
import requests
from pydantic import SecretStr
from pytest import MonkeyPatch

from money_pit.pipeline import orchestration
from money_pit.pipeline.orchestration import _DirectOpenEndedTools


@dataclass
class _Response:
    payload: object

    def json(self) -> object:
        return self.payload


_DEFAULT_API_KEY = SecretStr("secret")


def _tools(api_key: SecretStr | None = _DEFAULT_API_KEY) -> _DirectOpenEndedTools:
    return _DirectOpenEndedTools(api_key, "owner@example.com")


def test_brave_search_with_no_api_key_returns_empty() -> None:
    assert _tools(None).brave_search("query") == []


def test_brave_search_with_request_failure_returns_empty(
    monkeypatch: MonkeyPatch,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> object:
        raise requests.RequestException("offline")

    monkeypatch.setattr(orchestration.requests, "get", fail)

    assert _tools().brave_search("query") == []


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"web": []},
        {"web": {"results": "invalid"}},
    ],
)
def test_brave_search_with_malformed_response_returns_empty(
    monkeypatch: MonkeyPatch,
    payload: object,
) -> None:
    monkeypatch.setattr(
        orchestration.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload),
    )

    assert _tools().brave_search("query") == []


def test_brave_search_with_valid_results_filters_invalid_entries(
    monkeypatch: MonkeyPatch,
) -> None:
    payload = {
        "web": {
            "results": [
                {"title": "Primary", "description": "Evidence"},
                "invalid",
                {"title": 7, "description": "wrong type"},
                {"title": "Second", "description": "Support"},
            ]
        }
    }
    monkeypatch.setattr(
        orchestration.requests,
        "get",
        lambda *_args, **_kwargs: _Response(payload),
    )

    result = _tools().brave_search("query", n_results=4)

    assert result == ["Primary: Evidence", "Second: Support"]
