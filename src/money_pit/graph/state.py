"""Module containing the PipelineState TypedDict of incremental LangGraph state accumulated across pipeline nodes in the money_pit package."""

from pathlib import Path
from typing import Literal
from typing import Protocol
from typing import TypedDict

from money_pit.schemas.enums import Determination
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.validation_results import ValidationStep


class PipelineState(TypedDict, total=False):
    """Incremental LangGraph state accumulated across pipeline nodes."""

    slug: str
    working_dir: str
    completed_steps: list[str]
    terminal_state: TerminalState | None
    run_has_actionable_content: bool
    validation_steps: list[ValidationStep]
    determination: Determination | None
    failed_steps: list[str]
    sub_agent_spawned: Literal["execution", "notification"] | None
    determination_reason: str


def require_working_dir(state: PipelineState) -> Path:
    """Return the working directory as a Path, raising if it is unset."""
    working_dir: str | None = state.get("working_dir")
    if working_dir is None:
        raise ValueError("PipelineState missing required key 'working_dir'")
    return Path(working_dir)


def require_slug(state: PipelineState) -> str:
    slug: str | None = state.get("slug")
    if slug is None:
        raise ValueError("PipelineState missing required key 'slug'")
    return slug


def with_completed_step(state: PipelineState, step: str) -> list[str]:
    return [*list(state.get("completed_steps") or []), step]


class PipelineNode(Protocol):
    """A LangGraph node: maps the accumulated state to a partial state update.

    A partial-update dict is itself a valid total=False PipelineState, so the return
    annotation type-checks every emitted key and value against the schema. Expressed as
    a Protocol rather than Callable so the `state` parameter stays keyword-capable, which
    LangGraph's add_node signature requires.
    """

    def __call__(self, state: PipelineState) -> PipelineState: ...
