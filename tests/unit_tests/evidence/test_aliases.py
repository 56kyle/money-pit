import json
from typing import Literal

import pytest

from money_pit.evidence.aliases import project_evidence
from money_pit.evidence.errors import EvidenceProjectionError
from money_pit.evidence.errors import UnknownEvidenceAliasError
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TextLocator
from money_pit.schemas.evidence import TimestampLocator


def _fragment(
    fragment_id: str,
    *,
    extracted_text: str | None = None,
    cited_source_text: str | None = None,
) -> EvidenceFragment:
    return EvidenceFragment(
        fragment_id=fragment_id,
        asset_id="a" * 64,
        kind="web_span",
        locator=TextLocator(start_offset=0, end_offset=len(extracted_text or cited_source_text or "")),
        extracted_text=extracted_text,
        cited_source_text=cited_source_text,
        extraction_method="test",
    )


def _timestamped_fragment(
    fragment_id: str,
    *,
    kind: Literal["transcript", "frame"] = "transcript",
    text: str | None = None,
    cited_source_text: str | None = None,
    start_seconds: float,
    end_seconds: float | None = None,
    asset_id: str = "a" * 64,
) -> EvidenceFragment:
    return EvidenceFragment(
        fragment_id=fragment_id,
        asset_id=asset_id,
        kind=kind,
        locator=TimestampLocator(
            start_seconds=start_seconds,
            end_seconds=end_seconds,
        ),
        extracted_text=text,
        cited_source_text=cited_source_text,
        extraction_method="test",
    )


def test_project_evidence_assigns_deterministic_aliases_to_complete_bounded_spans() -> None:
    projection = project_evidence(
        (
            _fragment("fragment-1", extracted_text="abcdef"),
            _fragment("fragment-2", cited_source_text="xy"),
            _fragment("empty"),
        ),
        alias_text_limit=3,
    )

    assert tuple((item.alias, item.text, item.fragment_ids) for item in projection.evidence) == (
        ("E000001", "abc", ("fragment-1",)),
        ("E000002", "def", ("fragment-1",)),
        ("E000003", "xy", ("fragment-2",)),
    )


def test_resolve_preserves_first_reference_order_and_deduplicates_fragment_ids() -> None:
    projection = project_evidence(
        (_fragment("fragment-1", extracted_text="abcdef"), _fragment("fragment-2", extracted_text="x")),
        alias_text_limit=3,
    )

    assert projection.resolve(("E000002", "E000003", "E000001")) == (
        "fragment-1",
        "fragment-2",
    )


def test_resolve_rejects_an_unknown_model_visible_alias() -> None:
    projection = project_evidence((_fragment("fragment-1", extracted_text="text"),))

    with pytest.raises(UnknownEvidenceAliasError):
        _ = projection.resolve(("E999999",))


def test_chunks_never_split_a_model_visible_record() -> None:
    projection = project_evidence(
        (_fragment("fragment-1", extracted_text="one"), _fragment("fragment-2", extracted_text="two")),
    )
    one_record_budget = len(
        json.dumps(
            [projection.evidence[0].prompt_record(core=True)],
            separators=(",", ":"),
        ),
    )

    chunks = projection.chunks(one_record_budget)

    assert tuple(tuple(item.alias for item in chunk.evidence) for chunk in chunks) == (
        ("E000001",),
        ("E000002",),
    )
    assert all(len(json.dumps(chunk.prompt_records(), separators=(",", ":"))) <= one_record_budget for chunk in chunks)


def test_chunks_rejects_a_record_larger_than_the_budget() -> None:
    projection = project_evidence((_fragment("fragment-1", extracted_text="text"),))

    with pytest.raises(EvidenceProjectionError):
        _ = projection.chunks(2)


def test_project_evidence_removes_the_longest_normalized_rolling_caption_overlap() -> None:
    projection = project_evidence(
        (
            _timestamped_fragment(
                "cue-2",
                text="GAMMA delta! epsilon zeta",
                start_seconds=2,
                end_seconds=4,
            ),
            _timestamped_fragment(
                "cue-1",
                text="Alpha beta, gamma delta",
                start_seconds=0,
                end_seconds=2,
            ),
        ),
    )

    assert tuple(
        (item.text, item.start_seconds, item.end_seconds, item.fragment_ids) for item in projection.evidence
    ) == (("Alpha beta, gamma delta epsilon zeta", 0.0, 4.0, ("cue-1", "cue-2")),)
    assert projection.resolve(("T000001",)) == ("cue-1", "cue-2")


def test_project_evidence_orders_timestamped_media_before_untimed_evidence() -> None:
    projection = project_evidence(
        (
            _fragment("page", extracted_text="untimed"),
            _timestamped_fragment("late", text="late cue", start_seconds=8, end_seconds=9),
            _timestamped_fragment(
                "frame",
                kind="frame",
                text="early frame",
                start_seconds=1,
            ),
            _timestamped_fragment("early", text="early cue", start_seconds=3, end_seconds=4),
        ),
    )

    assert tuple(item.alias for item in projection.evidence) == (
        "F000001",
        "T000001",
        "E000001",
    )
    assert tuple(item.start_seconds for item in projection.evidence) == (1.0, 3.0, None)


def test_project_evidence_groups_frame_text_and_source_labels_by_asset_and_timestamp() -> None:
    projection = project_evidence(
        (
            _timestamped_fragment(
                "frame-text",
                kind="frame",
                text="Revenue +20%",
                start_seconds=5,
            ),
            _timestamped_fragment(
                "frame-source",
                kind="frame",
                cited_source_text="SEC 10-Q",
                start_seconds=5,
            ),
            _timestamped_fragment(
                "other-asset",
                kind="frame",
                text="Separate frame",
                start_seconds=5,
                asset_id="b" * 64,
            ),
        ),
    )

    grouped = projection.evidence[0]
    assert (grouped.text, grouped.fragment_ids, grouped.start_seconds) == (
        "Revenue +20% | SEC 10-Q",
        ("frame-text", "frame-source"),
        5.0,
    )
    assert projection.resolve((grouped.alias,)) == ("frame-text", "frame-source")


def test_chunks_enforce_the_exact_serialized_bound_and_unique_core_ownership() -> None:
    projection = project_evidence(
        tuple(
            _fragment(f"fragment-{index}", extracted_text=f"evidence record {index} with bounded text")
            for index in range(8)
        ),
    )
    character_budget = 180

    chunks = projection.chunks(character_budget)

    assert all(len(json.dumps(chunk.prompt_records(), separators=(",", ":"))) <= character_budget for chunk in chunks)
    owned = tuple(alias for chunk in chunks for alias in chunk.core_aliases)
    assert sorted(owned) == sorted(item.alias for item in projection.evidence)
    assert len(owned) == len(set(owned))
    assert all(chunk.core_aliases <= {item.alias for item in chunk.evidence} for chunk in chunks)


def test_project_evidence_compacts_1652_rolling_cues_with_bounded_complete_provenance() -> None:
    cue_count = 1_652
    fragments = tuple(
        _timestamped_fragment(
            f"cue-{index:04d}",
            text=" ".join(f"token{token:04d}" for token in range(index * 2, index * 2 + 4)),
            start_seconds=float(index),
            end_seconds=float(index + 1),
        )
        for index in range(cue_count)
    )

    projection = project_evidence(reversed(fragments))

    assert all(len(item.text) <= 600 for item in projection.evidence)
    assert all(
        item.start_seconds is not None and item.end_seconds is not None and item.end_seconds - item.start_seconds <= 20
        for item in projection.evidence
    )
    assert projection.resolve(item.alias for item in projection.evidence) == tuple(
        fragment.fragment_id for fragment in fragments
    )
    assert " ".join(item.text for item in projection.evidence).split() == [
        f"token{index:04d}" for index in range(cue_count * 2 + 2)
    ]
