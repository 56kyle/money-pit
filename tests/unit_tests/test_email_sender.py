"""Tests for money_pit.email_sender — message construction and the fail-closed SMTP send seam.

The Gmail app password is resolved through a real in-memory keyring; smtplib.SMTP is replaced with a
hand-written fake context manager (no unittest.mock) so the send path runs offline. Assertions target the
EmailSendError / CredentialResolutionError TYPES and concrete header values, never message text.
"""

import smtplib
from collections.abc import Iterator
from email.message import EmailMessage
from typing import TYPE_CHECKING

import pytest
from pytest import FixtureRequest
from pytest import MonkeyPatch

from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.email_sender import EmailSendError
from money_pit.email_sender import _build_message
from money_pit.email_sender import make_gmail_email_sender
from tests.unit_tests.conftest import InMemoryKeyring


if TYPE_CHECKING:
    from money_pit.contracts import EmailSender


class _FakeSMTP:
    """A hand-written smtplib.SMTP stand-in that records sent messages and never touches the network."""

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
    """A fake SMTP whose send raises, exercising the EmailSendError translation."""

    def send_message(self, message: EmailMessage) -> None:
        raise smtplib.SMTPException("relay rejected the message")


@pytest.fixture
def config__gmail_address(request: FixtureRequest) -> str | None:
    return getattr(request, "param", "sender@gmail.com")


@pytest.fixture
def config__gmail_app_password() -> str:
    return "gmail-app-password"


@pytest.fixture
def config(config__gmail_address: str | None) -> Config:
    return Config(
        alpaca_service="alpaca-paper",
        alpaca_username="alpaca-api-key",
        alpaca_paper=True,
        gmail_address=config__gmail_address,
    )


@pytest.fixture
def keyring_with_gmail_password(
    config: Config, config__gmail_app_password: str, in_memory_keyring: InMemoryKeyring
) -> InMemoryKeyring:
    if config.gmail_address is not None:
        in_memory_keyring.set_password(config.gmail_service, config.gmail_address, config__gmail_app_password)
    return in_memory_keyring


@pytest.fixture
def sent_messages(monkeypatch: MonkeyPatch) -> Iterator[list[EmailMessage]]:
    sent: list[EmailMessage] = []
    monkeypatch.setattr(smtplib, "SMTP", lambda _host, _port: _FakeSMTP(sent))
    return sent


@pytest.fixture
def failing_smtp(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", lambda _host, _port: _FailingSMTP([]))


def test__build_message_sets_headers() -> None:
    message = _build_message("from@gmail.com", "to@example.com", "the subject", "the body")

    assert message["From"] == "from@gmail.com"
    assert message["To"] == "to@example.com"
    assert message["Subject"] == "the subject"


_REPORT_SUBJECT: str = "Daily report"
_REPORT_BODY: str = "All positions nominal."


@pytest.fixture
def sent_report(
    config: Config, keyring_with_gmail_password: InMemoryKeyring, sent_messages: list[EmailMessage]
) -> list[EmailMessage]:
    sender: EmailSender = make_gmail_email_sender(config)
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    return sent_messages


def test_send_email_with_success(sent_report: list[EmailMessage]) -> None:
    assert len(sent_report) == 1


def test_send_email_addresses_owner_recipient(sent_report: list[EmailMessage], config: Config) -> None:
    assert sent_report[0]["To"] == config.owner_recipient


def test_send_email_sets_subject(sent_report: list[EmailMessage]) -> None:
    assert sent_report[0]["Subject"] == _REPORT_SUBJECT


def test_send_email_with_smtp_failure(
    config: Config, keyring_with_gmail_password: InMemoryKeyring, failing_smtp: None
) -> None:
    sender: EmailSender = make_gmail_email_sender(config)

    with pytest.raises(EmailSendError):
        sender("Daily report", "All positions nominal.")


@pytest.mark.parametrize("config__gmail_address", [None], indirect=True)
def test_make_gmail_email_sender_with_no_address(config: Config) -> None:
    with pytest.raises(CredentialResolutionError):
        _ = make_gmail_email_sender(config)
