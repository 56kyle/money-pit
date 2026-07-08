"""Module containing the ExecutionJournalEntry and ExecutionJournal models of the execution_journal.json contract for the money_pit package."""

from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict

from money_pit.schemas.enums import ExecutionOutcome
from money_pit.schemas.enums import ExecutionPhase


class ExecutionJournalEntry(BaseModel):
    """One append-only journal record written by the execution sub-agent per phase transition."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    group_id: str | None
    client_order_id: str
    phase: ExecutionPhase
    intended: dict[str, object]
    broker_order_id: str | None
    status: str | None
    filled_qty: float | None
    filled_avg_price: float | None
    realized_notional: float | None
    compensation_of: str | None
    error: str | None
    timestamp: str


class ExecutionJournal(BaseModel):
    """Append-only execution record for a run; written incrementally to execution_journal.json."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    outcome: ExecutionOutcome | None
    entries: list[ExecutionJournalEntry]
