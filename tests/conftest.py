"""Fixtures used in all tests."""
import importlib.util

pytest_plugins: list[str] = (
    ["pytest-repo-structure"]
    if importlib.util.find_spec("pytest_repo_structure") is not None
    else []
)

