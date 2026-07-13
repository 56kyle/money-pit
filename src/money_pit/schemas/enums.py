"""Module containing every canonical enum used throughout the money_pit pipeline boundary contracts."""

from enum import Enum


class SourceType(str, Enum):
    NARRATED_VIDEO = "narrated_video"
    NEWSLETTER = "newsletter"
    RSS = "rss"
    RESEARCH_PDF = "research_pdf"
    MANUAL_NOTE = "manual_note"


class ClaimRelationType(str, Enum):
    AGREE = "agree"
    DISAGREE = "disagree"


class SignalTier(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    PORTFOLIO = "portfolio"


class ClaimCategory(str, Enum):
    FUNDAMENTAL = "fundamental"
    TECHNICAL = "technical"
    MACRO = "macro"
    SENTIMENT = "sentiment"
    CATALYST = "catalyst"


class QuestionCategory(str, Enum):
    THESIS_VALIDATION = "thesis_validation"
    MACRO_REGIME = "macro_regime"
    CURRENT_EVENTS = "current_events"
    PORTFOLIO_GAP = "portfolio_gap"
    INVALIDATION_CONDITIONS = "invalidation_conditions"


class DataSourceToken(str, Enum):
    FRED_MCP = "fred_mcp"
    YFINANCE_MCP = "yfinance_mcp"
    EDGARTOOLS_MCP = "edgartools_mcp"
    BRAVE_SEARCH_MCP = "brave_search_mcp"
    ALPACA_MCP = "alpaca_mcp"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ActionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    TRIM = "TRIM"
    ADD = "ADD"


class FactorTag(str, Enum):
    GROWTH = "growth"
    VALUE = "value"
    MOMENTUM = "momentum"
    QUALITY = "quality"
    LOW_VOL = "low_vol"


class ValidationStatus(str, Enum):
    MATCHED = "MATCHED"
    UNMATCHED = "UNMATCHED"


class OverallValidationStatus(str, Enum):
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"


class ExecutionPhase(str, Enum):
    PLANNED = "PLANNED"
    PREFLIGHT_OK = "PREFLIGHT_OK"
    PREFLIGHT_FAILED = "PREFLIGHT_FAILED"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    COMPENSATING = "COMPENSATING"
    COMPENSATED = "COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    SKIPPED = "SKIPPED"


class ExecutionOutcome(str, Enum):
    EXECUTED_CLEAN = "EXECUTED_CLEAN"
    EXECUTED_INCOMPLETE = "EXECUTED_INCOMPLETE"
    PARTIAL_COMPENSATED = "PARTIAL_COMPENSATED"
    COMPENSATION_FAILED = "COMPENSATION_FAILED"
    EXECUTION_FAILED = "EXECUTION_FAILED"


class RegimeTag(str, Enum):
    UNCERTAIN = "UNCERTAIN"
    LATE_CYCLE_STRESS = "LATE_CYCLE_STRESS"
    STAGFLATION = "STAGFLATION"
    GROWTH_ACCELERATING = "GROWTH_ACCELERATING"
    RECOVERY = "RECOVERY"
    GROWTH_DECELERATING = "GROWTH_DECELERATING"


class ConvictionLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Step1Disposition(str, Enum):
    SUPPORTED = "SUPPORTED"
    UNVERIFIED = "UNVERIFIED"
    CONTRADICTED = "CONTRADICTED"


class TerminalState(str, Enum):
    NO_ACTION = "NO_ACTION"
    ANALYSIS_HALT = "ANALYSIS_HALT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    ORCHESTRATION_ERROR = "ORCHESTRATION_ERROR"


class Determination(str, Enum):
    PROCEED = "PROCEED"
    HALT = "HALT"
