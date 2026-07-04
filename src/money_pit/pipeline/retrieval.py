"""A3 node: deterministic known-param fetch, budget control, file writes."""

from pathlib import Path

from loguru import logger

from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.compute.confidence import derive_confidence
from money_pit.constants import AGGREGATED_SIGNALS_JSON_FILENAME
from money_pit.constants import INITIAL_ANSWERS_JSON_FILENAME
from money_pit.constants import INITIAL_ANSWERS_MD_FILENAME
from money_pit.constants import INITIAL_QUESTIONS_JSON_FILENAME
from money_pit.contracts import AnswerSynthesisAgent
from money_pit.graph.state import PipelineNode
from money_pit.graph.state import PipelineState
from money_pit.graph.state import require_slug
from money_pit.graph.state import require_working_dir
from money_pit.graph.state import with_completed_step
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.answers import Answer
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import DataSourceToken
from money_pit.schemas.enums import QuestionCategory
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import INDICATOR_PREFIX
from money_pit.schemas.questions import InitialQuestions
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import AggregatedSignals


_FRED_SERIES: dict[str, str] = {
    "yield_curve": "T10Y2Y",
    "credit_spreads": "BAMLH0A0HYM2",
    "pmi": "NAPM",
    "earnings_revisions": "SP500",
    "inflation": "CPILFESL",
}

_DETERMINISTIC_CATEGORIES: frozenset[QuestionCategory] = frozenset(
    {
        QuestionCategory.MACRO_REGIME,
        QuestionCategory.PORTFOLIO_GAP,
    }
)

_MACRO_REGIME_SOURCE: DataSourceToken = DataSourceToken.FRED_MCP
_PORTFOLIO_GAP_SOURCE: DataSourceToken = DataSourceToken.YFINANCE_MCP


def _draft_to_answer(draft: AnswerDraft, question: Question) -> Answer:
    """Join an A3 draft to its originating question, deriving code-owned fields."""
    return Answer(
        question_id=draft.question_id,
        question=question.question,
        category=question.category,
        signal_source=question.signal_source,
        signal_tier=question.signal_tier,
        answer=draft.answer,
        confidence=derive_confidence(draft.sources_used),
        sources_used=draft.sources_used,
        data_retrieved=draft.data_retrieved,
        limitations=draft.limitations,
    )


def _render_markdown(slug: str, answers: list[Answer]) -> str:
    """Return the initial_answers.md content for a completed retrieval run."""
    lines: list[str] = [
        f"# Initial Answers — {slug}",
        "",
        "## Answers",
        "",
    ]
    for answer in answers:
        lines.extend(
            [
                f"### {answer.question_id} — {answer.category.value}",
                f"**Question:** {answer.question}",
                f"**Answer:** {answer.answer}",
                f"Confidence: {answer.confidence.value} | Sources: {', '.join(answer.sources_used)}",
                f"Limitations: {answer.limitations or 'none'}",
                "",
            ]
        )
    return "\n".join(lines)


def _fetch_deterministic(
    question: Question,
    tools: DeterministicResearchTools,
) -> dict[str, object] | None:
    """Return fetched data for a deterministic question, or None if unavailable."""
    if question.category == QuestionCategory.MACRO_REGIME:
        if not question.signal_source.startswith(INDICATOR_PREFIX):
            return None
        indicator_name: str = question.signal_source[len(INDICATOR_PREFIX) :]
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


def _deterministic_answer(question: Question, data_retrieved: dict[str, object] | None) -> Answer:
    """Build an Answer for a deterministic question, deriving confidence from provenance."""
    source_token: DataSourceToken = (
        _MACRO_REGIME_SOURCE if question.category == QuestionCategory.MACRO_REGIME else _PORTFOLIO_GAP_SOURCE
    )
    sources_used: list[DataSourceToken] = [source_token] if data_retrieved else []
    return Answer(
        question_id=question.id,
        question=question.question,
        category=question.category,
        signal_source=question.signal_source,
        signal_tier=question.signal_tier,
        answer=f"Fetched: {data_retrieved}" if data_retrieved else "Data unavailable.",
        confidence=derive_confidence(sources_used),
        sources_used=sources_used,
        data_retrieved=data_retrieved,
        limitations=("Direct deterministic fetch; no LLM synthesis." if data_retrieved else "Fetch returned no data."),
    )


def _load_retrieval_inputs(working_dir: Path) -> tuple[InitialQuestions, AggregatedSignals]:
    """Load the A2 questions and aggregated signals for a retrieval run."""
    initial_questions = InitialQuestions.model_validate_json(
        (working_dir / INITIAL_QUESTIONS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    aggregated_signals = AggregatedSignals.model_validate_json(
        (working_dir / AGGREGATED_SIGNALS_JSON_FILENAME).read_text(encoding="utf-8")
    )
    return initial_questions, aggregated_signals


def _answer_deterministic(
    deterministic_questions: list[Question],
    deterministic_tools: DeterministicResearchTools,
) -> list[Answer]:
    """Fetch and answer the deterministic questions via code-owned research tools."""
    deterministic_pairs: list[tuple[Question, dict[str, object] | None]] = [
        (q, _fetch_deterministic(q, deterministic_tools)) for q in deterministic_questions
    ]
    return [_deterministic_answer(q, data_retrieved) for q, data_retrieved in deterministic_pairs]


def _answer_open_ended(
    answer_synthesis_agent: AnswerSynthesisAgent,
    open_ended_questions: list[Question],
    sources: list[SourceRef],
) -> list[Answer]:
    """Synthesize open-ended answers via the A3 agent, soft-failing to an empty list."""
    question_by_id: dict[str, Question] = {q.id: q for q in open_ended_questions}
    try:
        drafts: list[AnswerDraft] = answer_synthesis_agent(open_ended_questions, sources)
    except Exception:
        logger.exception("A3 answer synthesis agent failed; proceeding with no open-ended answers")
        drafts = []

    llm_answers: list[Answer] = []
    for draft in drafts:
        question = question_by_id.get(draft.question_id)
        if question is None:
            logger.warning(
                "Dropping A3 draft with unmatched question_id {question_id}",
                question_id=draft.question_id,
            )
            continue
        llm_answers.append(_draft_to_answer(draft, question))
    return llm_answers


def _write_retrieval_outputs(
    working_dir: Path,
    slug: str,
    initial_answers: InitialAnswers,
    all_answers: list[Answer],
) -> None:
    """Write the initial_answers.json and initial_answers.md retrieval artifacts."""
    _ = (working_dir / INITIAL_ANSWERS_JSON_FILENAME).write_text(
        initial_answers.model_dump_json(indent=2),
        encoding="utf-8",
    )
    _ = (working_dir / INITIAL_ANSWERS_MD_FILENAME).write_text(
        _render_markdown(slug, all_answers),
        encoding="utf-8",
    )


def make_retrieval_node(
    answer_synthesis_agent: AnswerSynthesisAgent,
    deterministic_tools: DeterministicResearchTools,
) -> PipelineNode:
    """Return a LangGraph node that answers research questions via agent retrieval."""

    def retrieval_node(state: PipelineState) -> PipelineState:
        working_dir = require_working_dir(state)
        slug = require_slug(state)

        initial_questions, aggregated_signals = _load_retrieval_inputs(working_dir)
        questions: list[Question] = initial_questions.questions
        sources: list[SourceRef] = aggregated_signals.sources

        deterministic_questions: list[Question] = [q for q in questions if q.category in _DETERMINISTIC_CATEGORIES]
        open_ended_questions: list[Question] = [q for q in questions if q.category not in _DETERMINISTIC_CATEGORIES]

        deterministic_answers: list[Answer] = _answer_deterministic(deterministic_questions, deterministic_tools)
        llm_answers: list[Answer] = _answer_open_ended(answer_synthesis_agent, open_ended_questions, sources)

        all_answers: list[Answer] = deterministic_answers + llm_answers
        initial_answers = InitialAnswers(slug=slug, sources=sources, answers=all_answers)

        _write_retrieval_outputs(working_dir, slug, initial_answers, all_answers)

        result: PipelineState = {"completed_steps": with_completed_step(state, "retrieval")}
        return result

    return retrieval_node
