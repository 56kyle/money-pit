"""Unit tests for bounded video evidence projection and draft composition."""

from collections.abc import Iterable
from typing import Literal

import pytest
from pydantic import ValidationError

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.adapters.video_llm import _build_user_message
from money_pit.adapters.video_prompt import SPAN_MAX_CHARACTERS
from money_pit.adapters.video_prompt import SPAN_MAX_SECONDS
from money_pit.adapters.video_prompt import EvidenceProjectionError
from money_pit.adapters.video_prompt import ProjectedEvidenceSpan
from money_pit.adapters.video_prompt import UnknownEvidenceAliasError
from money_pit.adapters.video_prompt import VideoChunkClaimDraft
from money_pit.adapters.video_prompt import VideoChunkDraft
from money_pit.adapters.video_prompt import VideoPromptChunk
from money_pit.adapters.video_prompt import VideoPromptProjection
from money_pit.adapters.video_prompt import _novel_tokens
from money_pit.adapters.video_prompt import chunk_projection
from money_pit.adapters.video_prompt import compose_video_drafts
from money_pit.adapters.video_prompt import project_video_evidence
from money_pit.config import Config
from money_pit.schemas.enums import SourceType
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator
from money_pit.schemas.provenance import SourceRef


def test_config_uses_bounded_video_llm_defaults() -> None:
    config = Config(alpaca_service="alpaca", alpaca_username="key", alpaca_paper=True)

    assert (config.video_llm_context_character_budget, config.video_llm_response_character_reserve) == (
        160_000,
        32_000,
    )


def test_config_with_video_response_reserve_at_context_limit_raises() -> None:
    with pytest.raises(ValidationError):
        Config(
            alpaca_service="alpaca",
            alpaca_username="key",
            alpaca_paper=True,
            video_llm_context_character_budget=64_000,
            video_llm_response_character_reserve=64_000,
        )


def _source_ref() -> SourceRef:
    return SourceRef(
        source_id="yt_test_001",
        source_type=SourceType.NARRATED_VIDEO,
        title="Bounded video",
        url="https://www.youtube.com/watch?v=test",
        published_at="2026-01-15T12:00:00Z",
        retrieved_at="2026-06-18T14:30:00Z",
        locator=None,
    )


def _fragment(
    index: int,
    text: str | None,
    *,
    kind: Literal["transcript", "frame"] = "transcript",
    start: float | None = None,
    end: float | None = None,
    asset_id: str = "asset-1",
    cited_source_text: str | None = None,
) -> EvidenceFragment:
    return EvidenceFragment(
        fragment_id=f"fragment-{index}",
        asset_id=asset_id,
        kind=kind,
        locator=TimestampLocator(start_seconds=float(index) if start is None else start, end_seconds=end),
        extracted_text=text,
        cited_source_text=cited_source_text,
        extraction_method="test",
    )


def _payload(
    fragments: Iterable[EvidenceFragment] = (),
    *,
    transcript: str = "",
    on_screen_text: list[str] | None = None,
) -> VideoPayload:
    return VideoPayload(
        slug="test-run",
        source_ref=_source_ref(),
        transcript=transcript,
        transcript_source=TranscriptSource.UPLOADER_CAPTIONS,
        has_word_timestamps=False,
        on_screen_text=on_screen_text or [],
        evidence_fragments=tuple(fragments),
    )


@pytest.mark.parametrize(
    ("accumulated", "incoming", "expected"),
    [
        (["have", "we", "seen", "that?"], "seen that? Big announcements", ["Big", "announcements"]),
        (["have", "we", "seen", "that?"], "we seen that?", []),
        (["Have", "WE", "seen,"], "we seen. THAT", ["THAT"]),
        (["alpha", "beta"], "gamma delta", ["gamma", "delta"]),
        (["alpha"], "alpha beta", ["alpha", "beta"]),
        (["alpha"], "alpha", []),
    ],
)
def test__novel_tokens_with_rolling_caption_variants(
    accumulated: list[str], incoming: str, expected: list[str]
) -> None:
    assert _novel_tokens(accumulated, incoming) == expected


def test_project_video_evidence_with_span_limits() -> None:
    fragments = (
        _fragment(1, "a" * 350, start=0, end=2),
        _fragment(2, "b" * 350, start=3, end=5),
        _fragment(3, "third cue", start=25, end=27),
    )

    projection = project_video_evidence(_payload(fragments))

    assert all(len(span.text) <= SPAN_MAX_CHARACTERS for span in projection.spans)
    assert all(
        span.end_seconds is None or span.end_seconds - span.start_seconds <= SPAN_MAX_SECONDS
        for span in projection.spans
    )


def test_project_video_evidence_with_irreducible_long_atomic_cue() -> None:
    payload = _payload((_fragment(1, "word", start=0, end=40),))

    with pytest.raises(EvidenceProjectionError) as raised:
        project_video_evidence(payload)

    assert (raised.value.fragment_id, raised.value.duration) == ("fragment-1", 40)


def test_project_video_evidence_enforces_character_and_duration_limits_together() -> None:
    text = " ".join(("x" * 595, "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"))
    payload = _payload((_fragment(1, text, start=0, end=120),))

    projection = project_video_evidence(payload)

    assert len(projection.spans) >= 6
    assert all(len(span.text) <= SPAN_MAX_CHARACTERS for span in projection.spans)
    assert all(
        span.end_seconds is not None and span.end_seconds - span.start_seconds <= SPAN_MAX_SECONDS
        for span in projection.spans
    )


def test_project_video_evidence_with_late_identical_cue_does_not_extend_span() -> None:
    fragments = (
        _fragment(1, "same words", start=0, end=1),
        _fragment(2, "same words", start=30, end=31),
    )

    projection = project_video_evidence(_payload(fragments))

    assert [(span.start_seconds, span.end_seconds) for span in projection.spans] == [(0, 1)]


def test_project_video_evidence_orders_out_of_order_fragments_by_timestamp() -> None:
    fragments = (
        _fragment(2, "later cue", start=10, end=11),
        _fragment(1, "earlier cue", start=1, end=2),
    )

    projection = project_video_evidence(_payload(fragments))

    assert projection.spans[0].text == "earlier cue later cue"


def test_project_video_evidence_with_legacy_payload() -> None:
    projection = project_video_evidence(_payload(transcript="Legacy transcript text."))

    assert projection.spans == (
        ProjectedEvidenceSpan(
            alias="T000001",
            kind="transcript",
            start_seconds=0,
            end_seconds=0,
            text="Legacy transcript text.",
            fragment_ids=(),
        ),
    )


def test_project_video_evidence_groups_frames_by_asset_and_timestamp() -> None:
    fragments = (
        _fragment(1, "Revenue chart", kind="frame", start=5, asset_id="frame-asset", cited_source_text="SEC"),
        _fragment(2, "Revenue +20%", kind="frame", start=5, asset_id="frame-asset"),
        _fragment(3, "Other image", kind="frame", start=5, asset_id="other-asset"),
    )

    frames = [span for span in project_video_evidence(_payload(fragments)).spans if span.kind == "frame"]

    assert [(span.text, span.fragment_ids) for span in frames] == [
        ("Revenue chart | SEC | Revenue +20%", ("fragment-1", "fragment-2")),
        ("Other image", ("fragment-3",)),
    ]


def test__build_user_message_with_frame_evidence_omits_legacy_screen_text() -> None:
    payload = _payload(
        (_fragment(1, "Frame text", kind="frame", start=5),),
        on_screen_text=["Duplicated frame text"],
    )

    message = _build_user_message(payload)

    assert "Duplicated frame text" not in message


def _span(
    index: int,
    *,
    kind: Literal["transcript", "frame"] = "transcript",
    text: str = "evidence",
) -> ProjectedEvidenceSpan:
    prefix = "T" if kind == "transcript" else "F"
    return ProjectedEvidenceSpan(
        alias=f"{prefix}{index:06d}",
        kind=kind,
        start_seconds=float(index),
        end_seconds=float(index + 1) if kind == "transcript" else None,
        text=text,
        fragment_ids=(f"fragment-{index}",),
    )


def test_chunk_projection_with_neighbor_context_and_core_ownership() -> None:
    projection = VideoPromptProjection(spans=tuple(_span(index, text="x" * 100) for index in range(1, 5)))
    one_span_budget = len(projection.spans[0].model_dump_json())

    chunks = chunk_projection(projection, one_span_budget)

    assert [([span.alias for span in chunk.spans], chunk.core_aliases) for chunk in chunks] == [
        (["T000001", "T000002"], frozenset({"T000001"})),
        (["T000001", "T000002", "T000003"], frozenset({"T000002"})),
        (["T000002", "T000003", "T000004"], frozenset({"T000003"})),
        (["T000003", "T000004"], frozenset({"T000004"})),
    ]


def test_chunk_projection_is_deterministic() -> None:
    projection = VideoPromptProjection(spans=tuple(_span(index, text="x" * 100) for index in range(1, 5)))
    budget = len(projection.spans[0].model_dump_json()) * 2

    first = chunk_projection(projection, budget)
    second = chunk_projection(projection, budget)

    assert first == second


def test_chunk_projection_ignores_large_durable_fragment_mapping_in_prompt_budget() -> None:
    span = ProjectedEvidenceSpan(
        alias="T000001",
        kind="transcript",
        start_seconds=0,
        end_seconds=1,
        text="Small model-visible text",
        fragment_ids=tuple(f"fragment-{index}" for index in range(10_000)),
    )
    projection = VideoPromptProjection(spans=(span,))
    rendered_size = len(__import__("json").dumps(span.prompt_record(core=True), separators=(",", ":"))) + 1

    chunks = chunk_projection(projection, rendered_size)

    assert len(chunks) == 1


def _chunk_claim(
    claim: str,
    tier: Literal["high", "medium", "low"],
    aliases: tuple[str, ...],
    *,
    tickers: tuple[str, ...] = (),
) -> VideoChunkClaimDraft:
    return VideoChunkClaimDraft(
        claim=claim,
        tier=tier,
        category="fundamental",
        tickers_affected=tickers,
        cited_sources=(),
        evidence_aliases=aliases,
    )


def _chunk_draft(
    claims: tuple[VideoChunkClaimDraft, ...],
    *,
    tickers: tuple[str, ...] = (),
    sectors: tuple[str, ...] = (),
    themes: tuple[str, ...] = (),
) -> VideoChunkDraft:
    return VideoChunkDraft(
        summary="Chunk summary",
        claims=claims,
        tickers_mentioned=tickers,
        sectors_mentioned=sectors,
        macro_themes=themes,
    )


def test_compose_video_drafts_with_unknown_alias() -> None:
    span = _span(1)
    projection = VideoPromptProjection(spans=(span,))
    chunk = VideoPromptChunk(index=0, spans=(span,), core_aliases=frozenset({span.alias}))
    draft = _chunk_draft((_chunk_claim("Claim", "high", ("T999999",)),))

    with pytest.raises(UnknownEvidenceAliasError):
        compose_video_drafts(_payload(), projection, (chunk,), (draft,), "Summary")


def test_compose_video_drafts_resolves_aliases_to_durable_fragment_ids() -> None:
    first, second = _span(1), _span(2)
    projection = VideoPromptProjection(spans=(first, second))
    chunk = VideoPromptChunk(
        index=0,
        spans=(first, second),
        core_aliases=frozenset({first.alias, second.alias}),
    )
    draft = _chunk_draft((_chunk_claim("Claim", "high", (second.alias, first.alias, second.alias)),))

    result = compose_video_drafts(_payload(), projection, (chunk,), (draft,), "Summary")

    assert result.claims[0].evidence_fragment_ids == ["fragment-2", "fragment-1"]


def test_compose_video_drafts_discards_context_owned_claim_and_exact_duplicate() -> None:
    first, second = _span(1), _span(2)
    projection = VideoPromptProjection(spans=(first, second))
    chunks = (
        VideoPromptChunk(index=0, spans=(first, second), core_aliases=frozenset({first.alias})),
        VideoPromptChunk(index=1, spans=(first, second), core_aliases=frozenset({second.alias})),
    )
    duplicate = _chunk_claim("Owned by first", "high", (first.alias,))
    drafts = (
        _chunk_draft((duplicate,)),
        _chunk_draft((duplicate, _chunk_claim("Owned by second", "low", (second.alias,)))),
    )

    result = compose_video_drafts(_payload(), projection, chunks, drafts, "Summary")

    assert [claim.claim for claim in result.claims] == ["Owned by first", "Owned by second"]


def test_compose_video_drafts_orders_by_tier_then_evidence_and_reindexes() -> None:
    spans = (_span(1), _span(2), _span(3))
    projection = VideoPromptProjection(spans=spans)
    chunk = VideoPromptChunk(index=0, spans=spans, core_aliases=frozenset(span.alias for span in spans))
    draft = _chunk_draft(
        (
            _chunk_claim("Low first", "low", (spans[0].alias,)),
            _chunk_claim("High later", "high", (spans[2].alias,)),
            _chunk_claim("High earlier", "high", (spans[1].alias,)),
        )
    )

    result = compose_video_drafts(_payload(), projection, (chunk,), (draft,), "Summary")

    assert [(claim.claim_id, claim.claim) for claim in result.claims] == [
        ("yt_test_001:S001", "High earlier"),
        ("yt_test_001:S002", "High later"),
        ("yt_test_001:S003", "Low first"),
    ]


def test_compose_video_drafts_unions_mentions_in_first_seen_order() -> None:
    first, second = _span(1), _span(2)
    projection = VideoPromptProjection(spans=(first, second))
    chunks = (
        VideoPromptChunk(index=0, spans=(first,), core_aliases=frozenset({first.alias})),
        VideoPromptChunk(index=1, spans=(second,), core_aliases=frozenset({second.alias})),
    )
    drafts = (
        _chunk_draft((), tickers=("NVDA", "AMD"), sectors=("Technology",), themes=("AI",)),
        _chunk_draft(
            (),
            tickers=("AMD", "MSFT"),
            sectors=("Technology", "Utilities"),
            themes=("AI", "Rates"),
        ),
    )

    result = compose_video_drafts(_payload(), projection, chunks, drafts, "Summary")

    assert (result.tickers_mentioned, result.sectors_mentioned, result.macro_themes) == (
        ["NVDA", "AMD", "MSFT"],
        ["Technology", "Utilities"],
        ["AI", "Rates"],
    )


def test_project_video_evidence_with_1652_rolling_cues_stays_within_default_budget() -> None:
    cue_count = 1_652
    fragments = tuple(
        _fragment(
            index,
            f"word{index} word{index + 1} word{index + 2}",
            start=index * 0.5,
            end=index * 0.5 + 1,
        )
        for index in range(cue_count)
    )
    payload = _payload(fragments)

    projection = project_video_evidence(payload)
    message = _build_user_message(payload)

    assert len(message) < 160_000
    assert {fragment_id for span in projection.spans for fragment_id in span.fragment_ids} == {
        fragment.fragment_id for fragment in fragments
    }
