"""PipelineState TypedDict: incremental LangGraph state accumulated across pipeline nodes."""
from typing import Literal
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
