import json

from money_pit.evidence.aliases import project_evidence
from money_pit.schemas.evidence import EvidenceFragment
from money_pit.schemas.evidence import TimestampLocator


def _transcript_fragment(index: int, text: str) -> EvidenceFragment:
    return EvidenceFragment(
        fragment_id=f"fragment-{index}",
        asset_id="a" * 64,
        kind="transcript",
        locator=TimestampLocator(
            start_seconds=index * 0.5,
            end_seconds=index * 0.5 + 1.0,
        ),
        extracted_text=text,
        extraction_method="caption-track",
    )


def test_project_evidence_compacts_1652_rolling_cues_with_durable_provenance() -> None:
    fragments = tuple(
        _transcript_fragment(
            index,
            f"word{index} word{index + 1} word{index + 2}",
        )
        for index in range(1_652)
    )

    projection = project_evidence(fragments)
    chunks = projection.chunks(800)
    projected_words = [word for evidence in projection.evidence for word in evidence.text.split()]

    assert projected_words == [f"word{index}" for index in range(1_654)]
    assert set(projection.resolve(item.alias for item in projection.evidence)) == {
        fragment.fragment_id for fragment in fragments
    }
    assert all(len(json.dumps(chunk.prompt_records(), separators=(",", ":"))) <= 800 for chunk in chunks)
    assert all(chunk.core_aliases for chunk in chunks)
    owned_aliases = tuple(alias for chunk in chunks for alias in chunk.core_aliases)
    assert len(owned_aliases) == len(set(owned_aliases)) == len(projection.evidence)


def test_project_evidence_groups_frame_text_and_source_under_one_alias() -> None:
    fragments = (
        EvidenceFragment(
            fragment_id="frame-text",
            asset_id="a" * 64,
            kind="frame",
            locator=TimestampLocator(start_seconds=5.0),
            extracted_text="Revenue rose 20%",
            extraction_method="vision",
        ),
        EvidenceFragment(
            fragment_id="frame-source",
            asset_id="a" * 64,
            kind="frame",
            locator=TimestampLocator(start_seconds=5.0),
            cited_source_text="SEC filing",
            extraction_method="vision",
        ),
    )

    projection = project_evidence(fragments)

    assert projection.evidence[0].text == "Revenue rose 20% | SEC filing"
    assert projection.resolve((projection.evidence[0].alias,)) == ("frame-text", "frame-source")
