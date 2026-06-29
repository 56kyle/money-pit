"""A3 node: deterministic known-param fetch, budget control, file writes."""
from pathlib import Path
from typing import Callable

from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.graph.state import PipelineState
from money_pit.schemas.answers import Answer, InitialAnswers
from money_pit.schemas.enums import Confidence, QuestionCategory
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import InitialQuestions, Question
from money_pit.schemas.signals import AggregatedSignals


_FRED_SERIES: dict[str, str] = {
    "yield_curve": "T10Y2Y",
    "credit_spreads": "BAMLH0A0HYM2",
    "pmi": "NAPM",
    "earnings_revisions": "SP500",
    "inflation": "CPILFESL",
}

_DETERMINISTIC_CATEGORIES: frozenset[QuestionCategory] = frozenset({
    QuestionCategory.MACRO_REGIME,
    QuestionCategory.PORTFOLIO_GAP,
})

_INDICATOR_PREFIX: str = "indicator:"
_FRED_SOURCE: str = "fred_mcp"
_ALPACA_SOURCE: str = "alpaca_mcp"


def _render_markdown(slug: str, answers: list[Answer]) -> str:
    """Return the initial_answers.md content for a completed retrieval run."""
    lines: list[str] = [
        f"# Initial Answers — {slug}",
        "",
        "## Answers",
        "",
    ]
    for answer in answers:
        lines.extend([
            f"### {answer.question_id} — {answer.category.value}",
            f"**Question:** {answer.question}",
            f"**Answer:** {answer.answer}",
            f"Confidence: {answer.confidence.value} | Sources: {', '.join(answer.sources_used)}",
            f"Limitations: {answer.limitations or 'none'}",
            "",
        ])
    return "\n".join(lines)


def _fetch_deterministic(
    question: Question,
    tools: DeterministicResearchTools,
) -> dict[str, object] | None:
    """Return fetched data for a deterministic question, or None if unavailable."""
    if question.category == QuestionCategory.MACRO_REGIME:
        if not question.signal_source.startswith(_INDICATOR_PREFIX):
            return None
        indicator_name: str = question.signal_source[len(_INDICATOR_PREFIX):]
        series_id: str | None = _FRED_SERIES.get(indicator_name)
        if series_id is None:
            return None
        fred_value: float | None = tools.fetch_fred_series(series_id)
        if fred_value is None:
            return None
        fred_result: dict[str, object] = {"value": fred_value}
        return fred_result

    if question.category == QuestionCategory.PORTFOLIO_GAP:
        ticker: str = question.signal_source
        price: float | None = tools.fetch_ticker_price(ticker) if ticker != "none" else None
        if price is None:
            return None
        price_result: dict[str, object] = {"value": price}
        return price_result

    return None


def make_retrieval_node(
    answer_synthesis_agent: Callable[[list[Question], list[SourceRef]], list[Answer]],
    deterministic_tools: DeterministicResearchTools,
) -> Callable[[PipelineState], dict[str, object]]:
    """Return a LangGraph node that answers research questions via agent retrieval."""

    def retrieval_node(state: PipelineState) -> dict[str, object]:
        working_dir_str = state.get("working_dir")
        slug = state.get("slug")
        if working_dir_str is None or slug is None:
            raise ValueError("retrieval_node requires 'working_dir' and 'slug' in state")
        working_dir = Path(working_dir_str)

        initial_questions = InitialQuestions.model_validate_json(
            (working_dir / "initial_questions.json").read_text(encoding="utf-8")
        )
        aggregated_signals = AggregatedSignals.model_validate_json(
            (working_dir / "aggregated_signals.json").read_text(encoding="utf-8")
        )

        questions: list[Question] = initial_questions.questions
        sources: list[SourceRef] = aggregated_signals.sources

        deterministic_questions: list[Question] = [
            q for q in questions if q.category in _DETERMINISTIC_CATEGORIES
        ]
        open_ended_questions: list[Question] = [
            q for q in questions if q.category not in _DETERMINISTIC_CATEGORIES
        ]

        deterministic_pairs: list[tuple[Question, dict[str, object] | None]] = [
            (q, _fetch_deterministic(q, deterministic_tools))
            for q in deterministic_questions
        ]

        deterministic_answers: list[Answer] = [
            Answer(
                question_id=q.id,
                question=q.question,
                category=q.category,
                signal_source=q.signal_source,
                signal_tier=q.signal_tier,
                answer=f"Fetched: {data_retrieved}" if data_retrieved else "Data unavailable.",
                confidence=Confidence.HIGH if data_retrieved else Confidence.LOW,
                sources_used=[
                    _FRED_SOURCE if q.category == QuestionCategory.MACRO_REGIME else _ALPACA_SOURCE
                ],
                data_retrieved=data_retrieved,
                limitations=(
                    "Direct deterministic fetch; no LLM synthesis."
                    if data_retrieved
                    else "Fetch returned no data."
                ),
            )
            for q, data_retrieved in deterministic_pairs
        ]

        try:
            llm_answers: list[Answer] = answer_synthesis_agent(open_ended_questions, sources)
        except Exception:
            llm_answers = []

        all_answers: list[Answer] = deterministic_answers + llm_answers
        initial_answers = InitialAnswers(slug=slug, sources=sources, answers=all_answers)

        _ = (working_dir / "initial_answers.json").write_text(
            initial_answers.model_dump_json(indent=2),
            encoding="utf-8",
        )
        _ = (working_dir / "initial_answers.md").write_text(
            _render_markdown(slug, all_answers),
            encoding="utf-8",
        )

        result: dict[str, object] = {
            "completed_steps": list(state.get("completed_steps") or []) + ["retrieval"]
        }
        return result

    return retrieval_node
