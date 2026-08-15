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
from typer.core import TyperGroup
from typing_extensions import override

from money_pit.config import ConfigurationError
from money_pit.sources.errors import ConnectorConfigurationError
from money_pit.sources.errors import SourceRegistryError
from money_pit.storage.errors import StorageError
from money_pit.storage.recovery_audit import RecoveryAuditBlockedError


_T = TypeVar("_T")
if TYPE_CHECKING:
    from collections.abc import Callable
_SENSITIVE_KEY = r"(api[_-]?key|authorization|credential|password|secret|token)"
_SENSITIVE_VALUE = r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^,;\r\n]*)"
_SENSITIVE_ASSIGNMENT = re.compile(rf"(?i)\b{_SENSITIVE_KEY}\s*[:=]\s*{_SENSITIVE_VALUE}")
_JSON_OUTPUT_META_KEY = "money_pit_json_output"
_DEBUG_META_KEY = "money_pit_debug"
_JSON_FLAG = "--json"
_DEBUG_FLAG = "--debug"


@dataclass(frozen=True)
class CliContext:
    """Global presentation and diagnostics settings for one invocation."""

    json_output: bool = False
    debug: bool = False


_CLI_SETTINGS: ContextVar[CliContext | None] = ContextVar("money_pit_cli_settings", default=None)


class OperatorFailureDiagnostic(BaseModel):
    """One sanitized durable-state detail associated with a command failure."""

    code: str
    detail: str
    durable_ids: tuple[str, ...] = ()


class OperatorFailure(BaseModel):
    """Sanitized machine-readable command failure."""

    category: str
    failure_kind: str
    error: str
    next_action: str
    diagnostics: tuple[OperatorFailureDiagnostic, ...] = ()


class RootCliGroup(TyperGroup):
    """Allow root presentation flags at any pre-separator CLI position."""

    @override
    def parse_args(self, ctx: object, args: list[str]) -> list[str]:
        """Move root flags ahead of nested commands before Click parses them."""
        typer_context = cast("typer.Context", ctx)
        command_args, separator, literal_args = _split_literal_args(args)
        root_flags = [argument for argument in command_args if argument in {_DEBUG_FLAG, _JSON_FLAG}]
        nested_args = [argument for argument in command_args if argument not in {_DEBUG_FLAG, _JSON_FLAG}]
        _publish_preparsed_context(root_flags)
        try:
            return super().parse_args(typer_context, [*root_flags, *nested_args, *separator, *literal_args])
        except Exception as error:
            if not _is_click_usage_error(error):
                raise
            raise typer.Exit(_emit_failure(error)) from error

    @override
    def invoke(self, ctx: object) -> object:
        """Render nested parsing failures through the shared error contract."""
        typer_context = cast("typer.Context", ctx)
        try:
            return cast("object", super().invoke(typer_context))
        except Exception as error:
            if not _is_click_usage_error(error):
                raise
            raise typer.Exit(_emit_failure(error)) from error


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
        failure_kind=type(error).__name__,
        error=_sanitized_error(error),
        next_action=_next_action(error, debug=context.debug),
        diagnostics=_failure_diagnostics(error),
    )
    if context.json_output:
        typer.echo(failure.model_dump_json(), err=True)
    else:
        summary = (
            f"[bold red]{failure.category} error:[/bold red] {failure.error}\nFailure kind: {failure.failure_kind}"
        )
        Console(stderr=True, highlight=False).print(summary)
        for diagnostic in failure.diagnostics:
            Console(stderr=True, highlight=False).print(
                f"[bold]{diagnostic.code}:[/bold] {diagnostic.detail}",
            )
            for durable_id in diagnostic.durable_ids:
                Console(stderr=True, highlight=False).print(f"  - {durable_id}")
        Console(stderr=True, highlight=False).print(f"Next action: {failure.next_action}")
    if failure.category == "usage":
        return 2
    return 78 if failure.category == "configuration" else 1


def _next_action(error: Exception, *, debug: bool) -> str:
    if _is_click_usage_error(error):
        return "Correct the command arguments. Run the command with --help to list valid options."
    if isinstance(error, RecoveryAuditBlockedError):
        source_option = "" if error.report.source_id is None else f" --source {error.report.source_id}"
        return f"Run money-pit intelligence audit{source_option} for full recovery details."
    if debug:
        return "Use the traceback and failure kind above to inspect the originating boundary."
    return "Add --debug anywhere before -- to include the originating traceback."


def _failure_diagnostics(error: Exception) -> tuple[OperatorFailureDiagnostic, ...]:
    if not isinstance(error, RecoveryAuditBlockedError):
        return ()
    return tuple(
        OperatorFailureDiagnostic(
            code=finding.code,
            detail=_sanitized_text(finding.detail),
            durable_ids=finding.durable_ids,
        )
        for finding in error.report.findings
    )


def _error_category(error: Exception) -> str:
    if _is_click_usage_error(error):
        return "usage"
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
    return _sanitized_text(message)


def _sanitized_text(message: str) -> str:
    redacted: str = _SENSITIVE_ASSIGNMENT.sub(lambda match: f"{match.group(1)}=<redacted>", message)
    return redacted[:500]


def _split_literal_args(args: list[str]) -> tuple[list[str], list[str], list[str]]:
    try:
        separator_index = args.index("--")
    except ValueError:
        return args, [], []
    return args[:separator_index], ["--"], args[separator_index + 1 :]


def _publish_preparsed_context(root_flags: list[str]) -> None:
    _ = _CLI_SETTINGS.set(
        CliContext(
            json_output=_JSON_FLAG in root_flags,
            debug=_DEBUG_FLAG in root_flags,
        )
    )


def _is_click_usage_error(error: Exception) -> bool:
    if isinstance(error, click.UsageError):
        return True
    return any(
        error_type.__name__ == "UsageError" and error_type.__module__ == "typer._click.exceptions"
        for error_type in type(error).__mro__
    )


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
