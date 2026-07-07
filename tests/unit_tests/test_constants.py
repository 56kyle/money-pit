"""Tests for money_pit.constants lazy per-user directory accessors."""

from collections.abc import Callable
from pathlib import Path

from pytest import MonkeyPatch

from money_pit import constants

_FolderAccessor = Callable[[], Path]


def _fake_platformdir(base: Path) -> Callable[..., Path]:
    def _resolve(appname: str, appauthor: str, ensure_exists: bool = False) -> Path:
        target = base / appauthor / appname
        if ensure_exists:
            target.mkdir(parents=True, exist_ok=True)
        return target

    return _resolve


def _assert_creates_directory(
    accessor: _FolderAccessor, platformdir_name: str, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(constants, platformdir_name, _fake_platformdir(tmp_path))
    assert accessor().exists()


def _assert_location(
    accessor: _FolderAccessor, platformdir_name: str, tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    monkeypatch.setattr(constants, platformdir_name, _fake_platformdir(tmp_path))
    assert tmp_path in accessor().parents


def test_user_config_folder_creates_directory(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _assert_creates_directory(constants.user_config_folder, "user_config_path", tmp_path, monkeypatch)


def test_user_config_folder_location(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _assert_location(constants.user_config_folder, "user_config_path", tmp_path, monkeypatch)


def test_user_state_folder_creates_directory(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _assert_creates_directory(constants.user_state_folder, "user_state_path", tmp_path, monkeypatch)


def test_user_state_folder_location(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _assert_location(constants.user_state_folder, "user_state_path", tmp_path, monkeypatch)


def test_user_log_folder_creates_directory(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _assert_creates_directory(constants.user_log_folder, "user_log_path", tmp_path, monkeypatch)


def test_user_log_folder_location(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    _assert_location(constants.user_log_folder, "user_log_path", tmp_path, monkeypatch)


def test_default_config_path_creates_parent(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "user_config_path", _fake_platformdir(tmp_path))
    assert constants.default_config_path().parent.exists()


def test_default_config_path_location(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "user_config_path", _fake_platformdir(tmp_path))
    assert constants.default_config_path().name == constants._CONFIG_FILENAME
