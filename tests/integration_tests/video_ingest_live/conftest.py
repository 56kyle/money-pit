"""Fixtures for the opt-in live video-ingestion integration tier.

These tests run the real, expensive ingestion pipeline (yt-dlp download, faster-whisper/captions,
PySceneDetect, OpenCV, and Claude VLM) against a pinned public YouTube URL. They are marked
@pytest.mark.live_video, which is deselected by default, and are additionally skipped unless
MONEY_PIT_LIVE=1 and the `video` optional extra is installed.
"""

import pytest

from money_pit.config import Config
from money_pit.config import load_config


@pytest.fixture
def live_config() -> Config:
    """Load the live-tier Config.

    The session-autouse integration_env fixture supplies placeholder MONEY_PIT__ALPACA_* so Config
    constructs; the VLM/classifier resolve the operator's ambient ANTHROPIC credentials at run time.
    """
    return load_config()
