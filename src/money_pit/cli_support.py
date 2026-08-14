"""Module containing shared operator-facing CLI behavior."""

from __future__ import annotations

import json
import re
import traceback
from contextvars import ContextVar
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import cast

import click
import typer
from pydantic import BaseModel
from pydantic import JsonValue
from rich.console import Console

from money_pit.config import ConfigurationError
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import SourceRegistryError
from money_pit.storage.errors import StorageError


_T = TypeVar("_T")
if TYPE_CHECKING:
    from collections.abc import Callable
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)(api[_-]?key|authorization|credential|password|secret|token)\s*[:=]\s*([^\s,;]+)",
)
_JSON_OUTPUT_META_KEY = "money_pit_json_output"
_DEBUG_META_KEY = "money_pit_debug"


@dataclass(frozen=True)
class CliContext:
    """Global presentation and diagnostics settings for one invocation."""

    json_output: bool = False
    debug: bool = False


_CLI_SETTINGS: ContextVar[CliContext | None] = ContextVar("money_pit_cli_settings", default=None)


class OperatorFailure(BaseModel):
    """Sanitized machine-readable command failure."""

    category: str
    error: str
    next_action: str


def current_cli_context() -> CliContext:
    """Return root CLI settings, including safe defaults for sub-app tests."""
    return _CLI_SETTINGS.get() or CliContext()


def configure_cli_context(context: typer.Context, settings: CliContext) -> None:
    """Publish invocation settings through Click's shared context metadata."""
    context.obj = settings
    context.meta[_JSON_OUTPUT_META_KEY] = settings.json_output
    context.meta[_DEBUG_META_KEY] = settings.debug
    _ = _CLI_SETTINGS.set(settings)


def run_operator_command(
    action: Callable[[], _T],
    *,
    heading: str,
    human_value: Callable[[object], object] | None = None,
) -> _T:
    """Run one command through the shared sanitized error and output boundary."""
    try:
        result: _T = action()
    except (typer.Exit, click.ClickException):
        raise
    except Exception as error:
        exit_code = _emit_failure(error)
        raise typer.Exit(code=exit_code) from error
    emit_result(result, heading=heading, human_value=human_value)
    return result


def emit_result(
    value: object,
    *,
    heading: str,
    human_value: Callable[[object], object] | None = None,
) -> None:
    """Write one JSON value or one compact human-readable result."""
    context: CliContext = current_cli_context()
    serializable: JsonValue = _json_value(value)
    if context.json_output:
        typer.echo(json.dumps(serializable, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return
    displayed_value: object = human_value(value) if human_value is not None else _mask_human_accounts(serializable)
    displayed: JsonValue = _json_value(displayed_value)
    console = Console(highlight=False)
    console.print(f"[bold]{heading}[/bold]")
    _render_human(console, displayed)


def emit_progress(message: str) -> None:
    """Write progress to stderr so stdout remains one machine-readable value."""
    Console(stderr=True, highlight=False).print(message)


def _emit_failure(error: Exception) -> int:
    context: CliContext = current_cli_context()
    if context.debug:
        traceback.print_exc()
    failure = OperatorFailure(
        category=_error_category(error),
        error=_sanitized_error(error),
        next_action=(
            "Run the command again with --debug and inspect stderr."
            if not context.debug
            else "Use the traceback on stderr to correct the failing input or dependency."
        ),
    )
    if context.json_output:
        typer.echo(failure.model_dump_json(), err=True)
    else:
        Console(stderr=True, highlight=False).print(
            f"[bold red]{failure.category} error:[/bold red] {failure.error}\nNext action: {failure.next_action}",
        )
    return 78 if failure.category == "configuration" else 1


def _error_category(error: Exception) -> str:
    if isinstance(error, ConfigurationError | SourceRegistryError | ConnectorConfigurationError | ValueError):
        return "configuration"
    if isinstance(error, StorageError):
        return "storage"
    if isinstance(error, PermissionError):
        return "authorization"
    if isinstance(error, OSError):
        return "filesystem"
    name: str = type(error).__name__.casefold()
    if "source" in name or "provider" in name:
        return "provider"
    if "credential" in name or "secret" in name:
        return "authorization"
    return "operation"


def _sanitized_error(error: Exception) -> str:
    message: str = " ".join(str(error).split()) or type(error).__name__
    redacted: str = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    return redacted[:500]


def _json_value(value: object) -> JsonValue:
    if isinstance(value, BaseModel):
        return cast("JsonValue", value.model_dump(mode="json"))
    if isinstance(value, Enum):
        enum_value = cast("object", value.value)
        return _json_value(enum_value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        mapping = cast("dict[object, object]", value)
        return {str(key): _json_value(item) for key, item in mapping.items()}
    if isinstance(value, tuple | list):
        sequence = cast("tuple[object, ...] | list[object]", value)
        return [_json_value(item) for item in sequence]
    return str(value)


def _mask_human_accounts(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return {
            key: _mask_account(str(item)) if key == "account_id" and item is not None else _mask_human_accounts(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_mask_human_accounts(item) for item in value]
    return value


def _mask_account(account_id: str) -> str:
    visible: str = account_id[-4:]
    return f"****{visible}"


def _render_human(console: Console, value: JsonValue, *, prefix: str = "") -> None:
    if isinstance(value, dict):
        mapping = cast("dict[str, JsonValue]", value)
        for key, item in mapping.items():
            label: str = key.replace("_", " ")
            if isinstance(item, dict | list):
                console.print(f"{prefix}[bold]{label}[/bold]")
                _render_human(console, item, prefix=f"{prefix}  ")
            else:
                console.print(f"{prefix}{label}: {_human_scalar(item)}")
        return
    if isinstance(value, list):
        sequence = cast("list[JsonValue]", value)
        if not sequence:
            console.print(f"{prefix}none")
        for item in sequence:
            if isinstance(item, dict | list):
                console.print(f"{prefix}-")
                _render_human(console, item, prefix=f"{prefix}  ")
            else:
                console.print(f"{prefix}- {_human_scalar(item)}")
        return
    console.print(f"{prefix}{_human_scalar(value)}")


def _human_scalar(value: object) -> str:
    if value is None:
        return "none"
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)
