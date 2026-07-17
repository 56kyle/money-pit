"""Module containing the A1 LLM core (plain-text thesis into SignalSetDraft) encapsulated inside the text adapter of the money_pit package."""

from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic_ai import Agent

from money_pit.config import Config
from money_pit.constants import ANTHROPIC_MODEL_PREFIX
from money_pit.prompt_loader import system_prompt
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import SignalSetDraft


class TextPayload(BaseModel):
    """Typed boundary between deterministic thesis ingestion and the A1 LLM classification step."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    source_ref: SourceRef
    body: str


_PROMPT_NAME: str = "agent_1_text"


def _build_user_message(payload: TextPayload) -> str:
    return "## Source Metadata\n" + payload.source_ref.model_dump_json(indent=2) + "\n\n## Thesis\n" + payload.body


def make_text_llm_agent(
    config: Config,
    *,
    model: str | None = None,
) -> Callable[[TextPayload], SignalSetDraft]:
    """Creates a closure that runs a TextPayload through the A1 LLM and returns SignalSetDraft."""
    agent: Agent[None, SignalSetDraft] = Agent(
        f"{ANTHROPIC_MODEL_PREFIX}{model or config.llm_model}",
        output_type=SignalSetDraft,
        system_prompt=system_prompt(_PROMPT_NAME),
    )

    def run(payload: TextPayload) -> SignalSetDraft:
        result = agent.run_sync(_build_user_message(payload))
        return result.output

    return run
