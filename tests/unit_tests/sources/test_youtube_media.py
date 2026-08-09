import hashlib
import json
import sys
from datetime import UTC
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from typing import cast


if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

import pytest
from pydantic import SecretStr
from pytest import MonkeyPatch

from money_pit.evidence.media import MediaAnalysis
from money_pit.schemas.sources import AllowedUse
from money_pit.schemas.sources import SourceCursor
from money_pit.schemas.sources import SourceCursorPurpose
from money_pit.schemas.sources import SourceDefinition
from money_pit.schemas.sources import SourceItem
from money_pit.schemas.sources import SourceTrustSetting
from money_pit.schemas.sources import TrustCategory
from money_pit.schemas.sources import TrustLevel
from money_pit.sources._shared import source_definition_hash
from money_pit.sources.errors import SourceDiscoveryError
from money_pit.sources.errors import SourceFetchError
from money_pit.sources.http import HttpResponse
from money_pit.sources.http import HttpTransport
from money_pit.sources.youtube import YouTubeConnector
from money_pit.sources.youtube import YtDlpMediaTransport


NOW = datetime(2026, 8, 9, tzinfo=UTC)
VIDEO = b"\x00\x00\x00\x18ftypmp42actual-video"


class _UnusedHttpTransport:
    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        raise AssertionError((url, maximum_bytes, timeout_seconds))


class _StaticHttpTransport:
    def __init__(self, response: HttpResponse) -> None:
        self._response: HttpResponse = response
        self.calls: list[str] = []

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        del maximum_bytes, timeout_seconds
        self.calls.append(url)
        return self._response


class _SequenceHttpTransport:
    def __init__(self, responses: tuple[HttpResponse, ...]) -> None:
        self._responses: Iterator[HttpResponse] = iter(responses)
        self.calls: list[str] = []

    def get(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> HttpResponse:
        del maximum_bytes, timeout_seconds
        self.calls.append(url)
        return next(self._responses)


class _MediaTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, float]] = []

    def fetch(self, url: str, *, maximum_bytes: int, timeout_seconds: float) -> tuple[bytes, str, str]:
        self.calls.append((url, maximum_bytes, timeout_seconds))
        return VIDEO, "video/mp4", "video.mp4"


class _UnusedMediaAnalyzer:
    def analyze(self, path: Path, *, media_type: str) -> MediaAnalysis:
        raise AssertionError((path, media_type))


def _definition() -> SourceDefinition:
    return SourceDefinition(
        source_id="youtube",
        adapter_name="youtube",
        locator="uploads-playlist",
        provenance_group="channel",
        allowed_uses=(AllowedUse.INTERPRETATION,),
        trust_settings=(SourceTrustSetting(category=TrustCategory.FACTUAL, level=TrustLevel.COMMENTARY),),
        adapter_config={
            "max_media_bytes": 1_024,
            "timeout_seconds": 7,
        },
    )


def _connector(
    definition: SourceDefinition,
    transport: HttpTransport,
    media_transport: _MediaTransport,
) -> YouTubeConnector:
    return YouTubeConnector(
        definition,
        SecretStr("configured-key"),
        _UnusedMediaAnalyzer(),
        transport,
        media_transport,
    )


def _item(
    definition: SourceDefinition,
    *,
    source_id: str = "youtube",
    definition_hash: str | None = None,
) -> SourceItem:
    return SourceItem(
        source_item_id="youtube:playlist-item",
        source_id=source_id,
        source_definition_hash=definition_hash or source_definition_hash(definition),
        canonical_uri="https://www.youtube.com/watch?v=video-id",
        discovered_at=NOW,
        content_version="playlist-item",
    )


def test_youtube_connector_fetch_returns_injected_video_media_not_watch_html() -> None:
    definition = _definition()
    media_transport = _MediaTransport()
    connector = _connector(definition, _UnusedHttpTransport(), media_transport)

    artifact = connector.fetch(_item(definition))

    assert (artifact.content, artifact.media_type, artifact.filename) == (
        VIDEO,
        "video/mp4",
        Path("video.mp4"),
    )
    assert b"<html" not in artifact.content
    assert artifact.source_item.content_version == hashlib.sha256(VIDEO).hexdigest()
    assert media_transport.calls == [
        ("https://www.youtube.com/watch?v=video-id", 1_024, 7.0),
    ]


@pytest.mark.parametrize(
    ("source_id", "definition_hash"),
    [("other-source", None), ("youtube", "f" * 64)],
)
def test_youtube_connector_fetch_rejects_an_item_outside_its_source_identity(
    source_id: str,
    definition_hash: str | None,
) -> None:
    definition = _definition()
    media_transport = _MediaTransport()
    connector = _connector(definition, _UnusedHttpTransport(), media_transport)

    with pytest.raises(SourceFetchError):
        _ = connector.fetch(
            _item(
                definition,
                source_id=source_id,
                definition_hash=definition_hash,
            ),
        )

    assert media_transport.calls == []


def test_youtube_discovery_uses_the_stable_video_id_not_playlist_item_identity() -> None:
    definition = _definition()
    response = {
        "items": [
            {
                "id": "mutable-playlist-item",
                "snippet": {
                    "publishedAt": "2026-08-09T00:00:00Z",
                    "resourceId": {"videoId": "stable-video-id"},
                },
            },
        ],
    }
    connector = _connector(
        definition,
        _StaticHttpTransport(
            HttpResponse(
                json.dumps(response).encode(),
                "application/json",
                "https://example.test",
            ),
        ),
        _MediaTransport(),
    )

    item = connector.discover(None).items[0]

    assert (item.source_item_id, item.content_version) == (
        "youtube:stable-video-id",
        "stable-video-id",
    )


def test_youtube_discovery_uses_page_tokens_only_for_backfill() -> None:
    transport = _SequenceHttpTransport(
        (
            HttpResponse(b'{"items":[]}', "application/json", "https://example.test"),
            HttpResponse(
                b'{"nextPageToken":"older-page","items":[]}',
                "application/json",
                "https://example.test",
            ),
        )
    )
    connector = _connector(_definition(), transport, _MediaTransport())
    sync_cursor = SourceCursor(
        value=json.dumps({"high_water_video_id": "saved-watermark", "pending_ranges": []}),
    )
    backfill_cursor = SourceCursor(value="saved-page")

    sync = connector.discover(sync_cursor, purpose=SourceCursorPurpose.SYNC)
    backfill = connector.discover(backfill_cursor, purpose=SourceCursorPurpose.BACKFILL)

    assert "pageToken" not in transport.calls[0]
    assert sync.next_cursor is not None
    assert "pageToken=saved-page" in transport.calls[1]
    assert backfill.next_cursor == SourceCursor(value="older-page")


@pytest.mark.parametrize("cursor_value", ["plain-watermark", '{"high_water_video_id":1}'])
def test_youtube_sync_rejects_a_plain_or_corrupt_cursor(
    cursor_value: str,
) -> None:
    connector = _connector(_definition(), _SequenceHttpTransport(()), _MediaTransport())

    with pytest.raises(SourceDiscoveryError):
        _ = connector.discover(SourceCursor(value=cursor_value), purpose=SourceCursorPurpose.SYNC)


def test_youtube_sync_pages_until_the_previous_high_water_after_an_upload_burst() -> None:
    def page(video_ids: tuple[str, ...], *, next_page: str | None = None) -> HttpResponse:
        payload: dict[str, object] = {
            "items": [
                {
                    "id": f"playlist-{video_id}",
                    "snippet": {
                        "publishedAt": "2026-08-09T00:00:00Z",
                        "resourceId": {"videoId": video_id},
                    },
                }
                for video_id in video_ids
            ],
        }
        if next_page is not None:
            payload["nextPageToken"] = next_page
        return HttpResponse(json.dumps(payload).encode(), "application/json", "https://example.test")

    transport = _SequenceHttpTransport(
        (
            page(("old-watermark",)),
            page(("new-3", "new-2"), next_page="burst-page-2"),
            page(("new-1", "old-watermark")),
        )
    )
    connector = _connector(_definition(), transport, _MediaTransport())
    initial = connector.discover(None, purpose=SourceCursorPurpose.SYNC)
    assert initial.next_cursor is not None

    burst = connector.discover(initial.next_cursor, purpose=SourceCursorPurpose.SYNC)

    assert tuple(item.source_item_id for item in burst.items) == (
        "youtube:new-3",
        "youtube:new-2",
        "youtube:new-1",
    )
    assert "pageToken=burst-page-2" in transport.calls[2]
    assert burst.next_cursor is not None
    cursor_state = cast("dict[str, object]", json.loads(burst.next_cursor.value))
    assert cursor_state == {"high_water_video_id": "new-3", "pending_ranges": []}


class _FakeYoutubeDl:
    extension: str = "mp4"
    invoke_hook: bool = False

    def __init__(self, options: dict[str, object]) -> None:
        self._options: dict[str, object] = options

    def __enter__(self) -> "_FakeYoutubeDl":
        return self

    def __exit__(self, *exc: object) -> None:
        del exc

    def extract_info(self, url: str, *, download: bool) -> dict[str, object]:
        del url, download
        if self.invoke_hook:
            hooks = cast("list[object]", self._options["progress_hooks"])
            hook = cast("Callable[[dict[str, object]], object]", hooks[0])
            if callable(hook):
                _ = hook({})
        output = str(self._options["outtmpl"]).replace("%(ext)s", self.extension)
        _ = Path(output).write_bytes(VIDEO)
        return {}


@pytest.mark.parametrize(
    ("extension", "expected_media_type"),
    [("m4a", "audio/mp4"), ("mkv", "video/x-matroska"), ("webm", "video/webm")],
)
def test_yt_dlp_media_transport_maps_the_downloaded_container_mime(
    monkeypatch: MonkeyPatch,
    extension: str,
    expected_media_type: str,
) -> None:
    _FakeYoutubeDl.extension = extension
    _FakeYoutubeDl.invoke_hook = False
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_FakeYoutubeDl))

    _, media_type, filename = YtDlpMediaTransport().fetch(
        "https://www.youtube.com/watch?v=video-id",
        maximum_bytes=1_024,
        timeout_seconds=7,
    )

    assert (media_type, Path(filename).suffix) == (expected_media_type, f".{extension}")


def test_yt_dlp_media_transport_normalizes_elapsed_hook_timeout(
    monkeypatch: MonkeyPatch,
) -> None:
    _FakeYoutubeDl.extension = "mp4"
    _FakeYoutubeDl.invoke_hook = True
    monkeypatch.setitem(sys.modules, "yt_dlp", SimpleNamespace(YoutubeDL=_FakeYoutubeDl))
    elapsed = iter((0.0, 8.0))
    monkeypatch.setattr("money_pit.sources.youtube.monotonic", lambda: next(elapsed))

    with pytest.raises(SourceFetchError):
        _ = YtDlpMediaTransport().fetch(
            "https://www.youtube.com/watch?v=video-id",
            maximum_bytes=1_024,
            timeout_seconds=7,
        )
