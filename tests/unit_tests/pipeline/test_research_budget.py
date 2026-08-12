import pytest

from money_pit.contracts import ResearchTaskDraft
from money_pit.pipeline.research import _select_research_tasks  # pyright: ignore[reportPrivateUsage]


def _task(index: int, *, maximum_results: int) -> ResearchTaskDraft:
    return ResearchTaskDraft(
        candidate_thesis_id="candidate-1",
        provider="brave",
        query=f"query-{index}",
        purpose=f"purpose-{index}",
        maximum_results=maximum_results,
    )


def test__select_research_tasks_preserves_original_task_identity() -> None:
    tasks = tuple(_task(index, maximum_results=maximum) for index, maximum in enumerate((5, 5, 5, 5, 20)))

    selected = _select_research_tasks(tasks, query_budget=10, fetch_budget=19)

    assert selected == tasks


def test__select_research_tasks_limits_task_count_by_query_budget() -> None:
    tasks = tuple(_task(index, maximum_results=5) for index in range(5))

    selected = _select_research_tasks(tasks, query_budget=3, fetch_budget=19)

    assert tuple(task.query for task in selected) == ("query-0", "query-1", "query-2")


def test__select_research_tasks_limits_task_count_by_minimum_fetch_coverage() -> None:
    tasks = tuple(_task(index, maximum_results=20) for index in range(4))

    selected = _select_research_tasks(tasks, query_budget=4, fetch_budget=2)

    assert tuple(task.query for task in selected) == ("query-0", "query-1")
    assert tuple(task.maximum_results for task in selected) == (20, 20)


@pytest.mark.parametrize(("query_budget", "fetch_budget"), [(0, 1), (1, 0)])
def test__select_research_tasks_returns_empty_with_exhausted_budget(
    query_budget: int,
    fetch_budget: int,
) -> None:
    tasks = (_task(0, maximum_results=5),)

    assert _select_research_tasks(tasks, query_budget=query_budget, fetch_budget=fetch_budget) == ()
