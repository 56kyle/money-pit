"""Module exposing a Gmail-backed EmailSender and its undelivered-record fallback for the money_pit package."""

import smtplib
from datetime import datetime
from datetime import timezone
from email.message import EmailMessage
from pathlib import Path

from loguru import logger
from pydantic import SecretStr

from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import resolve_gmail_app_password
from money_pit.constants import UNDELIVERED_EMAIL_FILENAME_GLOB
from money_pit.constants import UNDELIVERED_EMAIL_FILENAME_TEMPLATE
from money_pit.contracts import EmailSender


class EmailSendError(Exception):
    """Raised when an outgoing email cannot be delivered — SMTP failure, connection failure, or auth rejection."""


class EmailNotConfiguredError(EmailSendError):
    """Raised when an email cannot be sent because no Gmail account is configured for this run."""


def _build_message(sender_address: str, recipient: str, subject: str, body: str) -> EmailMessage:
    """Return an EmailMessage addressed from the Gmail account to the owner recipient."""
    message: EmailMessage = EmailMessage()
    message["From"] = sender_address
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return message


def _log_send_failure(subject: str, recipient: str, error: Exception) -> None:
    """Log the full upstream failure locally, this log being the only surviving copy of its text."""
    logger.warning(
        "Failed to send email {subject!r} to {recipient!r}: {error}",
        subject=subject,
        recipient=recipient,
        error=error,
    )


def make_gmail_email_sender(config: Config) -> EmailSender:
    """Return an EmailSender that delivers to the owner recipient via Gmail SMTP with STARTTLS.

    Raises CredentialResolutionError when no gmail_address is configured or no app password can be resolved.
    """
    if config.gmail_address is None:
        raise CredentialResolutionError("No gmail_address configured; cannot build a Gmail email sender.")
    sender_address: str = config.gmail_address
    app_password: SecretStr = resolve_gmail_app_password(config)

    def send_email(subject: str, body: str) -> None:
        message: EmailMessage = _build_message(sender_address, config.owner_recipient, subject, body)
        try:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
                _ = server.starttls()
                _ = server.login(sender_address, app_password.get_secret_value())
                _ = server.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            _log_send_failure(subject, config.owner_recipient, error)
            raise EmailSendError(
                f"Failed to send email {subject!r} to {config.owner_recipient!r}: {type(error).__name__}."
            ) from error

    return send_email


def make_unconfigured_email_sender(reason: str) -> EmailSender:
    """Return an EmailSender whose every send fails with EmailNotConfiguredError carrying reason."""

    def send_email(subject: str, body: str) -> None:
        raise EmailNotConfiguredError(f"Email is not configured, so {subject!r} cannot be sent: {reason}")

    return send_email


def _next_undelivered_email_path(working_dir: Path) -> Path:
    """Return the lowest-numbered undelivered-email artifact path not already taken in working_dir."""
    taken: set[str] = {path.name for path in working_dir.glob(UNDELIVERED_EMAIL_FILENAME_GLOB)}
    index: int = 1
    while UNDELIVERED_EMAIL_FILENAME_TEMPLATE.format(index=index) in taken:
        index += 1
    return working_dir / UNDELIVERED_EMAIL_FILENAME_TEMPLATE.format(index=index)


def _write_undelivered_email_artifact(working_dir: Path, subject: str, body: str, reason: str) -> Path:
    """Write an undelivered email to the run's working directory and return the artifact path."""
    artifact_path: Path = _next_undelivered_email_path(working_dir)
    lines: list[str] = [
        f"Undelivered at: {datetime.now(timezone.utc).isoformat()}",
        f"Reason: {reason}",
        f"Subject: {subject}",
        "",
        body,
    ]
    _ = artifact_path.write_text("\n".join(lines), encoding="utf-8")
    return artifact_path


def _log_undelivered_email_held_by_artifact(subject: str, reason: str, artifact_path: Path) -> None:
    """Log an undelivered email without its body, which the named artifact holds instead."""
    logger.error(
        "Email {subject!r} could not be delivered ({reason}); recorded at {artifact_path}",
        subject=subject,
        reason=reason,
        artifact_path=str(artifact_path),
    )


def _log_undelivered_email_with_inline_body(subject: str, body: str, reason: str, write_error: OSError) -> None:
    """Log an undelivered email carrying its full body, this log being the only surviving copy of the message."""
    logger.error(
        "Email {subject!r} could not be delivered ({reason}) and no artifact could be written ({write_error}); "
        "full body follows: {body}",
        subject=subject,
        reason=reason,
        write_error=str(write_error),
        body=body,
    )


def _record_undelivered_email(working_dir: Path, subject: str, body: str, reason: str) -> None:
    """Record an undelivered email as a working-directory artifact, degrading to a body-carrying log if that fails."""
    try:
        artifact_path: Path = _write_undelivered_email_artifact(working_dir, subject, body, reason)
    except OSError as write_error:
        _log_undelivered_email_with_inline_body(subject, body, reason, write_error)
        return
    _log_undelivered_email_held_by_artifact(subject, reason, artifact_path)


def with_undelivered_record(send_email: EmailSender, working_dir: Path) -> EmailSender:
    """Return an EmailSender that records undeliverable messages into working_dir instead of raising EmailSendError."""

    def send_email_or_record(subject: str, body: str) -> None:
        try:
            send_email(subject, body)
        except EmailSendError as error:
            _record_undelivered_email(working_dir, subject, body, str(error))

    return send_email_or_record
