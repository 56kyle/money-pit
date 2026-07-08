"""Module containing the category-to-tool routing table consumed by A2 templating and A3 fetch in the money_pit package."""

from money_pit.schemas.enums import DataSourceToken
from money_pit.schemas.enums import QuestionCategory


CATEGORY_TO_TOOLS: dict[QuestionCategory, list[DataSourceToken]] = {
    QuestionCategory.THESIS_VALIDATION: [
        DataSourceToken.EDGARTOOLS_MCP,
        DataSourceToken.YFINANCE_MCP,
        DataSourceToken.FRED_MCP,
        DataSourceToken.BRAVE_SEARCH_MCP,
    ],
    QuestionCategory.MACRO_REGIME: [
        DataSourceToken.FRED_MCP,
        DataSourceToken.BRAVE_SEARCH_MCP,
    ],
    QuestionCategory.CURRENT_EVENTS: [
        DataSourceToken.BRAVE_SEARCH_MCP,
        DataSourceToken.EDGARTOOLS_MCP,
        DataSourceToken.ALPACA_MCP,
    ],
    QuestionCategory.PORTFOLIO_GAP: [
        DataSourceToken.ALPACA_MCP,
        DataSourceToken.YFINANCE_MCP,
        DataSourceToken.EDGARTOOLS_MCP,
    ],
    QuestionCategory.INVALIDATION_CONDITIONS: [
        DataSourceToken.YFINANCE_MCP,
        DataSourceToken.EDGARTOOLS_MCP,
        DataSourceToken.FRED_MCP,
        DataSourceToken.BRAVE_SEARCH_MCP,
    ],
}
