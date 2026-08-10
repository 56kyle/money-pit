from pathlib import Path

from pytest import MonkeyPatch
from typer.testing import CliRunner

from money_pit.sources.cli import source_app
from money_pit.sources.service import SourceIngestResult


class _IngestService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ingest(self, source_id: str, url: str) -> SourceIngestResult:
        self.calls.append((source_id, url))
        return SourceIngestResult(
            source_id=source_id,
            source_item_id="youtube:dQw4w9WgXcQ",
            ingested_count=1,
            evidence_document_count=1,
        )


def test_ingest_command_passes_the_source_and_url_to_direct_ingestion(monkeypatch: MonkeyPatch) -> None:
    service = _IngestService()

    def source_runtime(sources_path: Path) -> tuple[_IngestService, object]:
        del sources_path
        return service, object()

    monkeypatch.setattr(
        "money_pit.sources.cli._source_runtime",
        source_runtime,
    )
    url = "https://youtu.be/dQw4w9WgXcQ"

    result = CliRunner().invoke(source_app, ["ingest", "youtube", url])

    assert (result.exit_code, service.calls, '"source_item_id": "youtube:dQw4w9WgXcQ"' in result.stdout) == (
        0,
        [("youtube", url)],
        True,
    )
