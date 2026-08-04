"""Module containing bounded A1 video classification."""

import json
from collections.abc import Callable
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai import ModelHTTPError

from money_pit.adapters.video_payload import TranscriptSource as TranscriptSource
from money_pit.adapters.video_payload import VideoPayload
from money_pit.adapters.video_prompt import VideoChunkDraft
from money_pit.adapters.video_prompt import VideoPromptChunk
from money_pit.adapters.video_prompt import VideoPromptProjection
from money_pit.adapters.video_prompt import VideoSummaryDraft
from money_pit.adapters.video_prompt import chunk_projection
from money_pit.adapters.video_prompt import compose_video_drafts
from money_pit.adapters.video_prompt import project_video_evidence
from money_pit.config import Config
from money_pit.constants import OPENAI_MODEL_PREFIX
from money_pit.prompt_loader import system_prompt
from money_pit.schemas.signal_draft import SignalSetDraft


class VideoPromptBudgetExceededError(ValueError):
    """Raised before model I/O when one bounded video request cannot fit."""

    def __init__(
        self,
        source_id: str,
        limit: int,
        observed: int,
        *,
        chunk_index: int | None = None,
        offending_span: str | None = None,
    ) -> None:
        """Record bounded diagnostics without retaining source content."""
        self.source_id: str = source_id
        self.limit: int = limit
        self.observed: int = observed
        self.chunk_index: int | None = chunk_index
        self.offending_span: str | None = offending_span
        location = f", chunk {chunk_index}" if chunk_index is not None else ""
        span = f", span {offending_span!r}" if offending_span is not None else ""
        super().__init__(
            f"Video prompt for {source_id!r} exceeds the {limit}-character budget "
            f"({observed} characters{location}{span})."
        )


class VideoModelContextExceededError(RuntimeError):
    """Raised when the provider rejects a preflighted video request as too large."""

    def __init__(
        self,
        source_id: str,
        model: str,
        limit: int,
        chunk_index: int,
        *,
        operation: str = "classification",
    ) -> None:
        """Record the rejected model call without retaining source content."""
        self.source_id: str = source_id
        self.model: str = model
        self.limit: int = limit
        self.chunk_index: int = chunk_index
        self.operation: str = operation
        super().__init__(
            f"Model {model!r} rejected video {operation} step {chunk_index} for {source_id!r} "
            f"after the {limit}-character preflight."
        )


class _ProviderErrorBody(BaseModel):
    """The provider error fields used to classify context failures."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="allow")

    code: str | None = None


class _SummaryCapacityError(ValueError):
    """Raised when one generated summary exceeds reduction capacity."""

    def __init__(self, observed: int) -> None:
        """Record the generated summary size without its content."""
        self.observed: int = observed
        super().__init__(observed)


_PROMPT_NAME = "agent_1_video_chunk"
_SUMMARY_PROMPT = (
    "Combine the ordered chunk summaries into two or three neutral sentences. "
    "Do not add facts. Return only the structured summary field."
)


def _chunk_user_message(
    payload: VideoPayload,
    chunk: VideoPromptChunk,
    *,
    include_legacy_screen_text: bool,
) -> str:
    evidence = [span.prompt_record(core=span.alias in chunk.core_aliases) for span in chunk.spans]
    legacy = (
        "\n\n## Legacy On-Screen Text\n" + "\n".join(payload.on_screen_text)
        if include_legacy_screen_text and payload.on_screen_text
        else ""
    )
    return (
        "## Source Metadata\n"
        + payload.source_ref.model_dump_json(indent=2)
        + f"\n\n## Chunk\n{chunk.index}\n\n## Evidence\n"
        + json.dumps(evidence, separators=(",", ":"))
        + legacy
        + "\n\n## Transcript Provenance\n"
        + f"Source: {payload.transcript_source.value}, word-level timestamps: {payload.has_word_timestamps}"
    )


def _build_user_message(payload: VideoPayload) -> str:
    """Build one projected message for compatibility with focused tests."""
    projection = project_video_evidence(payload)
    chunk = VideoPromptChunk(
        index=0,
        spans=projection.spans,
        core_aliases=frozenset(span.alias for span in projection.spans),
    )
    has_frames = any(span.kind == "frame" for span in projection.spans)
    return _chunk_user_message(payload, chunk, include_legacy_screen_text=not has_frames)


def _is_context_error(error: ModelHTTPError) -> bool:
    try:
        return _ProviderErrorBody.model_validate(error.body).code == "context_length_exceeded"
    except ValidationError:
        return False


def _summary_batches(summaries: list[str], capacity: int) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    size = 0
    for summary in summaries:
        if len(summary) > capacity:
            raise _SummaryCapacityError(len(summary))
        if current and size + len(summary) + 1 > capacity:
            batches.append(current)
            current, size = [], 0
        current.append(summary)
        size += len(summary) + 1
    if current:
        batches.append(current)
    return batches


class _VideoClassifier:
    """Own bounded classification calls and deterministic composition."""

    def __init__(self, config: Config, model_name: str) -> None:
        """Build the classifier and summary-only agents."""
        self._config: Config = config
        self._model_name: str = model_name
        self._classifier_prompt: str = system_prompt(_PROMPT_NAME)
        self._classifier: Agent[None, VideoChunkDraft] = Agent(
            f"{OPENAI_MODEL_PREFIX}{model_name}",
            output_type=VideoChunkDraft,
            system_prompt=self._classifier_prompt,
        )
        self._summarizer: Agent[None, VideoSummaryDraft] = Agent(
            f"{OPENAI_MODEL_PREFIX}{model_name}",
            output_type=VideoSummaryDraft,
            system_prompt=_SUMMARY_PROMPT,
        )
        self._classifier_schema_size: int = len(json.dumps(VideoChunkDraft.model_json_schema()))
        self._summary_schema_size: int = len(json.dumps(VideoSummaryDraft.model_json_schema()))

    def _chunks(
        self, payload: VideoPayload, projection: VideoPromptProjection, has_frames: bool
    ) -> tuple[VideoPromptChunk, ...]:
        empty_chunk = VideoPromptChunk(index=0, spans=(), core_aliases=frozenset())
        empty_chunk_message = _chunk_user_message(payload, empty_chunk, include_legacy_screen_text=not has_frames)
        fixed_size = (
            len(self._classifier_prompt)
            + self._classifier_schema_size
            + len(empty_chunk_message)
            + self._config.video_llm_response_character_reserve
        )
        span_sizes = [
            len(json.dumps(span.prompt_record(core=True), separators=(",", ":"))) + 1 for span in projection.spans
        ]
        available = self._config.video_llm_context_character_budget - fixed_size
        total_size = sum(span_sizes)
        maximum_span_size = max(span_sizes, default=0)
        evidence_capacity = available if total_size <= available else available - 2 * maximum_span_size
        if evidence_capacity <= 0:
            raise VideoPromptBudgetExceededError(
                payload.source_ref.source_id,
                self._config.video_llm_context_character_budget,
                fixed_size + maximum_span_size,
            )
        try:
            chunks = chunk_projection(projection, evidence_capacity)
        except ValueError as error:
            alias = str(error)
            raise VideoPromptBudgetExceededError(
                payload.source_ref.source_id,
                self._config.video_llm_context_character_budget,
                fixed_size + maximum_span_size,
                offending_span=alias,
            ) from error
        return chunks

    def _classify(
        self, payload: VideoPayload, chunks: tuple[VideoPromptChunk, ...], has_frames: bool
    ) -> tuple[VideoChunkDraft, ...]:
        drafts: list[VideoChunkDraft] = []
        for chunk in chunks:
            message = _chunk_user_message(payload, chunk, include_legacy_screen_text=not has_frames)
            observed = (
                len(self._classifier_prompt)
                + self._classifier_schema_size
                + len(message)
                + self._config.video_llm_response_character_reserve
            )
            if observed > self._config.video_llm_context_character_budget:
                raise VideoPromptBudgetExceededError(
                    payload.source_ref.source_id,
                    self._config.video_llm_context_character_budget,
                    observed,
                    chunk_index=chunk.index,
                )
            try:
                drafts.append(self._classifier.run_sync(message).output)
            except ModelHTTPError as error:
                if not _is_context_error(error):
                    raise
                raise VideoModelContextExceededError(
                    payload.source_ref.source_id,
                    self._model_name,
                    self._config.video_llm_context_character_budget,
                    chunk.index,
                ) from error
        return tuple(drafts)

    def _summary(self, payload: VideoPayload, drafts: tuple[VideoChunkDraft, ...]) -> str:
        if len(drafts) == 1:
            return drafts[0].summary
        summaries = [draft.summary for draft in drafts]
        capacity = (
            self._config.video_llm_context_character_budget
            - len(_SUMMARY_PROMPT)
            - self._summary_schema_size
            - self._config.video_llm_response_character_reserve
        )
        try:
            while len(summaries) > 1:
                batches = _summary_batches(summaries, capacity)
                if len(batches) == len(summaries):
                    raise _SummaryCapacityError(max(map(len, summaries), default=0))
                reduced: list[str] = []
                for batch_index, batch in enumerate(batches):
                    try:
                        reduced.append(self._summarizer.run_sync("\n".join(batch)).output.summary)
                    except ModelHTTPError as error:
                        if not _is_context_error(error):
                            raise
                        raise VideoModelContextExceededError(
                            payload.source_ref.source_id,
                            self._model_name,
                            self._config.video_llm_context_character_budget,
                            batch_index,
                            operation="summary",
                        ) from error
                summaries = reduced
            return summaries[0]
        except _SummaryCapacityError as error:
            raise VideoPromptBudgetExceededError(
                payload.source_ref.source_id,
                self._config.video_llm_context_character_budget,
                error.observed,
            ) from error

    def run(self, payload: VideoPayload) -> SignalSetDraft:
        """Classify all bounded chunks and compose one source draft."""
        projection = project_video_evidence(payload)
        has_frames = any(span.kind == "frame" for span in projection.spans)
        chunks = self._chunks(payload, projection, has_frames)
        drafts = self._classify(payload, chunks, has_frames)
        return compose_video_drafts(payload, projection, chunks, drafts, self._summary(payload, drafts))


def make_video_llm_agent(config: Config, *, model: str | None = None) -> Callable[[VideoPayload], SignalSetDraft]:
    """Create a bounded classifier that resolves prompt aliases to evidence IDs."""
    return _VideoClassifier(config, model or config.llm_model).run
