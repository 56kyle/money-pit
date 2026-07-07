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


def test_send_email_with_success(configured_env: None, sent_messages: list[EmailMessage]) -> None:
    result = send_email("owner@example.com", "the subject", "the body")

    assert result == "Sent email to owner@example.com."
    assert len(sent_messages) == 1
    assert sent_messages[0]["From"] == "sender@gmail.com"
    assert sent_messages[0]["To"] == "owner@example.com"
    assert sent_messages[0]["Subject"] == "the subject"


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
