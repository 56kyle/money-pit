"""A1 LLM core: multimodal/text → SignalSetDraft. Encapsulated inside the video adapter."""

from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic_ai import Agent

from money_pit.config import Config
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.signal_draft import SignalSetDraft


class TranscriptSource(str, Enum):
    """Origin of the transcript text delivered with a VideoPayload."""

    UPLOADER_CAPTIONS = "uploader_captions"
    AUTO_CAPTIONS = "auto_captions"
    WHISPER = "whisper"


class VideoPayload(BaseModel):
    """Typed boundary between deterministic video ingestion and the A1 LLM classification step."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    slug: str
    source_ref: SourceRef
    transcript: str
    transcript_source: TranscriptSource
    has_word_timestamps: bool  # wired in Phase 7 when WhisperX forced alignment runs
    on_screen_text: list[str]  # wired in Phase 7 when OCR/VLM keyframe extraction runs


_PROMPT_PATH: Path = Path(__file__).parent.parent.parent.parent / "data" / "agents" / "agent_1.md"
_SYSTEM_PROMPT: str = _PROMPT_PATH.read_text(encoding="utf-8")


def _build_user_message(payload: VideoPayload) -> str:
    return (
        "## Source Metadata\n"
        + payload.source_ref.model_dump_json(indent=2)
        + "\n\n## Transcript\n"
        + payload.transcript
        + ("\n\n## On-Screen Text\n" + "\n".join(payload.on_screen_text) if payload.on_screen_text else "")
        + "\n\n## Transcript Provenance\n"
        + f"Source: {payload.transcript_source.value}, word-level timestamps: {payload.has_word_timestamps}"
    )


def make_video_llm_agent(
    config: Config,
    *,
    model: str | None = None,
) -> Callable[[VideoPayload], SignalSetDraft]:
    """Creates a closure that runs a VideoPayload through the A1 LLM and returns SignalSetDraft."""
    agent: Agent[None, SignalSetDraft] = Agent(
        f"anthropic:{model or config.llm_model}",
        output_type=SignalSetDraft,
        system_prompt=_SYSTEM_PROMPT,
    )

    def run(payload: VideoPayload) -> SignalSetDraft:
        result = agent.run_sync(_build_user_message(payload))
        return result.output

    return run
