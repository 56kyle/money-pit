"""Opt-in live paper end-to-end for the plain-text thesis path: real A1 LLM + paper-account pipeline.

Marked `live` (deselected by default via addopts `-m 'not live'`) and additionally skipped unless
MONEY_PIT_LIVE=1, so a bare `-m live` run without an operator opt-in skips cleanly rather than moving capital.
Reuses the tier's live_config/live_credentials fixtures from this directory's conftest.py.
"""

import datetime
import os
import tempfile
from pathlib import Path

import pytest

import money_pit.__main__
from money_pit.adapters.text import TextAdapter
from money_pit.adapters.text_llm import TextPayload
from money_pit.adapters.text_llm import make_text_llm_agent
from money_pit.adapters.text_source import build_text_payload
from money_pit.config import AlpacaCredentials
from money_pit.config import Config
from money_pit.constants import source_id_to_dirname
from money_pit.graph.state import PipelineState
from money_pit.schemas.enums import TerminalState
from money_pit.schemas.signals import SignalSet


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("MONEY_PIT_LIVE") != "1",
        reason="live tier is opt-in; set MONEY_PIT_LIVE=1 (with real Alpaca paper / Gmail creds) to run",
    ),
]


def test_analyze_text_end_to_end_paper(
    live_credentials: AlpacaCredentials, live_config: Config, tmp_path: Path
) -> None:
    """Run a minimal thesis through the real A1 LLM and the paper-account pipeline to a terminal state.

    Real LLM + paper-account run; the thesis is kept deliberately tiny. Refuses to run unless credentials
    are paper-routed (mirrors the order-fill guard in test_paper_trade_live.py).
    """
    assert live_credentials.paper, "refusing to run a live thesis: credentials are not paper-routed"

    retrieved_at: str = datetime.datetime.now(tz=datetime.timezone.utc).isoformat()
    payload: TextPayload = build_text_payload(
        "NVDA remains well-positioned for continued AI infrastructure demand.",
        slug="2026-01-01_00-00-00",
        title="live-tier text smoke",
        retrieved_at=retrieved_at,
    )

    agent = make_text_llm_agent(live_config)
    signal_set: SignalSet = TextAdapter(agent=agent, cache_dir=tmp_path).process(payload)

    signals_dir: Path = Path(tempfile.mkdtemp())
    signal_path: Path = signals_dir / f"{source_id_to_dirname(signal_set.source_ref.source_id)}.json"
    _ = signal_path.write_text(signal_set.model_dump_json(indent=2), encoding="utf-8")

    state: PipelineState = money_pit.__main__._run_signals_dir(signals_dir, live_config)

    assert isinstance(state.get("terminal_state"), TerminalState)
