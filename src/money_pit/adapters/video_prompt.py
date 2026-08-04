"""Module containing bounded video evidence projection and draft composition."""

from __future__ import annotations

import json
import re
from math import ceil
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Literal

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator
from money_pit.schemas.signal_draft import ClaimDraft
from money_pit.schemas.signal_draft import SignalSetDraft


if TYPE_CHECKING:
    from collections.abc import Iterable

    from money_pit.adapters.video_payload import VideoPayload


SPAN_MAX_CHARACTERS = 600
SPAN_MAX_SECONDS = 20.0
_EDGE_PUNCTUATION: re.Pattern[str] = re.compile(r"(^\W+|\W+$)")


class ProjectedEvidenceSpan(BaseModel):
    """A compact model-visible span mapped to durable evidence fragments."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    alias: str
    kind: Literal["transcript", "frame"]
    start_seconds: float
    end_seconds: float | None = None
    text: str
    fragment_ids: tuple[str, ...]

    def prompt_record(self, *, core: bool) -> dict[str, object]:
        """Return the compact model-visible record without durable ID mappings."""
        return {
            "alias": self.alias,
            "kind": self.kind,
            "start_seconds": self.start_seconds,
            "end_seconds": self.end_seconds,
            "text": self.text,
            "core": core,
        }


class VideoPromptProjection(BaseModel):
    """The complete ephemeral evidence view for one video."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    spans: tuple[ProjectedEvidenceSpan, ...]


class VideoPromptChunk(BaseModel):
    """A bounded evidence window with explicit core ownership."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    index: int
    spans: tuple[ProjectedEvidenceSpan, ...]
    core_aliases: frozenset[str]


class VideoChunkClaimDraft(BaseModel):
    """A claim emitted for one bounded video chunk."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    claim: str
    tier: Literal["high", "medium", "low"]
    category: Literal["fundamental", "technical", "macro", "sentiment", "catalyst"]
    tickers_affected: tuple[str, ...]
    cited_sources: tuple[str, ...]
    evidence_aliases: tuple[str, ...] = Field(min_length=1)


class VideoChunkDraft(BaseModel):
    """Chunk-local classification output without source-global identifiers."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    summary: str
    claims: tuple[VideoChunkClaimDraft, ...]
    tickers_mentioned: tuple[str, ...]
    sectors_mentioned: tuple[str, ...]
    macro_themes: tuple[str, ...]


class VideoSummaryDraft(BaseModel):
    """A bounded reduction of ordered chunk summaries."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    summary: str


class UnknownEvidenceAliasError(ValueError):
    """Raised when a chunk claim cites an alias absent from its prompt."""


class EvidenceProjectionError(ValueError):
    """Raised when an atomic evidence cue cannot satisfy projection limits."""

    def __init__(self, fragment_id: str, duration: float) -> None:
        """Record the irreducible cue locator without retaining its content."""
        self.fragment_id: str = fragment_id
        self.duration: float = duration
        super().__init__(f"Evidence fragment {fragment_id!r} cannot fit a {SPAN_MAX_SECONDS}-second span.")


def _token_key(token: str) -> str:
    return _EDGE_PUNCTUATION.sub("", token.casefold())


def _novel_tokens(accumulated: list[str], text: str) -> list[str]:
    incoming = text.split()
    prior_keys = [_token_key(token) for token in accumulated]
    incoming_keys = [_token_key(token) for token in incoming]
    maximum = min(len(prior_keys), len(incoming_keys))
    for size in range(maximum, 0, -1):
        if prior_keys[-size:] == incoming_keys[:size] and (size >= 2 or len(incoming_keys) == 1):
            return incoming[size:]
    return incoming


def _timestamp(fragment: EvidenceFragment) -> tuple[float, float]:
    if isinstance(fragment.locator, TimestampLocator):
        return fragment.locator.start_seconds, fragment.locator.end_seconds or fragment.locator.start_seconds
    return 0.0, 0.0


def _split_words(words: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if len(word) > SPAN_MAX_CHARACTERS:
            if current:
                groups.append(current)
                current = []
            groups.extend(
                [[word[index : index + SPAN_MAX_CHARACTERS]] for index in range(0, len(word), SPAN_MAX_CHARACTERS)]
            )
            continue
        if current and len(" ".join((*current, word))) > SPAN_MAX_CHARACTERS:
            groups.append(current)
            current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _timed_groups(words: list[str], start: float, end: float, fragment_id: str) -> list[tuple[list[str], float, float]]:
    duration = max(0.0, end - start)
    character_groups = _split_words(words)
    duration_group_count = ceil(duration / SPAN_MAX_SECONDS)
    if duration_group_count > len(words) and duration > SPAN_MAX_SECONDS:
        raise EvidenceProjectionError(fragment_id, duration)
    groups = character_groups
    while len(groups) < duration_group_count:
        split_index = max(range(len(groups)), key=lambda index: len(groups[index]))
        selected = groups[split_index]
        if len(selected) < 2:
            raise EvidenceProjectionError(fragment_id, duration)
        midpoint = ceil(len(selected) / 2)
        groups[split_index : split_index + 1] = [selected[:midpoint], selected[midpoint:]]
    slice_duration = duration / len(groups) if groups else 0.0
    return [
        (group, start + index * slice_duration, start + (index + 1) * slice_duration)
        for index, group in enumerate(groups)
    ]


def _transcript_spans(fragments: list[EvidenceFragment], fallback_text: str) -> list[ProjectedEvidenceSpan]:
    if not fragments:
        return [
            ProjectedEvidenceSpan(
                alias=f"T{index:06d}",
                kind="transcript",
                start_seconds=0.0,
                end_seconds=0.0,
                text=" ".join(words),
                fragment_ids=(),
            )
            for index, words in enumerate(_split_words(fallback_text.split()), start=1)
        ]

    spans: list[ProjectedEvidenceSpan] = []
    accumulated: list[str] = []
    words: list[str] = []
    fragment_ids: list[str] = []
    start = 0.0
    end = 0.0

    def flush() -> None:
        nonlocal words, fragment_ids, start, end
        if words:
            spans.append(
                ProjectedEvidenceSpan(
                    alias=f"T{len(spans) + 1:06d}",
                    kind="transcript",
                    start_seconds=start,
                    end_seconds=end,
                    text=" ".join(words),
                    fragment_ids=tuple(dict.fromkeys(fragment_ids)),
                )
            )
        words, fragment_ids, start, end = [], [], 0.0, 0.0

    for fragment in fragments:
        if not fragment.extracted_text:
            continue
        cue_start, cue_end = _timestamp(fragment)
        novel = _novel_tokens(accumulated, fragment.extracted_text)
        accumulated.extend(novel)
        if not novel:
            continue
        for group, group_start, group_end in _timed_groups(novel, cue_start, cue_end, fragment.fragment_id):
            candidate = " ".join((*words, *group))
            duration = group_end - start if words else group_end - group_start
            if words and (len(candidate) > SPAN_MAX_CHARACTERS or duration > SPAN_MAX_SECONDS):
                flush()
            if not words:
                start = group_start
            words.extend(group)
            fragment_ids.append(fragment.fragment_id)
            end = group_end
    flush()
    return spans


def _frame_spans(fragments: list[EvidenceFragment]) -> list[ProjectedEvidenceSpan]:
    grouped: dict[tuple[str, float], list[EvidenceFragment]] = {}
    for fragment in fragments:
        start, _ = _timestamp(fragment)
        grouped.setdefault((fragment.asset_id, start), []).append(fragment)
    spans: list[ProjectedEvidenceSpan] = []
    for (_, start), group in grouped.items():
        text = " | ".join(
            value for fragment in group for value in (fragment.extracted_text, fragment.cited_source_text) if value
        )
        for words in _split_words(text.split()):
            spans.append(
                ProjectedEvidenceSpan(
                    alias=f"F{len(spans) + 1:06d}",
                    kind="frame",
                    start_seconds=start,
                    text=" ".join(words),
                    fragment_ids=tuple(fragment.fragment_id for fragment in group),
                )
            )
    return spans


def project_video_evidence(payload: VideoPayload) -> VideoPromptProjection:
    """Build a compact evidence view without changing the durable payload."""
    ordered = sorted(enumerate(payload.evidence_fragments), key=lambda item: (_timestamp(item[1])[0], item[0]))
    transcript = [fragment for _, fragment in ordered if fragment.kind == "transcript"]
    frames = [fragment for _, fragment in ordered if fragment.kind == "frame"]
    spans = (*_transcript_spans(transcript, payload.transcript), *_frame_spans(frames))
    return VideoPromptProjection(spans=tuple(sorted(spans, key=lambda span: (span.start_seconds, span.alias))))


def chunk_projection(projection: VideoPromptProjection, evidence_character_budget: int) -> tuple[VideoPromptChunk, ...]:
    """Partition projected evidence at span boundaries with one-span context."""
    cores: list[list[ProjectedEvidenceSpan]] = []
    current: list[ProjectedEvidenceSpan] = []
    size = 0
    for span in projection.spans:
        span_size = len(json.dumps(span.prompt_record(core=True), separators=(",", ":"))) + 1
        if span_size > evidence_character_budget:
            raise ValueError(span.alias)
        if current and size + span_size > evidence_character_budget:
            cores.append(current)
            current, size = [], 0
        current.append(span)
        size += span_size
    if current or not cores:
        cores.append(current)
    chunks: list[VideoPromptChunk] = []
    for index, core in enumerate(cores):
        before = [cores[index - 1][-1]] if index else []
        after = [cores[index + 1][0]] if index + 1 < len(cores) else []
        chunks.append(
            VideoPromptChunk(
                index=index,
                spans=(*before, *core, *after),
                core_aliases=frozenset(span.alias for span in core),
            )
        )
    return tuple(chunks)


def _first_seen(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def compose_video_drafts(
    payload: VideoPayload,
    projection: VideoPromptProjection,
    chunks: tuple[VideoPromptChunk, ...],
    drafts: tuple[VideoChunkDraft, ...],
    summary: str,
) -> SignalSetDraft:
    """Resolve aliases and create one deterministic source-level draft."""
    alias_order = {span.alias: index for index, span in enumerate(projection.spans)}
    candidates: list[tuple[int, VideoChunkClaimDraft, tuple[str, ...]]] = []
    for chunk, draft in zip(chunks, drafts, strict=True):
        visible = {span.alias: span for span in chunk.spans}
        for claim in draft.claims:
            unknown = [alias for alias in claim.evidence_aliases if alias not in visible]
            if unknown:
                raise UnknownEvidenceAliasError(f"Unknown evidence aliases: {', '.join(unknown)}")
            earliest = min(claim.evidence_aliases, key=alias_order.__getitem__)
            if earliest not in chunk.core_aliases:
                continue
            evidence_ids = tuple(
                dict.fromkeys(
                    fragment_id for alias in claim.evidence_aliases for fragment_id in visible[alias].fragment_ids
                )
            )
            candidates.append((alias_order[earliest], claim, evidence_ids))

    unique: dict[tuple[object, ...], tuple[int, VideoChunkClaimDraft, tuple[str, ...]]] = {}
    for candidate in candidates:
        _, claim, evidence_ids = candidate
        key = (
            claim.claim,
            claim.tier,
            claim.category,
            tuple(claim.tickers_affected),
            tuple(claim.cited_sources),
            evidence_ids,
        )
        _ = unique.setdefault(key, candidate)
    tier_order = {"high": 0, "medium": 1, "low": 2}
    ordered = sorted(unique.values(), key=lambda item: (tier_order.get(item[1].tier, 3), item[0]))
    claims = [
        ClaimDraft(
            claim_id=f"{payload.source_ref.source_id}:S{index:03d}",
            claim=claim.claim,
            tier=claim.tier,
            category=claim.category,
            tickers_affected=list(claim.tickers_affected),
            cited_sources=list(claim.cited_sources),
            evidence_fragment_ids=list(evidence_ids),
        )
        for index, (_, claim, evidence_ids) in enumerate(ordered, start=1)
    ]
    source = payload.source_ref
    return SignalSetDraft(
        source_id=source.source_id,
        source_type=source.source_type.value,
        title=source.title,
        url=str(source.url) if source.url else None,
        published_at=source.published_at,
        retrieved_at=source.retrieved_at,
        summary=summary,
        claims=claims,
        tickers_mentioned=_first_seen(value for draft in drafts for value in draft.tickers_mentioned),
        sectors_mentioned=_first_seen(value for draft in drafts for value in draft.sectors_mentioned),
        macro_themes=_first_seen(value for draft in drafts for value in draft.macro_themes),
    )
