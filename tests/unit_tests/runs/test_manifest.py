import json
import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from money_pit.runs.errors import RunAlreadyExistsError
from money_pit.runs.errors import RunManifestWriteError
from money_pit.runs.manifest import RunManifest
from money_pit.runs.manifest import create_run
from money_pit.runs.paths import RepositoryPaths


NON_UUID4_ERROR = "run_id must be a UUID4 value"
NAIVE_TIME_ERROR = "created_at must be timezone-aware"


def test_create_run_uses_opaque_id_and_explicit_time(tmp_path):
    paths = RepositoryPaths.from_data_root(
        tmp_path / "data",
        legacy_daily_show_root=tmp_path / "legacy",
    )
    run_id = uuid.UUID("4fa85f64-5717-4562-b3fc-2c963f66afa6")
    created_at = datetime(2026, 7, 29, 12, 30, tzinfo=UTC)

    manifest, run_dir = create_run(paths, created_at=created_at, run_id=run_id)

    serialized = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest == RunManifest(run_id=run_id, created_at=created_at)
    assert run_dir.name == str(run_id)
    assert serialized == {
        "schema_version": 1,
        "run_id": str(run_id),
        "created_at": created_at.isoformat(),
    }


def test_create_run_rejects_existing_run_id(tmp_path):
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    run_id = uuid.UUID("4fa85f64-5717-4562-b3fc-2c963f66afa6")
    create_run(paths, run_id=run_id)

    with pytest.raises(RunAlreadyExistsError):
        create_run(paths, run_id=run_id)


def test_run_manifest_rejects_non_uuid4():
    with pytest.raises(ValueError, match=NON_UUID4_ERROR):
        RunManifest(
            run_id=uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
            created_at=datetime.now(tz=UTC),
        )


def test_run_manifest_rejects_naive_time():
    with pytest.raises(ValueError, match=NAIVE_TIME_ERROR):
        RunManifest(run_id=uuid.uuid4(), created_at=datetime(2026, 7, 29, tzinfo=UTC).replace(tzinfo=None))


def test_run_manifest_rejects_unknown_schema_version():
    with pytest.raises(ValueError, match="schema_version must be 1"):
        RunManifest(
            run_id=uuid.uuid4(),
            created_at=datetime.now(tz=UTC),
            schema_version=2,
        )


def test_create_run_cleans_temporary_manifest_after_write_failure(
    tmp_path,
    monkeypatch,
):
    paths = RepositoryPaths.from_data_root(tmp_path / "data")

    def fail_replace(_self: Path, _target: Path) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(RunManifestWriteError):
        create_run(paths)

    assert not tuple(paths.runs_root.rglob("*.tmp"))


def test_ensure_writable_roots_does_not_touch_legacy(tmp_path):
    legacy_root = tmp_path / "legacy"
    paths = RepositoryPaths.from_data_root(
        tmp_path / "data",
        legacy_daily_show_root=legacy_root,
    )

    paths.ensure_writable_roots()

    assert (paths.assets_root.is_dir(), paths.runs_root.is_dir(), paths.reports_root.is_dir()) == (
        True,
        True,
        True,
    )
    assert not legacy_root.exists()
