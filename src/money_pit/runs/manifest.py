"""Module registering recoverable filesystem and database run starts."""

from __future__ import annotations

import os
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol

from pydantic import ValidationError

from money_pit.constants import RUN_MANIFEST_FILENAME
from money_pit.runs.errors import RunAlreadyExistsError
from money_pit.runs.errors import RunManifestWriteError
from money_pit.runs.errors import RunReconciliationNotFoundError
from money_pit.runs.errors import RunRegistrationIncompleteError
from money_pit.schemas.runs import RunRecord
from money_pit.schemas.runs import validate_run_id
from money_pit.storage.runs import ImmutableRunCollisionError


if TYPE_CHECKING:
    from money_pit.runs.paths import RepositoryPaths


class RunStartStore(Protocol):
    """Database authority required for recoverable run registration."""

    def append_run(self, run: RunRecord) -> None:
        """Append or validate the exact immutable start record."""
        ...


class RunReconciliationStore(RunStartStore, Protocol):
    """Database authority that can discover an existing run start."""

    def find_run(self, run_id: str) -> RunRecord | None:
        """Return the registered start, if any."""
        ...


def register_run(paths: RepositoryPaths, store: RunStartStore, record: RunRecord) -> Path:
    """Register one run start in SQLite before atomically installing its manifest."""
    paths.runs_root.mkdir(parents=True, exist_ok=True)
    run_dir = paths.runs_root / record.run_id
    pending_dir = paths.runs_root / f".run-{record.run_id}.pending"
    if run_dir.exists():
        _require_exact_manifest(run_dir, record)
        _append_database_start(store, record)
        if pending_dir.exists():
            _require_exact_manifest(pending_dir, record)
            _remove_redundant_pending(pending_dir, record.run_id)
        return run_dir
    if pending_dir.exists():
        _require_exact_manifest(pending_dir, record)
    else:
        _create_pending_manifest(pending_dir, record)
    _append_database_start(store, record)
    try:
        _ = pending_dir.replace(run_dir)
    except OSError as error:
        raise RunRegistrationIncompleteError(record.run_id, "manifest_install") from error
    return run_dir


def reconcile_run(paths: RepositoryPaths, store: RunReconciliationStore, run_id: str) -> Path:
    """Discover and safely reconcile database, final, and pending run-start state."""
    _ = validate_run_id(run_id)
    final_dir = paths.runs_root / run_id
    pending_dir = paths.runs_root / f".run-{run_id}.pending"
    database_record = store.find_run(run_id)
    final_record = _record_if_present(final_dir, run_id)
    pending_record = _record_if_present(pending_dir, run_id)
    discovered = tuple(record for record in (database_record, final_record, pending_record) if record is not None)
    if not discovered:
        raise RunReconciliationNotFoundError(f"No durable state exists for run {run_id}.")
    expected = discovered[0]
    if any(record != expected for record in discovered[1:]):
        raise RunAlreadyExistsError(f"Run stores disagree for {run_id}.")
    return register_run(paths, store, expected)


def _create_pending_manifest(pending_dir: Path, record: RunRecord) -> None:
    try:
        pending_dir.mkdir(parents=False, exist_ok=False)
    except FileExistsError as error:
        raise RunAlreadyExistsError(f"Pending run directory already exists for {record.run_id}.") from error
    manifest_path = pending_dir / RUN_MANIFEST_FILENAME
    temporary_path: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(dir=pending_dir, prefix=".run.", suffix=".tmp")
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            _ = stream.write(record.canonical_json())
            stream.flush()
            os.fsync(stream.fileno())
        _ = temporary_path.replace(manifest_path)
        temporary_path = None
    except OSError as error:
        raise RunManifestWriteError(f"Could not write manifest for run {record.run_id}.") from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        if not manifest_path.exists():
            with suppress(OSError):
                pending_dir.rmdir()


def _append_database_start(store: RunStartStore, record: RunRecord) -> None:
    try:
        store.append_run(record)
    except ImmutableRunCollisionError:
        raise
    except Exception as error:
        raise RunRegistrationIncompleteError(record.run_id, "database_registration") from error


def _require_exact_manifest(directory: Path, expected: RunRecord) -> None:
    actual = _read_manifest(directory, expected.run_id)
    if actual != expected:
        raise RunAlreadyExistsError(f"Run manifest differs for {expected.run_id}.")


def _record_if_present(directory: Path, run_id: str) -> RunRecord | None:
    if not directory.exists():
        return None
    return _read_manifest(directory, run_id)


def _read_manifest(directory: Path, run_id: str) -> RunRecord:
    manifest_path = directory / RUN_MANIFEST_FILENAME
    try:
        actual = RunRecord.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, ValidationError) as error:
        raise RunAlreadyExistsError(f"Run manifest is invalid for {run_id}.") from error
    if actual.run_id != run_id:
        raise RunAlreadyExistsError(f"Run manifest identity differs for {run_id}.")
    return actual


def _remove_redundant_pending(pending_dir: Path, run_id: str) -> None:
    try:
        (pending_dir / RUN_MANIFEST_FILENAME).unlink()
        pending_dir.rmdir()
    except OSError as error:
        raise RunRegistrationIncompleteError(run_id, "pending_cleanup") from error
