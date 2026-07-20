"""Opt-in live smoke test for the real video-ingestion pipeline.

The single test here downloads a short public YouTube video and makes up to 3 real Claude VLM calls,
so it is marked `live_video` (deselected by default via addopts) and additionally skipped unless
MONEY_PIT_LIVE=1 and the `video` optional extra (yt-dlp, faster-whisper, scenedetect, OpenCV) is
installed. Run explicitly with, e.g., `nox -s tests-python -- -m live_video` and MONEY_PIT_LIVE=1 set.
"""

import os
from importlib.util import find_spec
from pathlib import Path

import pytest

from money_pit.adapters.video_llm import TranscriptSource
from money_pit.adapters.video_llm import VideoPayload
from money_pit.config import Config
from money_pit.ingestion.pipeline import ingest_video
from money_pit.ingestion.pipeline import production_seams


# A short, stable public video chosen for smoke-test stability, not content ("Me at the zoo").
_PINNED_URL: str = "https://www.youtube.com/watch?v=jNQXAC9IVRw"

_VIDEO_EXTRA_MISSING: bool = (
    find_spec("yt_dlp") is None
    or find_spec("faster_whisper") is None
    or find_spec("scenedetect") is None
    or find_spec("cv2") is None
)


pytestmark = [
    pytest.mark.live_video,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE_VIDEO") != "1",
        reason="live_video tier is opt-in; set MONEY_PIT_LIVE_VIDEO=1 (with real OPENAI_API_KEY creds) to run",
    ),
    pytest.mark.skipif(
        _VIDEO_EXTRA_MISSING,
        reason="video extra not installed (needs yt-dlp, faster-whisper, scenedetect, OpenCV)",
    ),
]


def test_live_video_ingest_produces_valid_payload(live_config: Config, tmp_path: Path) -> None:
    """Run the real ingestion pipeline end-to-end and assert it assembles a valid VideoPayload.

    Downloads a short public YouTube video and makes up to 3 real OpenAI VLM calls. Opt-in only:
    requires MONEY_PIT_LIVE=1, the `video` extra, network, a GPU, and OPENAI_API_KEY credentials.
    """
    config = live_config.model_copy(update={"keyframe_max_frames": 3})

    payload = ingest_video(
        _PINNED_URL,
        slug="live-video-smoke",
        cache_dir=tmp_path,
        seams=production_seams(config),
        max_frames=config.keyframe_max_frames,
    )

    assert isinstance(payload, VideoPayload)
    _ = VideoPayload.model_validate(payload.model_dump())
    assert payload.transcript.strip() != ""
    assert payload.transcript_source in set(TranscriptSource)
    assert isinstance(payload.on_screen_text, list)
    assert payload.source_ref.source_id == "yt:jNQXAC9IVRw"
