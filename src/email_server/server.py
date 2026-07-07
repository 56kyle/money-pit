"""Module containing the FastMCP email server exposing send_email(to, subject, body), folded into the money_pit package.

Configuration follows the app's MONEY_PIT__ double-underscore convention, single-sourcing the SMTP
defaults and the EmailSendError type from money_pit:
  MONEY_PIT__SMTP_HOST            SMTP host (default: money_pit.constants.DEFAULT_SMTP_HOST)
  MONEY_PIT__SMTP_PORT            SMTP port (default: money_pit.constants.DEFAULT_SMTP_PORT)
  MONEY_PIT__GMAIL_ADDRESS        authenticating Gmail address, used as the From header
  MONEY_PIT__GMAIL_APP_PASSWORD   Gmail app password for that address
"""

import os
import smtplib
from email.message import EmailMessage

from fastmcp import FastMCP

from money_pit.constants import DEFAULT_SMTP_HOST
from money_pit.constants import DEFAULT_SMTP_PORT
from money_pit.email_sender import EmailSendError


class EmailServerConfigError(Exception):
    """Raised when a required SMTP environment variable is unset."""


mcp: FastMCP = FastMCP("money-pit email")


def _require_env(name: str) -> str:
    """Return an environment variable's value, failing closed if it is unset."""
    value: str | None = os.environ.get(name)
    if value is None:
        raise EmailServerConfigError(f"Required environment variable {name!r} is unset.")
    return value


def _env_port(name: str, default: int) -> int:
    """Return an environment variable parsed as a port, failing closed with EmailServerConfigError if non-numeric."""
    raw: str | None = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as error:
        raise EmailServerConfigError(f"Environment variable {name!r} must be an integer port, got {raw!r}.") from error


def _send(to: str, subject: str, body: str) -> None:
    """Deliver one email via Gmail SMTP with STARTTLS, raising EmailServerConfigError on bad config and EmailSendError on transport failure."""
    sender_address: str = _require_env("MONEY_PIT__GMAIL_ADDRESS")
    app_password: str = _require_env("MONEY_PIT__GMAIL_APP_PASSWORD")
    host: str = os.environ.get("MONEY_PIT__SMTP_HOST", DEFAULT_SMTP_HOST)
    port: int = _env_port("MONEY_PIT__SMTP_PORT", DEFAULT_SMTP_PORT)

    message: EmailMessage = EmailMessage()
    message["From"] = sender_address
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        with smtplib.SMTP(host, port) as server:
            _ = server.starttls()
            _ = server.login(sender_address, app_password)
            _ = server.send_message(message)
    except (smtplib.SMTPException, OSError) as error:
        raise EmailSendError(f"Failed to send email to {to!r}: {error}.") from error


@mcp.tool
def send_email(to: str, subject: str, body: str) -> str:
    """Send an email and return a short confirmation string, raising EmailServerConfigError on bad config and EmailSendError on transport failure."""
    _send(to, subject, body)
    return f"Sent email to {to}."


if __name__ == "__main__":
    mcp.run()
