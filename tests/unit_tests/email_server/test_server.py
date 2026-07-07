"""Tests for the email_server FastMCP tool — send composition, config gate, and failure surfacing.

email_server single-sources its SMTP defaults and EmailSendError from money_pit (per ADR 0010, superseding the
ADR 0009 isolation invariant). smtplib.SMTP is replaced with a hand-written fake (no unittest.mock) and
configuration is supplied via real environment variables. Assertions target the EmailServerConfigError /
EmailSendError TYPES and header values.
"""

import smtplib
from collections.abc import Iterator
from email.message import EmailMessage

import pytest
from pytest import MonkeyPatch

from email_server.server import EmailSendError
from email_server.server import EmailServerConfigError
from email_server.server import _env_port
from email_server.server import send_email


class _FakeSMTP:
    """A hand-written smtplib.SMTP stand-in recording sent messages, never touching the network."""

    def __init__(self, sent: list[EmailMessage]) -> None:
        self._sent = sent

    def __enter__(self) -> "_FakeSMTP":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def starttls(self) -> None:
        return None

    def login(self, _user: str, _password: str) -> None:
        return None

    def send_message(self, message: EmailMessage) -> None:
        self._sent.append(message)


class _FailingSMTP(_FakeSMTP):
    def send_message(self, message: EmailMessage) -> None:
        raise smtplib.SMTPException("relay rejected the message")


@pytest.fixture
def configured_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MONEY_PIT__GMAIL_ADDRESS", "sender@gmail.com")
    monkeypatch.setenv("MONEY_PIT__GMAIL_APP_PASSWORD", "app-password")


@pytest.fixture
def sent_messages(monkeypatch: MonkeyPatch) -> Iterator[list[EmailMessage]]:
    sent: list[EmailMessage] = []
    monkeypatch.setattr(smtplib, "SMTP", lambda _host, _port: _FakeSMTP(sent))
    yield sent


@pytest.fixture
def failing_smtp(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", lambda _host, _port: _FailingSMTP([]))


_RECIPIENT: str = "owner@example.com"
_SUBJECT: str = "the subject"
_BODY: str = "the body"
_SENDER_ADDRESS: str = "sender@gmail.com"
_EXPECTED_CONFIRMATION: str = f"Sent email to {_RECIPIENT}."


@pytest.fixture
def send_result(configured_env: None, sent_messages: list[EmailMessage]) -> tuple[str, list[EmailMessage]]:
    result = send_email(_RECIPIENT, _SUBJECT, _BODY)
    return result, sent_messages


def test_send_email_with_success(send_result: tuple[str, list[EmailMessage]]) -> None:
    result, _ = send_result
    assert result == _EXPECTED_CONFIRMATION


def test_send_email_sends_one_message(send_result: tuple[str, list[EmailMessage]]) -> None:
    _, sent = send_result
    assert len(sent) == 1


@pytest.mark.parametrize(
    ("header", "expected"),
    [("From", _SENDER_ADDRESS), ("To", _RECIPIENT), ("Subject", _SUBJECT)],
)
def test_send_email_composes_header(
    send_result: tuple[str, list[EmailMessage]], header: str, expected: str
) -> None:
    _, sent = send_result
    assert sent[0][header] == expected


def test_send_email_with_smtp_failure(configured_env: None, failing_smtp: None) -> None:
    with pytest.raises(EmailSendError):
        _ = send_email("owner@example.com", "the subject", "the body")


def test_send_email_with_unset_env(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MONEY_PIT__GMAIL_ADDRESS", raising=False)

    with pytest.raises(EmailServerConfigError):
        _ = send_email("owner@example.com", "the subject", "the body")


def test__env_port_with_valid(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MONEY_PIT__SMTP_PORT", "2525")

    assert _env_port("MONEY_PIT__SMTP_PORT", 587) == 2525


def test__env_port_with_unset(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("MONEY_PIT__SMTP_PORT", raising=False)

    assert _env_port("MONEY_PIT__SMTP_PORT", 587) == 587


def test__env_port_with_non_numeric(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setenv("MONEY_PIT__SMTP_PORT", "not-a-port")

    with pytest.raises(EmailServerConfigError):
        _ = _env_port("MONEY_PIT__SMTP_PORT", 587)
