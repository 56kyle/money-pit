"""Failure-path tests for non-cue and blank WebVTT blocks."""

from money_pit.ingestion.captions import _split_blocks
from money_pit.ingestion.captions import parse_vtt


def test_parse_vtt_with_unrecognized_non_cue_block_ignores_it() -> None:
    assert parse_vtt("WEBVTT\n\nidentifier without timing\n") == []


def test__split_blocks_with_only_blank_lines_returns_empty() -> None:
    assert _split_blocks("\n\n \n") == []
