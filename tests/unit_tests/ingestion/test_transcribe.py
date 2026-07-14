"""Unit tests for money_pit.ingestion.transcribe — whisper segment joining."""

from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from money_pit.ingestion.transcribe import _whisper_segments_to_transcript


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        ([" Hello ", "world"], "Hello world"),
        (["a", "   ", "b"], "a b"),
        ([], ""),
        (["", "  ", "\t\n"], ""),
        (["  spaced  out  "], "spaced  out"),
        (["one two", "three four"], "one two three four"),
    ],
)
def test__whisper_segments_to_transcript_with_valid(texts: list[str], expected: str) -> None:
    segments = [SimpleNamespace(text=text) for text in texts]

    assert _whisper_segments_to_transcript(segments) == expected


def test__whisper_segments_to_transcript_with_generator() -> None:
    def segment_stream() -> Iterator[SimpleNamespace]:
        for text in [" Hello ", "   ", "world"]:
            yield SimpleNamespace(text=text)

    assert _whisper_segments_to_transcript(segment_stream()) == "Hello world"
