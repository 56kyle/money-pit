"""FastMCP email server exposing send_email(to, subject, body); a standalone deployable, independent of money_pit.

Configuration is read from the environment, following the app's MONEY_PIT__ double-underscore convention:
  MONEY_PIT__SMTP_HOST        SMTP host (default: smtp.gmail.com)
  MONEY_PIT__SMTP_PORT        SMTP port (default: 587)
  MONEY_PIT__GMAIL_ADDRESS    authenticating Gmail address, used as the From header
  MONEY_PIT__GMAIL_APP_PASSWORD   Gmail app password for that address
"""

import os
import smtplib
from email.message import EmailMessage

from fastmcp import FastMCP


_DEFAULT_SMTP_HOST: str = "smtp.gmail.com"
_DEFAULT_SMTP_PORT: int = 587


class EmailServerConfigError(Exception):
    """Raised when a required SMTP environment variable is unset."""


class EmailSendError(Exception):
    """Raised when an outgoing email cannot be delivered."""


mcp: FastMCP = FastMCP("money-pit email")


def _require_env(name: str) -> str:
    """Return an environment variable's value, failing closed if it is unset."""
    value: str | None = os.environ.get(name)
    if value is None:
        raise EmailServerConfigError(f"Required environment variable {name!r} is unset.")
    return value


def _send(to: str, subject: str, body: str) -> None:
    """Deliver one email via Gmail SMTP with STARTTLS, wrapping transport failures in EmailSendError."""
    sender_address: str = _require_env("MONEY_PIT__GMAIL_ADDRESS")
    app_password: str = _require_env("MONEY_PIT__GMAIL_APP_PASSWORD")
    host: str = os.environ.get("MONEY_PIT__SMTP_HOST", _DEFAULT_SMTP_HOST)
    port: int = int(os.environ.get("MONEY_PIT__SMTP_PORT", _DEFAULT_SMTP_PORT))

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
    """Send an email and return a short confirmation string."""
    _send(to, subject, body)
    return f"Sent email to {to}."


if __name__ == "__main__":
    mcp.run()
