"""Tests for money_pit.email_sender — message construction and the fail-closed SMTP send seam.

The Gmail app password is resolved through a real in-memory keyring; smtplib.SMTP is replaced with a
hand-written fake context manager (no unittest.mock) so the send path runs offline. Assertions target the
EmailSendError / CredentialResolutionError TYPES and concrete header values, never message text.
"""

import smtplib
from collections.abc import Iterator
from email.message import EmailMessage
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pytest import FixtureRequest
from pytest import MonkeyPatch

from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.constants import UNDELIVERED_EMAIL_FILENAME_TEMPLATE
from money_pit.email_sender import EmailNotConfiguredError
from money_pit.email_sender import EmailSendError
from money_pit.email_sender import _build_message
from money_pit.email_sender import make_gmail_email_sender
from money_pit.email_sender import make_unconfigured_email_sender
from money_pit.email_sender import with_undelivered_record
from tests.conftest import UNCONFIGURED_EMAIL_REASON
from tests.unit_tests.conftest import CapturedLog
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


_RELAY_REJECTION: str = "relay rejected the message"


class _FailingSMTP(_FakeSMTP):
    """A fake SMTP whose send raises, exercising the EmailSendError translation."""

    def send_message(self, message: EmailMessage) -> None:
        raise smtplib.SMTPException(_RELAY_REJECTION)


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


_FIRST_ARTIFACT: str = UNDELIVERED_EMAIL_FILENAME_TEMPLATE.format(index=1)
_SECOND_ARTIFACT: str = UNDELIVERED_EMAIL_FILENAME_TEMPLATE.format(index=2)
_HALT_SUBJECT: str = "Recovery Halt"
_HALT_BODY: str = "A prior leg is still open."
_REASON_LINE_PREFIX: str = "Reason: "


class _NonEmailSendErrorSender:
    """A real EmailSender whose send fails with something other than EmailSendError."""

    def __call__(self, subject: str, body: str) -> None:
        raise RuntimeError("the working directory vanished")


def test_make_unconfigured_email_sender_raises_email_not_configured_error() -> None:
    sender: EmailSender = make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON)

    with pytest.raises(EmailNotConfiguredError):
        sender(_REPORT_SUBJECT, _REPORT_BODY)


def test_make_unconfigured_email_sender_is_caught_as_email_send_error() -> None:
    sender: EmailSender = make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON)

    with pytest.raises(EmailSendError):
        sender(_REPORT_SUBJECT, _REPORT_BODY)


@pytest.fixture
def delivered_working_dir(
    tmp_path: Path, config: Config, keyring_with_gmail_password: InMemoryKeyring, sent_messages: list[EmailMessage]
) -> Path:
    sender: EmailSender = with_undelivered_record(make_gmail_email_sender(config), tmp_path)
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    return tmp_path


def test_with_undelivered_record_with_successful_send_delivers_message(
    delivered_working_dir: Path, sent_messages: list[EmailMessage]
) -> None:
    assert len(sent_messages) == 1


def test_with_undelivered_record_with_successful_send_writes_no_artifact(delivered_working_dir: Path) -> None:
    assert list(delivered_working_dir.iterdir()) == []


@pytest.fixture
def undelivered_working_dir(
    tmp_path: Path, config: Config, keyring_with_gmail_password: InMemoryKeyring, failing_smtp: None
) -> Path:
    sender: EmailSender = with_undelivered_record(make_gmail_email_sender(config), tmp_path)
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    return tmp_path


def test_with_undelivered_record_with_failing_send_writes_artifact(undelivered_working_dir: Path) -> None:
    assert (undelivered_working_dir / _FIRST_ARTIFACT).is_file()


def test_with_undelivered_record_artifact_carries_subject_and_body(undelivered_working_dir: Path) -> None:
    recorded: str = (undelivered_working_dir / _FIRST_ARTIFACT).read_text(encoding="utf-8")

    assert _REPORT_SUBJECT in recorded
    assert _REPORT_BODY in recorded


@pytest.fixture
def twice_undelivered_working_dir(
    tmp_path: Path, config: Config, keyring_with_gmail_password: InMemoryKeyring, failing_smtp: None
) -> Path:
    sender: EmailSender = with_undelivered_record(make_gmail_email_sender(config), tmp_path)
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    sender(_HALT_SUBJECT, _HALT_BODY)
    return tmp_path


def test_with_undelivered_record_with_two_failures_numbers_artifacts(
    twice_undelivered_working_dir: Path,
) -> None:
    assert sorted(path.name for path in twice_undelivered_working_dir.iterdir()) == [
        _FIRST_ARTIFACT,
        _SECOND_ARTIFACT,
    ]


def test_with_undelivered_record_with_two_failures_keeps_each_message(
    twice_undelivered_working_dir: Path,
) -> None:
    assert _REPORT_BODY in (twice_undelivered_working_dir / _FIRST_ARTIFACT).read_text(encoding="utf-8")
    assert _HALT_BODY in (twice_undelivered_working_dir / _SECOND_ARTIFACT).read_text(encoding="utf-8")


@pytest.fixture
def two_wrappers_working_dir(tmp_path: Path) -> Path:
    first: EmailSender = with_undelivered_record(make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), tmp_path)
    second: EmailSender = with_undelivered_record(make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), tmp_path)
    first(_REPORT_SUBJECT, _REPORT_BODY)
    second(_HALT_SUBJECT, _HALT_BODY)
    return tmp_path


def test_with_undelivered_record_with_two_wrappers_numbers_artifacts(two_wrappers_working_dir: Path) -> None:
    assert sorted(path.name for path in two_wrappers_working_dir.iterdir()) == [_FIRST_ARTIFACT, _SECOND_ARTIFACT]


def test_with_undelivered_record_with_two_wrappers_keeps_each_message(two_wrappers_working_dir: Path) -> None:
    assert _REPORT_BODY in (two_wrappers_working_dir / _FIRST_ARTIFACT).read_text(encoding="utf-8")
    assert _HALT_BODY in (two_wrappers_working_dir / _SECOND_ARTIFACT).read_text(encoding="utf-8")


def test_with_undelivered_record_with_other_error_propagates(tmp_path: Path) -> None:
    sender: EmailSender = with_undelivered_record(_NonEmailSendErrorSender(), tmp_path)

    with pytest.raises(RuntimeError):
        sender(_REPORT_SUBJECT, _REPORT_BODY)

    assert list(tmp_path.iterdir()) == []


def _reason_line(artifact_path: Path) -> str:
    lines: list[str] = artifact_path.read_text(encoding="utf-8").splitlines()
    return next(line for line in lines if line.startswith(_REASON_LINE_PREFIX))


@pytest.fixture
def unconfigured_working_dir(tmp_path: Path) -> Path:
    sender: EmailSender = with_undelivered_record(make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), tmp_path)
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    return tmp_path


def test_with_undelivered_record_artifact_carries_unconfigured_reason(unconfigured_working_dir: Path) -> None:
    assert UNCONFIGURED_EMAIL_REASON in _reason_line(unconfigured_working_dir / _FIRST_ARTIFACT)


def test_with_undelivered_record_artifact_carries_rejection_reason(undelivered_working_dir: Path) -> None:
    assert _RELAY_REJECTION in _reason_line(undelivered_working_dir / _FIRST_ARTIFACT)


@pytest.fixture
def recorded_email_logs(loguru_records: list[CapturedLog], tmp_path: Path) -> list[CapturedLog]:
    sender: EmailSender = with_undelivered_record(make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), tmp_path)
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    return loguru_records


def test_with_undelivered_record_logs_the_artifact_path(recorded_email_logs: list[CapturedLog]) -> None:
    assert [record for record in recorded_email_logs if record.level == "ERROR" and _FIRST_ARTIFACT in record.message]


def test_with_undelivered_record_keeps_the_body_out_of_the_log(recorded_email_logs: list[CapturedLog]) -> None:
    assert all(_REPORT_BODY not in record.message for record in recorded_email_logs)


_FILE_IN_PLACE_OF_DIR_NAME: str = "not_a_directory"
_NEVER_CREATED_DIR_NAME: str = "never_created"


@pytest.fixture(params=[_FILE_IN_PLACE_OF_DIR_NAME, _NEVER_CREATED_DIR_NAME])
def unwritable_working_dir(request: FixtureRequest, tmp_path: Path) -> Path:
    """A working_dir the artifact write must fail against — a plain file, or a directory that was never created."""
    unwritable: Path = tmp_path / str(request.param)
    if request.param == _FILE_IN_PLACE_OF_DIR_NAME:
        _ = unwritable.write_text("this is a file, not a run directory", encoding="utf-8")
    return unwritable


def test_with_undelivered_record_with_unwritable_working_dir_returns_normally(unwritable_working_dir: Path) -> None:
    sender: EmailSender = with_undelivered_record(
        make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), unwritable_working_dir
    )

    assert sender(_REPORT_SUBJECT, _REPORT_BODY) is None


@pytest.fixture
def unwritable_record_logs(loguru_records: list[CapturedLog], unwritable_working_dir: Path) -> list[CapturedLog]:
    sender: EmailSender = with_undelivered_record(
        make_unconfigured_email_sender(UNCONFIGURED_EMAIL_REASON), unwritable_working_dir
    )
    sender(_REPORT_SUBJECT, _REPORT_BODY)
    return loguru_records


def test_with_undelivered_record_with_unwritable_working_dir_logs_the_body(
    unwritable_record_logs: list[CapturedLog],
) -> None:
    assert [record for record in unwritable_record_logs if record.level == "ERROR" and _REPORT_BODY in record.message]


def test_with_undelivered_record_with_unwritable_working_dir_writes_no_artifact(
    unwritable_record_logs: list[CapturedLog], unwritable_working_dir: Path
) -> None:
    assert not unwritable_working_dir.is_dir()
    assert not (unwritable_working_dir.parent / _FIRST_ARTIFACT).exists()
