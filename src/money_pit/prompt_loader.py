"""Module containing lazy, package-resource system-prompt loading used throughout the money_pit package."""

import functools
from importlib.resources import files


_PROMPTS_ANCHOR: str = "money_pit.prompts"
_PROMPT_SUFFIX: str = ".md"


@functools.cache
def system_prompt(name: str) -> str:
    """Return the named packaged system prompt, raising FileNotFoundError for an unknown name."""
    resource = files(_PROMPTS_ANCHOR) / f"{name}{_PROMPT_SUFFIX}"
    return resource.read_text(encoding="utf-8")
