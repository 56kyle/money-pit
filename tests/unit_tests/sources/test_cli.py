from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit.progress import IngestionProgressCallback
from money_pit.progress import IngestionProgressEvent
from money_pit.progress import IngestionProgressStage
from money_pit.sources.cli import source_app
from money_pit.sources.service import SourceIngestResult


class _IngestService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []
        self.progress: IngestionProgressCallback | None = None

    def ingest(self, source_id: str, url: str, *, refresh: bool = False) -> SourceIngestResult:
        self.calls.append((source_id, url, refresh))
        if self.progress is not None:
            self.progress(
                IngestionProgressEvent(
                    stage=IngestionProgressStage.DOWNLOAD_PROGRESS,
                    current=50,
                    total=100,
                ),
            )
        return SourceIngestResult(
            source_id=source_id,
            source_item_id="youtube:dQw4w9WgXcQ",
            ingested_count=1,
            evidence_document_count=1,
        )


def test_ingest_command_passes_the_source_and_url_to_direct_ingestion(monkeypatch: MonkeyPatch) -> None:
    service = _IngestService()

    def source_runtime(
        sources_path: Path,
        *,
        progress: IngestionProgressCallback | None = None,
    ) -> tuple[_IngestService, object]:
        del sources_path
        service.progress = progress
        return service, object()

    monkeypatch.setattr(
        "money_pit.sources.cli._source_runtime",
        source_runtime,
    )
    url = "https://youtu.be/dQw4w9WgXcQ"

    result = CliRunner().invoke(source_app, ["ingest", "youtube", url])

    assert (result.exit_code, service.calls, "source item id: youtube:dQw4w9WgXcQ" in result.stdout) == (
        0,
        [("youtube", url, False)],
        True,
    )


def test_ingest_command_sends_progress_to_stderr_and_keeps_final_json_on_stdout(
    monkeypatch: MonkeyPatch,
) -> None:
    service = _IngestService()

    def source_runtime(
        sources_path: Path,
        *,
        progress: IngestionProgressCallback | None = None,
    ) -> tuple[_IngestService, object]:
        del sources_path
        service.progress = progress
        return service, object()

    monkeypatch.setattr("money_pit.sources.cli._source_runtime", source_runtime)

    result = CliRunner().invoke(
        source_app,
        ["ingest", "youtube", "https://youtu.be/dQw4w9WgXcQ", "--refresh"],
    )

    assert result.exit_code == 0
    assert service.calls == [("youtube", "https://youtu.be/dQw4w9WgXcQ", True)]
    assert "download progress: 50.0%" in result.stderr
    assert "download progress" not in result.stdout
