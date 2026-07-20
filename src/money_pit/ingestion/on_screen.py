"""Module containing the multimodal VLM on-screen extractor for the money_pit package."""

from collections.abc import Callable
from typing import ClassVar
from typing import TypeAlias

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic_ai import Agent
from pydantic_ai import BinaryContent

from money_pit.config import Config
from money_pit.constants import OPENAI_MODEL_PREFIX
from money_pit.ingestion.artifacts import Keyframe
from money_pit.ingestion.artifacts import OnScreenExtraction
from money_pit.prompt_loader import system_prompt


class OnScreenDraft(BaseModel):
    """The VLM's per-keyframe output, before the deterministic locator is stamped on."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    on_screen_text: list[str]
    cited_sources: list[str]


OnScreenExtractor: TypeAlias = Callable[[list[Keyframe]], list[OnScreenExtraction]]


_PROMPT_NAME: str = "agent_video_onscreen"
_IMAGE_MEDIA_TYPE: str = "image/png"
_INSTRUCTION: str = "Extract the on-screen text and any source attribution from this keyframe."


def _to_extraction(draft: OnScreenDraft, keyframe: Keyframe) -> OnScreenExtraction:
    return OnScreenExtraction(
        locator=keyframe.locator,
        on_screen_text=draft.on_screen_text,
        cited_sources=draft.cited_sources,
    )


def make_on_screen_extractor(config: Config, *, model: str | None = None) -> OnScreenExtractor:
    """Creates a closure that reads on-screen text and cited sources from each keyframe via the VLM."""
    agent: Agent[None, OnScreenDraft] = Agent(
        f"{OPENAI_MODEL_PREFIX}{model or config.llm_model}",
        output_type=OnScreenDraft,
        system_prompt=system_prompt(_PROMPT_NAME),
    )

    def extract(keyframes: list[Keyframe]) -> list[OnScreenExtraction]:
        extractions: list[OnScreenExtraction] = []
        for keyframe in keyframes:
            image_bytes = keyframe.image_path.read_bytes()
            result = agent.run_sync([_INSTRUCTION, BinaryContent(data=image_bytes, media_type=_IMAGE_MEDIA_TYPE)])
            extractions.append(_to_extraction(result.output, keyframe))
        return extractions

    return extract
