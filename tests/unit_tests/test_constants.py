from money_pit import constants


def test_release_identity_is_0_0_3() -> None:
    assert constants.APP_VERSION == "0.0.6"


def test_runtime_layout_names_only_current_state() -> None:
    assert (
        constants.ASSETS_DIRNAME,
        constants.RUNS_DIRNAME,
        constants.REPORTS_DIRNAME,
        constants.STATE_DATABASE_FILENAME,
    ) == ("assets", "runs", "reports", "intelligence.sqlite3")
