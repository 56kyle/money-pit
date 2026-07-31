import json
import uuid
from datetime import UTC
from datetime import datetime

import pytest

from money_pit.legacy.daily_show import LegacyDailyShowImporter
from money_pit.runs.manifest import create_run
from money_pit.runs.paths import RepositoryPaths
from money_pit.runs.repository import RunManifestReadError
from money_pit.runs.repository import RunNotFoundError
from money_pit.runs.repository import RunRepository
from money_pit.storage.database import Database


@pytest.fixture
def run_repository(tmp_path):
    paths = RepositoryPaths.from_data_root(
        tmp_path / "data",
        legacy_daily_show_root=tmp_path / "legacy",
    )
    database = Database(paths.database_path)
    database.initialize()
    return RunRepository(paths, database), paths, database


def test_resolve_round_trips_native_manifest(run_repository):
    repository, paths, _ = run_repository
    manifest, run_dir = create_run(paths, created_at=datetime(2026, 7, 29, tzinfo=UTC))

    resolved = repository.resolve(str(manifest.run_id))

    assert (resolved.path, resolved.created_at, resolved.origin) == (
        run_dir,
        manifest.created_at,
        "native",
    )


def test_resolve_falls_back_to_indexed_legacy_run(run_repository):
    repository, paths, database = run_repository
    legacy_dir = paths.legacy_daily_show_root / "2026-07-29_12-30-00"
    legacy_dir.mkdir(parents=True)
    LegacyDailyShowImporter(database, paths.legacy_daily_show_root).index()
    with database.transaction() as connection:
        legacy_id = connection.execute("SELECT legacy_run_id FROM legacy_runs").fetchone()[0]

    resolved = repository.resolve(legacy_id)

    assert resolved.path == legacy_dir
    assert resolved.origin == "legacy"


def test_resolve_with_missing_run_raises(run_repository):
    repository, _, _ = run_repository

    with pytest.raises(RunNotFoundError):
        repository.resolve("missing")


@pytest.mark.parametrize(
    "run_id",
    [
        str(uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")),
        str(uuid.uuid4()),
    ],
)
def test_resolve_with_non_native_identifier_falls_through_to_repository(
    run_repository,
    run_id,
):
    repository, _, _ = run_repository

    with pytest.raises(RunNotFoundError):
        repository.resolve(run_id)


@pytest.mark.parametrize(
    "payload",
    [
        "not-json",
        "[]",
        json.dumps({"schema_version": 1}),
        json.dumps(
            {
                "schema_version": 1,
                "run_id": str(uuid.uuid4()),
                "created_at": "2026-07-29T00:00:00+00:00",
            }
        ),
    ],
)
def test_resolve_rejects_invalid_or_mismatched_native_manifest(run_repository, payload):
    repository, paths, _ = run_repository
    run_id = uuid.uuid4()
    run_dir = paths.runs_root / str(run_id)
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(payload, encoding="utf-8")

    with pytest.raises(RunManifestReadError):
        repository.resolve(str(run_id))
