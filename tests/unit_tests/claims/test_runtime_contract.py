from pytest import MonkeyPatch

from money_pit.claims import cli


def test__claim_repository_initializes_durable_store(
    tmp_path,
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "DATA_ROOT", tmp_path)

    repository = cli._claim_repository()

    assert repository.list_projections() == ()
