"""A3 node: deterministic known-param fetch, budget control, file writes."""
from pathlib import Path
from typing import Callable

from money_pit.graph.state import PipelineState
from money_pit.schemas.answers import Answer, InitialAnswers
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.questions import Question, InitialQuestions
from money_pit.schemas.signals import AggregatedSignals


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


def make_retrieval_node(
    answer_synthesis_agent: Callable[[list[Question], list[SourceRef]], list[Answer]],
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

        answers: list[Answer] = answer_synthesis_agent(
            initial_questions.questions,
            aggregated_signals.sources,
        )
        initial_answers = InitialAnswers(
            slug=slug,
            sources=aggregated_signals.sources,
            answers=answers,
        )

        _ = (working_dir / "initial_answers.json").write_text(
            initial_answers.model_dump_json(indent=2),
            encoding="utf-8",
        )
        _ = (working_dir / "initial_answers.md").write_text(
            _render_markdown(slug, answers),
            encoding="utf-8",
        )

        result: dict[str, object] = {
            "completed_steps": list(state.get("completed_steps") or []) + ["retrieval"]
        }
        return result

    return retrieval_node
