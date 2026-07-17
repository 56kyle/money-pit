"""Module containing the TextAdapter that persists a pre-built TextPayload before LLM classification and post-processes SignalSetDraft into SignalSet for the money_pit package."""

from collections.abc import Callable
from pathlib import Path

from typing_extensions import override

from money_pit.adapters.assembly import signal_set_from_draft
from money_pit.adapters.base import SourceAdapter
from money_pit.adapters.text_llm import TextPayload
from money_pit.constants import source_id_to_dirname
from money_pit.schemas.signal_draft import SignalSetDraft
from money_pit.schemas.signals import SignalSet


_PAYLOAD_FILENAME: str = "text_payload.json"


class TextAdapter(SourceAdapter[TextPayload]):
    """Concrete SourceAdapter for plain-text thesis sources."""

    _agent: Callable[[TextPayload], SignalSetDraft]
    _cache_dir: Path

    def __init__(
        self,
        agent: Callable[[TextPayload], SignalSetDraft],
        cache_dir: Path,
    ) -> None:
        """Store the LLM agent callable and the payload cache directory."""
        self._agent = agent
        self._cache_dir = cache_dir

    @override
    def process(self, payload: TextPayload) -> SignalSet:
        """Persist the payload, call the LLM agent, and post-process the draft into a SignalSet."""
        source_id: str = payload.source_ref.source_id
        payload_dir: Path = self._cache_dir / source_id_to_dirname(source_id)
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path: Path = payload_dir / _PAYLOAD_FILENAME
        _ = payload_path.write_text(payload.model_dump_json(indent=2), encoding="utf-8")

        return signal_set_from_draft(
            self._agent(payload),
            slug=payload.slug,
            source_ref=payload.source_ref,
        )
