"""Fixtures for pipeline integration tests."""
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def pipeline_signals_dir(data_folder: Path) -> Path:
    """Path to the directory of canned signal fixtures used by pipeline tests."""
    return data_folder / "pipeline" / "signals"
