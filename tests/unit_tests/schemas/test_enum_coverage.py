"""Drift guard: asserts every contract-specified string literal is present in the corresponding enum."""
import pytest

from money_pit.schemas.enums import (
    ActionType,
    ClaimCategory,
    ClaimRelationType,
    Confidence,
    ConvictionLevel,
    DataSourceToken,
    Determination,
    ExecutionOutcome,
    ExecutionPhase,
    FactorTag,
    QuestionCategory,
    RegimeTag,
    SignalTier,
    SourceType,
    TerminalState,
    ValidationStatus,
)


@pytest.mark.parametrize(
    "enum_cls, expected_values",
    [
        (SourceType, {"narrated_video", "newsletter", "rss", "research_pdf", "manual_note"}),
        (ClaimRelationType, {"agree", "disagree"}),
        (SignalTier, {"high", "medium", "low", "portfolio"}),
        (ClaimCategory, {"fundamental", "technical", "macro", "sentiment", "catalyst"}),
        (
            QuestionCategory,
            {
                "thesis_validation",
                "macro_regime",
                "current_events",
                "portfolio_gap",
                "invalidation_conditions",
            },
        ),
        (
            DataSourceToken,
            {
                "fred_mcp",
                "yfinance_mcp",
                "edgartools_mcp",
                "brave_search_mcp",
                "alpaca_mcp",
            },
        ),
        (Confidence, {"high", "medium", "low"}),
        (ActionType, {"BUY", "SELL", "TRIM", "ADD"}),
        (FactorTag, {"growth", "value", "momentum", "quality", "low_vol"}),
        (ValidationStatus, {"MATCHED", "UNMATCHED"}),
        (
            ExecutionPhase,
            {
                "PLANNED",
                "PREFLIGHT_OK",
                "PREFLIGHT_FAILED",
                "SUBMITTED",
                "FILLED",
                "PARTIALLY_FILLED",
                "REJECTED",
                "FAILED",
                "COMPENSATING",
                "COMPENSATED",
                "COMPENSATION_FAILED",
                "SKIPPED",
            },
        ),
        (
            ExecutionOutcome,
            {
                "EXECUTED_CLEAN",
                "PARTIAL_COMPENSATED",
                "COMPENSATION_FAILED",
                "EXECUTION_FAILED",
            },
        ),
        (
            RegimeTag,
            {
                "UNCERTAIN",
                "LATE_CYCLE_STRESS",
                "STAGFLATION",
                "GROWTH_ACCELERATING",
                "RECOVERY",
                "GROWTH_DECELERATING",
            },
        ),
        (ConvictionLevel, {"HIGH", "MEDIUM", "LOW"}),
        (
            TerminalState,
            {
                "NO_ACTION",
                "ANALYSIS_HALT",
                "VALIDATION_ERROR",
                "ORCHESTRATION_ERROR",
            },
        ),
        (Determination, {"PROCEED", "HALT"}),
    ],
)
def test_enum_has_all_contract_values(
    enum_cls: type, expected_values: set[str]
) -> None:
    member_values: set[str] = {m.value for m in enum_cls}
    assert expected_values <= member_values


def test_terminal_state_is_exactly_the_four_routing_members() -> None:
    member_values: set[str] = {m.value for m in TerminalState}
    assert member_values == {
        "NO_ACTION",
        "ANALYSIS_HALT",
        "VALIDATION_ERROR",
        "ORCHESTRATION_ERROR",
    }
