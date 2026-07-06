"""Scheduler trigger, working-dir creation, slug assignment."""

import shutil
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

from money_pit.agents.answer_synthesis import make_answer_synthesis_agent
from money_pit.agents.claim_questions import make_claim_questions_agent
from money_pit.agents.corroboration import corroborate
from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.agents.research_tools import OpenEndedResearchTools
from money_pit.agents.thesis_judgment import make_thesis_judgment_agent
from money_pit.alpaca_portfolio import make_alpaca_portfolio_fetcher
from money_pit.config import DEFAULT_OWNER_RECIPIENT
from money_pit.config import Config
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.constants import DAILY_SHOW_ROOT
from money_pit.email_sender import make_gmail_email_sender
from money_pit.mcp.clients import make_alpaca_write_deps
from money_pit.mcp.manifest import live_manifest
from money_pit.contracts import AnswerSynthesisAgent
from money_pit.contracts import ClaimQuestionsAgent
from money_pit.contracts import CorroborationAgent
from money_pit.contracts import EmailSender
from money_pit.contracts import OrderPlacer
from money_pit.contracts import PortfolioFetcher
from money_pit.contracts import ThesisAgent
from money_pit.contracts import ToolManifest
from money_pit.graph.graph import build_graph
from money_pit.graph.state import PipelineState
from money_pit.schemas.action_steps import ExecutionParameters
from money_pit.schemas.aggregation_draft import ClaimRelations
from money_pit.schemas.analysis_draft import AnalysisJudgment
from money_pit.schemas.analysis_draft import Scenario
from money_pit.schemas.analysis_draft import ScenarioTable
from money_pit.schemas.analysis_draft import ThesisJudgment
from money_pit.schemas.answer_draft import AnswerDraft
from money_pit.schemas.answers import InitialAnswers
from money_pit.schemas.enums import ActionType
from money_pit.schemas.enums import ConvictionLevel
from money_pit.schemas.enums import SignalTier
from money_pit.schemas.enums import Step1Disposition
from money_pit.schemas.portfolio import PortfolioSnapshot
from money_pit.schemas.provenance import SourceRef
from money_pit.schemas.question_draft import DraftQuestion
from money_pit.schemas.questions import Question
from money_pit.schemas.signals import AggregatedSignals
from money_pit.schemas.signals import Claim


_MISSING_DEP_MESSAGE: str = (
    "run_pipeline requires a real {name}; supply it via PipelineOverrides or use phase4_overrides() for tests."
)

_EDGAR_IDENTITY_FALLBACK: str = f"money-pit research {DEFAULT_OWNER_RECIPIENT}"


class MissingPipelineDependencyError(Exception):
    """Raised when a capital-critical pipeline dependency is not supplied."""


@dataclass
class PipelineOverrides:
    """Injectable agent overrides for testing. All fields default to None (use Phase 5 implementations)."""

    thesis_agent: ThesisAgent | None = field(default=None)
    claim_questions_agent: ClaimQuestionsAgent | None = field(default=None)
    answer_synthesis_agent: AnswerSynthesisAgent | None = field(default=None)
    corroboration_agent: CorroborationAgent | None = field(default=None)
    fetch_portfolio: PortfolioFetcher | None = field(default=None)
    deterministic_tools: DeterministicResearchTools | None = field(default=None)
    open_ended_tools: OpenEndedResearchTools | None = field(default=None)
    place_order: OrderPlacer | None = field(default=None)
    send_email: EmailSender | None = field(default=None)
    manifest: ToolManifest | None = field(default=None)
    order_schema_path: Path | None = field(default=None)
    """Sentinel-free order schema for the analysis node's execution-param emission; None uses the default committed path (which fails closed while the stub is unpinned)."""


class _DirectDeterministicTools:
    _fred_api_key: str | None

    def __init__(self, fred_api_key: str | None) -> None:
        self._fred_api_key = fred_api_key

    def fetch_fred_series(self, series_id: str) -> float | None:
        if self._fred_api_key is None:
            return None
        try:
            import requests

            response = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={
                    "series_id": series_id,
                    "api_key": self._fred_api_key,
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": "1",
                },
                timeout=10,
            )
            data: dict[str, object] = response.json()
            observations = data.get("observations")
            if not isinstance(observations, list) or not observations:
                return None
            first = observations[0]
            if not isinstance(first, dict):
                return None
            value_str = first.get("value")
            if not isinstance(value_str, str) or value_str == ".":
                return None
            return float(value_str)
        except Exception:
            return None

    def fetch_ticker_price(self, ticker: str) -> float | None:
        try:
            import yfinance as yf  # pyright: ignore[reportMissingTypeStubs]

            hist = yf.Ticker(ticker).history(period="1d")  # pyright: ignore[reportUnknownMemberType]
            if hist.empty:
                return None
            close_series = hist["Close"]
            last = close_series.iloc[-1]  # pyright: ignore[reportAny]
            return float(last)  # pyright: ignore[reportAny]
        except Exception:
            return None


class _DirectOpenEndedTools:
    _brave_api_key: str | None
    _owner_recipient: str

    def __init__(self, brave_api_key: str | None, owner_recipient: str) -> None:
        self._brave_api_key = brave_api_key
        self._owner_recipient = owner_recipient

    def brave_search(self, query: str, *, n_results: int = 5) -> list[str]:
        if self._brave_api_key is None:
            return []
        try:
            import requests

            resp = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._brave_api_key,
                },
                params={"q": query, "count": str(n_results)},
                timeout=10,
            )
            data: dict[str, object] = resp.json()  # pyright: ignore[reportAny]
            web = data.get("web")
            if not isinstance(web, dict):
                return []
            results = web.get("results")
            if not isinstance(results, list):
                return []
            snippets: list[str] = []
            for item in results[:n_results]:
                if isinstance(item, dict):
                    title = item.get("title", "")
                    desc = item.get("description", "")
                    if isinstance(title, str) and isinstance(desc, str):
                        snippets.append(f"{title}: {desc}")
            return snippets
        except Exception:
            return []

    def edgar_search(self, query: str, *, n_results: int = 5) -> list[str]:  # pragma: no cover
        try:
            from edgar import search_filings
            from edgar import set_identity

            set_identity(self._owner_recipient or _EDGAR_IDENTITY_FALLBACK)
            snippets: list[str] = []
            for result in search_filings(query, limit=n_results):
                snippets.append(f"{result.file_type} — {result.company} — {result.period}")
                if len(snippets) >= n_results:
                    break
            return snippets
        except Exception:
            return []


class _Phase4DeterministicTools:
    def fetch_fred_series(self, series_id: str) -> float | None:
        _ = series_id
        return None

    def fetch_ticker_price(self, ticker: str) -> float | None:
        _ = ticker
        return None


def _phase4_fetch_portfolio(slug: str) -> PortfolioSnapshot:
    """Return a minimal portfolio snapshot for Phase 4 testing."""
    return PortfolioSnapshot(
        slug=slug,
        as_of=slug,
        total_account_value=100_000.0,
        available_cash=20_000.0,
        positions=[],
        sector_weights={},
        correlated_overlaps=[],
    )


def _phase4_corroborate(_claims: list[Claim]) -> ClaimRelations:
    """Return empty agree/disagree relations — no LLM corroboration in Phase 4."""
    return ClaimRelations(agree=[], disagree=[])


def _phase4_claim_questions(_claims: list[Claim]) -> list[DraftQuestion]:
    """Return no LLM-generated draft questions in Phase 4."""
    return []


def _phase4_answer_synthesis(
    questions: list[Question],
    _sources: list[SourceRef],
) -> list[AnswerDraft]:
    """Return one stub draft per question."""
    return [
        AnswerDraft(
            question_id=q.id,
            answer="Stub answer — Phase 4 testing only.",
            sources_used=[],
            data_retrieved=None,
            limitations="Phase 4 stub; no real retrieval performed.",
        )
        for q in questions
    ]


def _phase4_thesis_agent(
    aggregated_signals: AggregatedSignals,
    _portfolio_snapshot: PortfolioSnapshot,
    _initial_answers: InitialAnswers,
) -> AnalysisJudgment:
    """Return a container with one SUPPORTED stub thesis for the first eligible claim, else empty."""
    qualifying_claim: Claim | None = next(
        (c for c in aggregated_signals.claims if c.tier in (SignalTier.HIGH, SignalTier.MEDIUM) and c.tickers_affected),
        None,
    )
    if qualifying_claim is None:
        return AnalysisJudgment(theses=[], dropped_claims=[], macro_read=[], halt=None)
    thesis = ThesisJudgment(
        claim_id=qualifying_claim.claim_id,
        instrument=qualifying_claim.tickers_affected[0],
        action_type=ActionType.BUY,
        description=f"Phase 4 stub: {qualifying_claim.claim[:100]}",
        group_id=None,
        one_sentence_thesis="Stub thesis — Phase 4 integration test.",
        expected_value=0.08,
        conviction=ConvictionLevel.MEDIUM,
        scenario_table=ScenarioTable(
            bull=Scenario.model_validate(
                {
                    "probability": 30,
                    "return": 0.20,
                    "timeframe": None,
                    "confirming_metric": None,
                    "mechanism": None,
                    "max_drawdown": None,
                }
            ),
            base=Scenario.model_validate(
                {
                    "probability": 50,
                    "return": 0.08,
                    "timeframe": None,
                    "confirming_metric": None,
                    "mechanism": None,
                    "max_drawdown": None,
                }
            ),
            bear=Scenario.model_validate(
                {
                    "probability": 20,
                    "return": -0.10,
                    "timeframe": None,
                    "confirming_metric": None,
                    "mechanism": None,
                    "max_drawdown": None,
                }
            ),
        ),
        invalidation_conditions=[],
        sizing_rationale="Phase 4 stub sizing.",
        disposition=Step1Disposition.SUPPORTED,
    )
    return AnalysisJudgment(theses=[thesis], dropped_claims=[], macro_read=[], halt=None)


def _phase4_place_order(params: ExecutionParameters) -> str:
    """Return a paper trade order ID — no real order placed in Phase 4."""
    return f"PAPER-{params.client_order_id}"


def _phase4_send_email(_subject: str, _body: str) -> None:
    """No-op email stub — real sender wired in Phase 7."""


def phase4_overrides() -> PipelineOverrides:
    """Return a PipelineOverrides instance using all Phase 4 stubs (for integration tests)."""
    return PipelineOverrides(
        thesis_agent=_phase4_thesis_agent,
        claim_questions_agent=_phase4_claim_questions,
        answer_synthesis_agent=_phase4_answer_synthesis,
        corroboration_agent=_phase4_corroborate,
        fetch_portfolio=_phase4_fetch_portfolio,
        deterministic_tools=_Phase4DeterministicTools(),
        place_order=_phase4_place_order,
        send_email=_phase4_send_email,
    )


def production_deps(config: Config) -> PipelineOverrides:
    """Return production PipelineOverrides wiring the real capital-critical deps and live tool manifest.

    Alpaca credentials are resolved eagerly here so composition fails closed before any run begins.
    order_schema_path is left as the default committed path, which is valid only after the operator
    has run the pin-order-schema command.
    """
    credentials = resolve_alpaca_credentials(config)
    return PipelineOverrides(
        fetch_portfolio=make_alpaca_portfolio_fetcher(credentials),
        place_order=make_alpaca_write_deps(credentials),
        send_email=make_gmail_email_sender(config),
        manifest=live_manifest(credentials),
    )


@dataclass(frozen=True)
class _CapitalCriticalDeps:
    """The three dependencies that move real capital, resolved and guaranteed non-None."""

    fetch_portfolio: PortfolioFetcher
    place_order: OrderPlacer
    send_email: EmailSender


def _require_capital_critical_deps(overrides: PipelineOverrides) -> _CapitalCriticalDeps:
    """Return the capital-critical dependencies, raising MissingPipelineDependencyError if any is None."""
    fetch_portfolio = overrides.fetch_portfolio
    place_order = overrides.place_order
    send_email = overrides.send_email
    if fetch_portfolio is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="fetch_portfolio"))
    if place_order is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="place_order"))
    if send_email is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="send_email"))
    return _CapitalCriticalDeps(
        fetch_portfolio=fetch_portfolio,
        place_order=place_order,
        send_email=send_email,
    )


def run_pipeline(
    signals_dir: Path,
    *,
    run_dir: Path | None = None,
    overrides: PipelineOverrides | None = None,
) -> PipelineState:
    """Execute the full pipeline on the signals in signals_dir and return the final state."""
    ov: PipelineOverrides = overrides or PipelineOverrides()
    capital_deps: _CapitalCriticalDeps = _require_capital_critical_deps(ov)

    slug: str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    working_dir: Path = run_dir if run_dir is not None else DAILY_SHOW_ROOT / slug
    working_dir.mkdir(parents=True, exist_ok=True)

    signals_out: Path = working_dir / "signals"
    signals_out.mkdir(exist_ok=True)

    for signal_file in signals_dir.glob("*.json"):
        _ = shutil.copy2(signal_file, signals_out / signal_file.name)

    config = load_config()

    det_tools: DeterministicResearchTools = ov.deterministic_tools or _DirectDeterministicTools(config.fred_api_key)
    open_tools: OpenEndedResearchTools = ov.open_ended_tools or _DirectOpenEndedTools(
        config.brave_api_key, config.owner_recipient
    )

    thesis = ov.thesis_agent or make_thesis_judgment_agent(config)
    claim_qs = ov.claim_questions_agent or make_claim_questions_agent(config)
    answer_synth = ov.answer_synthesis_agent or make_answer_synthesis_agent(open_tools, config)
    corr = ov.corroboration_agent or corroborate

    graph = build_graph(
        fetch_portfolio=capital_deps.fetch_portfolio,
        corroboration_agent=corr,
        claim_questions_agent=claim_qs,
        answer_synthesis_agent=answer_synth,
        deterministic_tools=det_tools,
        config=config,
        thesis_agent=thesis,
        place_order=capital_deps.place_order,
        send_email=capital_deps.send_email,
        manifest=ov.manifest,
        order_schema_path=ov.order_schema_path,
    )

    initial_state: PipelineState = {
        "slug": slug,
        "working_dir": str(working_dir),
        "completed_steps": [],
    }

    result = graph.invoke(initial_state)
    return result  # pyright: ignore[reportReturnType]
