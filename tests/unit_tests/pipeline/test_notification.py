"""Tests for money_pit.pipeline.notification subject convention (wave S8, theme T7).

Pins the contract-§7 / architecture-§6.9 subject convention `money-pit: <reason> - {slug}`,
replacing the legacy `[money-pit] ...` form. The VALIDATION_ERROR reason is a pinned external
contract (`money-pit: MCP Validation Error - {slug}`), so its exact text is asserted; the
ANALYSIS_HALT reason wording is left free — only the convention (prefix + `- {slug}` suffix)
is pinned.
"""

from money_pit.pipeline.notification import _build_subject
from money_pit.schemas.enums import TerminalState


_SLUG = "test-run"


def test__build_subject_with_validation_error() -> None:
    subject = _build_subject(_SLUG, TerminalState.VALIDATION_ERROR, None)

    assert subject == f"money-pit: MCP Validation Error - {_SLUG}"


def test__build_subject_with_analysis_halt_follows_convention() -> None:
    subject = _build_subject(_SLUG, TerminalState.ANALYSIS_HALT, None)

    assert subject.startswith("money-pit: ")
    assert subject.endswith(f" - {_SLUG}")
