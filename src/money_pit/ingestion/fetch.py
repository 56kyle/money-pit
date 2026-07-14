"""Module containing the yt-dlp video-fetch seam and its pure metadata-to-SourceRef mapping for the money_pit package."""

import json
from collections.abc import Callable
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import TypeAlias

from money_pit.ingestion.artifacts import VideoArtifacts
from money_pit.schemas.enums import SourceType
from money_pit.schemas.provenance import SourceRef


Downloader: TypeAlias = Callable[[str, Path], VideoArtifacts]

_UPLOAD_DATE_FORMAT: str = "%Y%m%d"
_ISO_UTC_FORMAT: str = "%Y-%m-%dT%H:%M:%SZ"
_SOURCE_ID_PREFIX: str = "yt"

_METADATA_FILENAME: str = "metadata.json"
_OUTTMPL_TEMPLATE: str = "media.%(ext)s"
_YTDLP_FORMAT: str = "bestvideo[height<=480]+bestaudio/best[height<=480]"
_SUBTITLE_LANGS: tuple[str, ...] = ("en",)
_METADATA_KEYS: tuple[str, ...] = ("id", "title", "webpage_url", "upload_date", "uploader", "duration")

_AUDIO_GLOB: str = "media.m4a"
_AUDIO_FALLBACK_GLOB: str = "media.*"
_VIDEO_GLOB: str = "media.mp4"
_UPLOADER_CAPTION_GLOB: str = "*.en.vtt"
_AUTO_CAPTION_GLOB: str = "*.en.auto.vtt"


def _upload_date_to_iso(upload_date: str | None) -> str | None:
    """Convert a yt-dlp `YYYYMMDD` upload date to an ISO-8601 UTC-midnight string, or None when absent."""
    if not upload_date:
        return None
    parsed: datetime = datetime.strptime(upload_date, _UPLOAD_DATE_FORMAT).replace(tzinfo=timezone.utc)
    return parsed.strftime(_ISO_UTC_FORMAT)


def _build_source_ref(info: dict[str, object], url: str, retrieved_at: str) -> SourceRef:
    """Map a yt-dlp info dict onto a SourceRef, using the passed url and retrieved_at as pure inputs."""
    title_value: object = info.get("title")
    title: str = str(title_value) if title_value is not None else ""

    webpage_url: object = info.get("webpage_url")
    resolved_url: str = str(webpage_url) if webpage_url is not None else url

    upload_date_value: object = info.get("upload_date")
    upload_date: str | None = upload_date_value if isinstance(upload_date_value, str) else None

    return SourceRef(
        source_id=f"{_SOURCE_ID_PREFIX}:{info['id']}",
        source_type=SourceType.NARRATED_VIDEO,
        title=title,
        url=resolved_url,
        published_at=_upload_date_to_iso(upload_date),
        retrieved_at=retrieved_at,
        locator=None,
    )


def _first_match(out_dir: Path, pattern: str) -> Path | None:
    """Return the first path under out_dir matching pattern, or None when nothing matches."""
    return next(iter(sorted(out_dir.glob(pattern))), None)


def make_ytdlp_downloader() -> Downloader:
    """Return a Downloader that lazily loads yt-dlp and fetches a video's media, captions, and metadata into out_dir."""

    def download(url: str, out_dir: Path) -> VideoArtifacts:  # pragma: no cover
        import yt_dlp

        options: dict[str, object] = {
            "format": _YTDLP_FORMAT,
            "outtmpl": str(out_dir / _OUTTMPL_TEMPLATE),
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": list(_SUBTITLE_LANGS),
            "quiet": True,
        }
        with yt_dlp.YoutubeDL(options) as ydl:
            info: dict[str, object] = ydl.extract_info(url, download=True)

        metadata_path: Path = out_dir / _METADATA_FILENAME
        metadata_subset: dict[str, object] = {key: info.get(key) for key in _METADATA_KEYS}
        metadata_path.write_text(json.dumps(metadata_subset, indent=2), encoding="utf-8")

        retrieved_at: str = datetime.now(timezone.utc).strftime(_ISO_UTC_FORMAT)
        audio_path: Path | None = _first_match(out_dir, _AUDIO_GLOB) or _first_match(out_dir, _AUDIO_FALLBACK_GLOB)
        auto_caption_path: Path | None = _first_match(out_dir, _AUTO_CAPTION_GLOB)
        uploader_caption_path: Path | None = next(
            (path for path in sorted(out_dir.glob(_UPLOADER_CAPTION_GLOB)) if path != auto_caption_path),
            None,
        )

        return VideoArtifacts(
            source_ref=_build_source_ref(info, url, retrieved_at),
            audio_path=audio_path,
            video_path=_first_match(out_dir, _VIDEO_GLOB),
            uploader_caption_path=uploader_caption_path,
            auto_caption_path=auto_caption_path,
            metadata_path=metadata_path,
        )

    return download
