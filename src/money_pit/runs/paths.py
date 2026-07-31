"""Module containing repository filesystem paths for the money_pit package."""

from dataclasses import dataclass
from pathlib import Path

from money_pit.constants import ASSETS_DIRNAME
from money_pit.constants import DAILY_SHOW_ROOT
from money_pit.constants import DATA_ROOT
from money_pit.constants import REPORTS_DIRNAME
from money_pit.constants import RUNS_DIRNAME
from money_pit.constants import STATE_DATABASE_FILENAME


@dataclass(frozen=True)
class RepositoryPaths:
    """All durable repository locations derived from one data root."""

    data_root: Path
    assets_root: Path
    runs_root: Path
    reports_root: Path
    database_path: Path
    legacy_daily_show_root: Path

    @classmethod
    def from_data_root(
        cls,
        data_root: Path = DATA_ROOT,
        *,
        legacy_daily_show_root: Path = DAILY_SHOW_ROOT,
    ) -> "RepositoryPaths":
        """Build repository paths without creating filesystem state."""
        return cls(
            data_root=data_root,
            assets_root=data_root / ASSETS_DIRNAME,
            runs_root=data_root / RUNS_DIRNAME,
            reports_root=data_root / REPORTS_DIRNAME,
            database_path=data_root / STATE_DATABASE_FILENAME,
            legacy_daily_show_root=legacy_daily_show_root,
        )

    def ensure_writable_roots(self) -> None:
        """Create the new-layout roots without touching the legacy tree."""
        self.assets_root.mkdir(parents=True, exist_ok=True)
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.reports_root.mkdir(parents=True, exist_ok=True)
