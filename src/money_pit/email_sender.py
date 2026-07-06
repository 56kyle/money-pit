"""Module exposing a Gmail-backed EmailSender for the money_pit package."""

import smtplib
from email.message import EmailMessage

from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import resolve_gmail_app_password
from money_pit.contracts import EmailSender


class EmailSendError(Exception):
    """Raised when an outgoing email cannot be delivered — SMTP failure, connection failure, or auth rejection."""


def _build_message(sender_address: str, recipient: str, subject: str, body: str) -> EmailMessage:
    """Return an EmailMessage addressed from the Gmail account to the owner recipient."""
    message: EmailMessage = EmailMessage()
    message["From"] = sender_address
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(body)
    return message


def make_gmail_email_sender(config: Config) -> EmailSender:
    """Return an EmailSender that delivers to the owner recipient via Gmail SMTP with STARTTLS."""
    if config.gmail_address is None:
        raise CredentialResolutionError("No gmail_address configured; cannot build a Gmail email sender.")
    sender_address: str = config.gmail_address
    app_password: str = resolve_gmail_app_password(config)

    def send_email(subject: str, body: str) -> None:
        message: EmailMessage = _build_message(sender_address, config.owner_recipient, subject, body)
        try:
            with smtplib.SMTP(config.smtp_host, config.smtp_port) as server:
                _ = server.starttls()
                _ = server.login(sender_address, app_password)
                _ = server.send_message(message)
        except (smtplib.SMTPException, OSError) as error:
            raise EmailSendError(f"Failed to send email to {config.owner_recipient!r}: {error}.") from error

    return send_email
