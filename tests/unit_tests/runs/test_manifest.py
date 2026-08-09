import uuid
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest
from pydantic import ValidationError
from pytest import MonkeyPatch

from money_pit.runs.errors import RunAlreadyExistsError
from money_pit.runs.errors import RunRegistrationIncompleteError
from money_pit.runs.manifest import reconcile_run
from money_pit.runs.manifest import register_run
from money_pit.runs.paths import RepositoryPaths
from money_pit.runs.repository import RunNotFoundError
from money_pit.runs.repository import RunRepository as FilesystemRunRepository
from money_pit.schemas.runs import RunRecord
from money_pit.storage.database import Database
from money_pit.storage.runs import RunRepository


def _record(run_id: str | None = None) -> RunRecord:
    started_at = datetime(2026, 7, 29, 12, 30, tzinfo=UTC)
    return RunRecord(
        run_id=run_id or str(uuid.UUID("4fa85f64-5717-4562-b3fc-2c963f66afa6")),
        requested_as_of=started_at,
        started_at=started_at,
        known_at=started_at,
        through_stage="A4",
        source_config_hash="a" * 64,
        intelligence_config_hash="b" * 64,
    )


def _repository(paths: RepositoryPaths) -> RunRepository:
    database = Database(paths.database_path)
    database.initialize()
    return RunRepository(database)


class _InspectingStore:
    def __init__(self, paths: RepositoryPaths, repository: RunRepository) -> None:
        self._paths: RepositoryPaths = paths
        self._repository: RunRepository = repository

    def append_run(self, run: RunRecord) -> None:
        pending = self._paths.runs_root / f".run-{run.run_id}.pending" / "run.json"
        final = self._paths.runs_root / run.run_id
        assert pending.is_file()
        assert not final.exists()
        self._repository.append_run(run)


class _FailingStore:
    def append_run(self, run: RunRecord) -> None:
        del run
        raise OSError("injected database boundary failure")


def test_register_run_registers_database_before_atomic_manifest_install(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    repository = _repository(paths)
    record = _record()

    run_dir = register_run(paths, _InspectingStore(paths, repository), record)

    assert repository.get_run(record.run_id) == record
    assert RunRecord.model_validate_json((run_dir / "run.json").read_text(encoding="utf-8")) == record


def test_register_run_recovers_pending_manifest_after_database_failure(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    record = _record()

    with pytest.raises(RunRegistrationIncompleteError) as failure:
        _ = register_run(paths, _FailingStore(), record)

    pending = paths.runs_root / f".run-{record.run_id}.pending" / "run.json"
    assert failure.value.phase == "database_registration"
    assert RunRecord.model_validate_json(pending.read_text(encoding="utf-8")) == record

    repository = _repository(paths)
    run_dir = reconcile_run(paths, repository, record.run_id)

    assert run_dir.is_dir()
    assert not pending.parent.exists()


def test_register_run_recovers_after_atomic_install_failure(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    repository = _repository(paths)
    record = _record()
    pending_dir = paths.runs_root / f".run-{record.run_id}.pending"
    final_dir = paths.runs_root / record.run_id
    original_replace = Path.replace

    def fail_final_install(path: Path, target: Path) -> Path:
        if path == pending_dir and target == final_dir:
            raise OSError("injected atomic install failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_final_install)
    with pytest.raises(RunRegistrationIncompleteError) as failure:
        _ = register_run(paths, repository, record)

    assert failure.value.phase == "manifest_install"
    assert repository.get_run(record.run_id) == record
    assert pending_dir.is_dir()

    monkeypatch.undo()
    assert reconcile_run(paths, repository, record.run_id) == final_dir
    assert not pending_dir.exists()


def test_register_run_reconstructs_manifest_from_database_only(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    repository = _repository(paths)
    record = _record()
    repository.append_run(record)

    run_dir = reconcile_run(paths, repository, record.run_id)

    assert RunRecord.model_validate_json((run_dir / "run.json").read_text(encoding="utf-8")) == record


def test_register_run_registers_exact_final_manifest_only(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    repository = _repository(paths)
    record = _record()
    run_dir = paths.runs_root / record.run_id
    run_dir.mkdir(parents=True)
    _ = (run_dir / "run.json").write_text(record.canonical_json(), encoding="utf-8")

    assert reconcile_run(paths, repository, record.run_id) == run_dir
    assert repository.get_run(record.run_id) == record


def test_register_run_rejects_a_different_existing_manifest(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")
    repository = _repository(paths)
    record = _record()
    run_dir = paths.runs_root / record.run_id
    run_dir.mkdir(parents=True)
    different = record.model_copy(update={"through_stage": "A5"})
    _ = (run_dir / "run.json").write_text(different.canonical_json(), encoding="utf-8")

    with pytest.raises(RunAlreadyExistsError):
        _ = register_run(paths, repository, record)


@pytest.mark.parametrize(
    "run_id",
    [
        pytest.param("not-a-uuid", id="invalid"),
        pytest.param("6ba7b810-9dad-11d1-80b4-00c04fd430c8", id="non-v4"),
    ],
)
def test_run_record_rejects_non_uuid4_identity(run_id: str) -> None:
    with pytest.raises(ValidationError):
        _ = _record(run_id)


def test_filesystem_repository_rejects_non_uuid_before_path_resolution(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")

    with pytest.raises(RunNotFoundError):
        _ = FilesystemRunRepository(paths).resolve("../outside")


def test_ensure_writable_roots_creates_only_the_current_runtime_layout(tmp_path: Path) -> None:
    paths = RepositoryPaths.from_data_root(tmp_path / "data")

    paths.ensure_writable_roots()

    assert (paths.assets_root.is_dir(), paths.runs_root.is_dir(), paths.reports_root.is_dir()) == (
        True,
        True,
        True,
    )
