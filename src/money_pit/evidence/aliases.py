"""Bounded, provenance-preserving aliases for model-visible evidence."""

from __future__ import annotations

import json
import re
from math import ceil
from typing import TYPE_CHECKING
from typing import ClassVar

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import model_validator

from money_pit.evidence.errors import EvidenceProjectionError
from money_pit.evidence.errors import UnknownEvidenceAliasError
from money_pit.schemas.evidence import TimestampLocator


if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable

    from money_pit.schemas.evidence import EvidenceFragment


DEFAULT_ALIAS_TEXT_LIMIT = 800
MEDIA_SPAN_TEXT_LIMIT = 600
MEDIA_SPAN_SECONDS_LIMIT = 20.0
_EDGE_PUNCTUATION = re.compile(r"(^\W+|\W+$)")


class AliasedEvidence(BaseModel):
    """One model-visible span mapped to one or more durable fragments."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    alias: str = Field(pattern=r"^[EFT][0-9]{6}$")
    kind: str
    text: str
    fragment_ids: tuple[str, ...] = Field(min_length=1)
    start_seconds: float | None = Field(default=None, ge=0)
    end_seconds: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_fragment_identities(self) -> AliasedEvidence:
        """Require each alias to expose a unique complete provenance mapping."""
        if len(self.fragment_ids) != len(set(self.fragment_ids)):
            raise ValueError("An evidence alias cannot repeat a durable fragment identity")
        return self

    def prompt_record(self, *, core: bool | None = None) -> dict[str, object]:
        """Return model-visible fields without durable identifiers."""
        record: dict[str, object] = {"alias": self.alias, "kind": self.kind, "text": self.text}
        if self.start_seconds is not None:
            record["start_seconds"] = self.start_seconds
        if self.end_seconds is not None:
            record["end_seconds"] = self.end_seconds
        if core is not None:
            record["core"] = core
        return record


class EvidenceProjectionChunk(BaseModel):
    """One hard-bounded evidence window with explicit claim ownership."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    evidence: tuple[AliasedEvidence, ...]
    core_aliases: frozenset[str] = frozenset()

    def prompt_records(self) -> tuple[dict[str, object], ...]:
        """Return bounded records with explicit context-versus-core ownership."""
        return tuple(item.prompt_record(core=item.alias in self.core_aliases) for item in self.evidence)


class EvidenceAliasProjection(BaseModel):
    """A deterministic ephemeral alias view over durable fragments."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    evidence: tuple[AliasedEvidence, ...]

    def resolve(self, aliases: Iterable[str]) -> tuple[str, ...]:
        """Resolve aliases to unique durable fragment identifiers."""
        mapping = {item.alias: item.fragment_ids for item in self.evidence}
        resolved: list[str] = []
        for alias in aliases:
            try:
                identities = mapping[alias]
            except KeyError as error:
                raise UnknownEvidenceAliasError(f"Unknown evidence alias: {alias}") from error
            for fragment_id in identities:
                if fragment_id not in resolved:
                    resolved.append(fragment_id)
        return tuple(resolved)

    def chunks(self, character_budget: int) -> tuple[EvidenceProjectionChunk, ...]:
        """Partition at alias boundaries and add bounded neighboring context."""
        if character_budget < 2:
            raise EvidenceProjectionError("Evidence projection budget cannot contain a JSON array")
        cores = _partition_cores(self.evidence, character_budget)
        return _contextualize_cores(self.evidence, cores, character_budget)

    def chunks_fitting(
        self,
        accepts: Callable[[EvidenceProjectionChunk], bool],
    ) -> tuple[EvidenceProjectionChunk, ...]:
        """Partition using the exact complete inference-request acceptance boundary."""
        cores: list[list[AliasedEvidence]] = []
        current: list[AliasedEvidence] = []
        for item in self.evidence:
            proposed = [*current, item]
            chunk = _core_chunk(len(cores), proposed)
            if not accepts(chunk):
                if not current:
                    raise EvidenceProjectionError(f"Evidence alias {item.alias} exceeds the inference budget")
                cores.append(current)
                current = [item]
                if not accepts(_core_chunk(len(cores), current)):
                    raise EvidenceProjectionError(f"Evidence alias {item.alias} exceeds the inference budget")
            else:
                current.append(item)
        if current or not cores:
            cores.append(current)
        chunks: list[EvidenceProjectionChunk] = []
        for index, core in enumerate(cores):
            evidence = list(core)
            context = ([cores[index - 1][-1]] if index else []) + (
                [cores[index + 1][0]] if index + 1 < len(cores) else []
            )
            core_aliases = frozenset(item.alias for item in core)
            for item in context:
                proposed = sorted((*evidence, item), key=self.evidence.index)
                chunk = EvidenceProjectionChunk(index=index, evidence=tuple(proposed), core_aliases=core_aliases)
                if accepts(chunk):
                    evidence = proposed
            chunks.append(EvidenceProjectionChunk(index=index, evidence=tuple(evidence), core_aliases=core_aliases))
        return tuple(chunks)


def _core_chunk(index: int, evidence: list[AliasedEvidence]) -> EvidenceProjectionChunk:
    return EvidenceProjectionChunk(
        index=index,
        evidence=tuple(evidence),
        core_aliases=frozenset(item.alias for item in evidence),
    )


def _partition_cores(
    evidence: tuple[AliasedEvidence, ...],
    character_budget: int,
) -> list[list[AliasedEvidence]]:
    cores: list[list[AliasedEvidence]] = []
    current: list[AliasedEvidence] = []
    for item in evidence:
        proposed = (*current, item)
        if _serialized_size(proposed, frozenset(value.alias for value in proposed)) > character_budget:
            if not current:
                raise EvidenceProjectionError(f"Evidence alias {item.alias} exceeds the character budget")
            cores.append(current)
            current = [item]
            if _serialized_size(current, frozenset({item.alias})) > character_budget:
                raise EvidenceProjectionError(f"Evidence alias {item.alias} exceeds the character budget")
        else:
            current.append(item)
    if current or not cores:
        cores.append(current)
    return cores


def _contextualize_cores(
    all_evidence: tuple[AliasedEvidence, ...],
    cores: list[list[AliasedEvidence]],
    character_budget: int,
) -> tuple[EvidenceProjectionChunk, ...]:
    chunks: list[EvidenceProjectionChunk] = []
    for index, core in enumerate(cores):
        core_aliases = frozenset(item.alias for item in core)
        candidates = ([cores[index - 1][-1]] if index else []) + core
        if index + 1 < len(cores):
            candidates.append(cores[index + 1][0])
        evidence = list(core)
        for context in (candidates[:1] if index else []) + (candidates[-1:] if index + 1 < len(cores) else []):
            proposed = sorted((*evidence, context), key=all_evidence.index)
            if _serialized_size(proposed, core_aliases) <= character_budget:
                evidence = proposed
        chunks.append(EvidenceProjectionChunk(index=index, evidence=tuple(evidence), core_aliases=core_aliases))
    return tuple(chunks)


def project_evidence(
    fragments: Iterable[EvidenceFragment],
    *,
    alias_text_limit: int = DEFAULT_ALIAS_TEXT_LIMIT,
) -> EvidenceAliasProjection:
    """Project evidence, using media-aware compaction for timestamped cues."""
    if alias_text_limit <= 0:
        raise ValueError("alias_text_limit must be positive")
    ordered = list(fragments)
    transcripts = [item for item in ordered if item.kind == "transcript"]
    frames = [item for item in ordered if item.kind == "frame"]
    generic = [item for item in ordered if item.kind not in {"transcript", "frame"}]
    projected = [*_transcript_aliases(transcripts), *_frame_aliases(frames)]
    for fragment in generic:
        text = fragment.extracted_text or fragment.cited_source_text or ""
        for start in range(0, len(text), alias_text_limit):
            projected.append(
                AliasedEvidence(
                    alias=f"E{sum(item.alias.startswith('E') for item in projected) + 1:06d}",
                    kind=fragment.kind,
                    text=text[start : start + alias_text_limit],
                    fragment_ids=(fragment.fragment_id,),
                )
            )
    projected.sort(key=lambda item: (item.start_seconds is None, item.start_seconds or 0, item.alias))
    return EvidenceAliasProjection(evidence=tuple(projected))


def _transcript_aliases(fragments: list[EvidenceFragment]) -> list[AliasedEvidence]:
    aliases: list[AliasedEvidence] = []
    accumulated: list[str] = []
    words: list[str] = []
    identities: list[str] = []
    start = end = 0.0

    def flush() -> None:
        nonlocal words, identities, start, end
        if words:
            aliases.append(
                AliasedEvidence(
                    alias=f"T{len(aliases) + 1:06d}",
                    kind="transcript",
                    text=" ".join(words),
                    fragment_ids=tuple(identities),
                    start_seconds=start,
                    end_seconds=end,
                )
            )
        words, identities, start, end = [], [], 0.0, 0.0

    for fragment in sorted(fragments, key=lambda item: _timestamp(item)[0]):
        cue_start, cue_end = _timestamp(fragment)
        novel = _novel_tokens(accumulated, fragment.extracted_text or "")
        accumulated.extend(novel)
        for group, group_start, group_end in _timed_groups(novel, cue_start, cue_end, fragment.fragment_id):
            candidate = " ".join((*words, *group))
            duration = group_end - start if words else group_end - group_start
            if words and (len(candidate) > MEDIA_SPAN_TEXT_LIMIT or duration > MEDIA_SPAN_SECONDS_LIMIT):
                flush()
            if not words:
                start = group_start
            words.extend(group)
            identities.append(fragment.fragment_id)
            end = group_end
    flush()
    return aliases


def _frame_aliases(fragments: list[EvidenceFragment]) -> list[AliasedEvidence]:
    grouped: dict[tuple[str, float], list[EvidenceFragment]] = {}
    for fragment in fragments:
        grouped.setdefault((fragment.asset_id, _timestamp(fragment)[0]), []).append(fragment)
    aliases: list[AliasedEvidence] = []
    for (_, start), group in sorted(grouped.items(), key=lambda item: item[0][1]):
        text = " | ".join(
            value for fragment in group for value in (fragment.extracted_text, fragment.cited_source_text) if value
        )
        for words in _split_words(text.split()):
            aliases.append(
                AliasedEvidence(
                    alias=f"F{len(aliases) + 1:06d}",
                    kind="frame",
                    text=" ".join(words),
                    fragment_ids=tuple(fragment.fragment_id for fragment in group),
                    start_seconds=start,
                )
            )
    return aliases


def _token_key(token: str) -> str:
    return _EDGE_PUNCTUATION.sub("", token.casefold())


def _novel_tokens(accumulated: list[str], text: str) -> list[str]:
    incoming = text.split()
    prior_keys = [_token_key(token) for token in accumulated]
    incoming_keys = [_token_key(token) for token in incoming]
    for size in range(min(len(prior_keys), len(incoming_keys)), 0, -1):
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
        if len(word) > MEDIA_SPAN_TEXT_LIMIT:
            if current:
                groups.append(current)
                current = []
            groups.extend(
                [[word[index : index + MEDIA_SPAN_TEXT_LIMIT]] for index in range(0, len(word), MEDIA_SPAN_TEXT_LIMIT)]
            )
        elif current and len(" ".join((*current, word))) > MEDIA_SPAN_TEXT_LIMIT:
            groups.append(current)
            current = [word]
        else:
            current.append(word)
    if current:
        groups.append(current)
    return groups


def _timed_groups(words: list[str], start: float, end: float, fragment_id: str) -> list[tuple[list[str], float, float]]:
    if not words:
        return []
    duration = max(0.0, end - start)
    groups = _split_words(words)
    required = ceil(duration / MEDIA_SPAN_SECONDS_LIMIT)
    if required > len(words):
        raise EvidenceProjectionError(
            f"Evidence fragment {fragment_id!r} cannot fit a {MEDIA_SPAN_SECONDS_LIMIT}-second span"
        )
    while len(groups) < required:
        index = max(range(len(groups)), key=lambda value: len(groups[value]))
        selected = groups[index]
        midpoint = ceil(len(selected) / 2)
        groups[index : index + 1] = [selected[:midpoint], selected[midpoint:]]
    slice_duration = duration / len(groups)
    return [
        (group, start + index * slice_duration, start + (index + 1) * slice_duration)
        for index, group in enumerate(groups)
    ]


def _serialized_size(evidence: Iterable[AliasedEvidence], core_aliases: frozenset[str]) -> int:
    records = [item.prompt_record(core=item.alias in core_aliases) for item in evidence]
    return len(json.dumps(records, separators=(",", ":")))
