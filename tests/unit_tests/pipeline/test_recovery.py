"""Tests for money_pit.pipeline.recovery."""

from pathlib import Path

import pytest

from money_pit.pipeline.recovery import recover_prior_state


def test_recover_prior_state(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        recover_prior_state(tmp_path)
