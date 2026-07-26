"""Module containing scheduler-trigger, working-dir creation, and slug-assignment logic for the money_pit package."""

import shutil
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timezone
from pathlib import Path

import requests
from loguru import logger
from pydantic import SecretStr

from money_pit.agents.answer_synthesis import make_answer_synthesis_agent
from money_pit.agents.claim_questions import make_claim_questions_agent
from money_pit.agents.corroboration import corroborate
from money_pit.agents.research_tools import DeterministicResearchTools
from money_pit.agents.research_tools import OpenEndedResearchTools
from money_pit.agents.thesis_judgment import make_thesis_judgment_agent
from money_pit.compute.fills import build_fill_observation
from money_pit.alpaca_orders import make_alpaca_fill_observer
from money_pit.alpaca_portfolio import make_alpaca_portfolio_fetcher
from money_pit.config import DEFAULT_OWNER_RECIPIENT
from money_pit.config import Config
from money_pit.config import CredentialResolutionError
from money_pit.config import load_config
from money_pit.config import resolve_alpaca_credentials
from money_pit.constants import DAILY_SHOW_ROOT
from money_pit.constants import SIGNALS_DIRNAME
from money_pit.contracts import AnswerSynthesisAgent
from money_pit.contracts import ClaimQuestionsAgent
from money_pit.contracts import CorroborationAgent
from money_pit.contracts import EmailSender
from money_pit.contracts import FillObserver
from money_pit.contracts import OrderPlacer
from money_pit.contracts import PortfolioFetcher
from money_pit.contracts import ResolveInstrumentFacts
from money_pit.contracts import ThesisAgent
from money_pit.contracts import ToolManifest
from money_pit.email_sender import make_gmail_email_sender
from money_pit.email_sender import make_unconfigured_email_sender
from money_pit.email_sender import with_undelivered_record
from money_pit.graph.graph import build_graph
from money_pit.graph.graph import thread_config
from money_pit.graph.state import PipelineState
from money_pit.market_data import make_yfinance_instrument_resolver
from money_pit.mcp.clients import make_alpaca_write_deps
from money_pit.mcp.manifest import live_manifest
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
from money_pit.schemas.fetch_result import FetchError
from money_pit.schemas.fetch_result import FetchResult
from money_pit.schemas.fetch_result import FetchValue
from money_pit.schemas.fetch_result import NoData
from money_pit.schemas.fills import FillObservation
from money_pit.schemas.instrument import InstrumentFacts
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

_FRED_OBSERVATIONS_URL: str = "https://api.stlouisfed.org/fred/series/observations"
_BRAVE_SEARCH_URL: str = "https://api.search.brave.com/res/v1/web/search"
_FETCH_TIMEOUT_SECONDS: int = 10
_FRED_MISSING_VALUE: str = "."
_TICKER_HISTORY_PERIOD: str = "1d"


class MissingPipelineDependencyError(Exception):
    """Raised when a capital-critical pipeline dependency is not supplied."""


@dataclass
class PipelineOverrides:
    """Injectable agent overrides for testing. All fields default to None (use Phase 5 implementations)."""

    thesis_agent: ThesisAgent | None = field(default=None)
    resolve_instrument_facts: ResolveInstrumentFacts | None = field(default=None)
    claim_questions_agent: ClaimQuestionsAgent | None = field(default=None)
    answer_synthesis_agent: AnswerSynthesisAgent | None = field(default=None)
    corroboration_agent: CorroborationAgent | None = field(default=None)
    fetch_portfolio: PortfolioFetcher | None = field(default=None)
    deterministic_tools: DeterministicResearchTools | None = field(default=None)
    open_ended_tools: OpenEndedResearchTools | None = field(default=None)
    place_order: OrderPlacer | None = field(default=None)
    observe_fill: FillObserver | None = field(default=None)
    send_email: EmailSender | None = field(default=None)
    manifest: ToolManifest | None = field(default=None)


class _DirectDeterministicTools:
    """Direct-fetch tools whose FetchError.reason never carries upstream exception text (see ADR 0032)."""

    _fred_api_key: SecretStr | None

    def __init__(self, fred_api_key: SecretStr | None) -> None:
        self._fred_api_key = fred_api_key

    def fetch_fred_series(self, series_id: str) -> FetchResult:
        """Return the latest FRED observation, NoData on an empty series, FetchError on an upstream or config failure."""
        fred_api_key: SecretStr | None = self._fred_api_key
        if fred_api_key is None:
            logger.warning("FRED API key not configured; cannot fetch series {series_id}", series_id=series_id)
            return FetchError(reason=f"FRED API key not configured; cannot fetch series {series_id}.")
        try:
            response = requests.get(
                _FRED_OBSERVATIONS_URL,
                params={
                    "series_id": series_id,
                    "api_key": fred_api_key.get_secret_value(),
                    "file_type": "json",
                    "sort_order": "desc",
                    "limit": "1",
                },
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            data: object = response.json()
        except (requests.RequestException, ValueError) as error:
            logger.warning("FRED fetch failed for series {series_id}: {error}", series_id=series_id, error=error)
            return FetchError(reason=f"FRED fetch failed for series {series_id}: {type(error).__name__}")
        if not isinstance(data, dict):
            return NoData()
        observations = data.get("observations")
        if not isinstance(observations, list) or not observations:
            return NoData()
        first = observations[0]
        if not isinstance(first, dict):
            return NoData()
        value_str = first.get("value")
        if not isinstance(value_str, str) or value_str == _FRED_MISSING_VALUE:
            return NoData()
        try:
            return FetchValue(value=float(value_str))
        except ValueError as error:
            logger.warning("FRED value unparseable for series {series_id}: {error}", series_id=series_id, error=error)
            return FetchError(reason=f"FRED value unparseable for series {series_id}: {value_str!r}")

    def fetch_ticker_price(self, ticker: str) -> FetchResult:
        """Return the latest close, NoData when the ticker has no recent bar, FetchError on a yfinance network failure."""
        import yfinance as yf  # pyright: ignore[reportMissingTypeStubs]

        try:
            hist = yf.Ticker(ticker).history(period=_TICKER_HISTORY_PERIOD)  # pyright: ignore[reportUnknownMemberType]
        except (requests.RequestException, OSError) as error:
            logger.warning("yfinance fetch failed for ticker {ticker}: {error}", ticker=ticker, error=error)
            return FetchError(reason=f"yfinance fetch failed for ticker {ticker}: {type(error).__name__}")
        if hist.empty:
            return NoData()
        close_series = hist["Close"]
        last = close_series.iloc[-1]  # pyright: ignore[reportAny]
        return FetchValue(value=float(last))  # pyright: ignore[reportAny]


class _DirectOpenEndedTools:
    _brave_api_key: SecretStr | None
    _owner_recipient: str

    def __init__(self, brave_api_key: SecretStr | None, owner_recipient: str) -> None:
        self._brave_api_key = brave_api_key
        self._owner_recipient = owner_recipient

    def brave_search(self, query: str, *, n_results: int = 5) -> list[str]:
        if self._brave_api_key is None:
            return []
        try:
            resp = requests.get(
                _BRAVE_SEARCH_URL,
                headers={
                    "Accept": "application/json",
                    "X-Subscription-Token": self._brave_api_key.get_secret_value(),
                },
                params={"q": query, "count": str(n_results)},
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
            data: object = resp.json()  # pyright: ignore[reportAny]
        except (requests.RequestException, ValueError) as error:
            logger.warning("Brave search failed for query {query!r}: {error}", query=query, error=error)
            return []
        if not isinstance(data, dict):
            return []
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

    def edgar_search(self, query: str, *, n_results: int = 5) -> list[str]:  # pragma: no cover
        from edgar import search_filings
        from edgar import set_identity

        try:
            set_identity(self._owner_recipient or _EDGAR_IDENTITY_FALLBACK)
            snippets: list[str] = []
            for result in search_filings(query, limit=n_results):
                snippets.append(f"{result.file_type} — {result.company} — {result.period}")
                if len(snippets) >= n_results:
                    break
            return snippets
        except (requests.RequestException, OSError) as error:
            logger.warning("EDGAR search failed for query {query!r}: {error}", query=query, error=error)
            return []


class _Phase4DeterministicTools:
    def fetch_fred_series(self, series_id: str) -> FetchResult:
        _ = series_id
        return NoData()

    def fetch_ticker_price(self, ticker: str) -> FetchResult:
        _ = ticker
        return NoData()


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


def _phase4_resolve_instrument_facts(_instrument: str) -> InstrumentFacts:
    """Return benign instrument facts so Phase 4 integration runs without network access."""
    return InstrumentFacts(sector="unknown", is_etf=False, holdings=[])


def _phase4_place_order(params: ExecutionParameters) -> str:
    """Return a paper trade order ID — no real order placed in Phase 4."""
    return f"PAPER-{params.client_order_id}"


def _phase4_observe_fill(client_order_id: str) -> FillObservation:
    """Return a synthetic FILLED observation so the deterministic-spine execute path reaches EXECUTED_CLEAN."""
    _ = client_order_id
    return build_fill_observation("filled", 1.0, 1.0)


def _phase4_send_email(_subject: str, _body: str) -> None:
    """Discard the notification so Phase 4 integration runs without sending mail."""


def phase4_overrides() -> PipelineOverrides:
    """Return a PipelineOverrides instance using all Phase 4 stubs (for integration tests)."""
    return PipelineOverrides(
        thesis_agent=_phase4_thesis_agent,
        resolve_instrument_facts=_phase4_resolve_instrument_facts,
        claim_questions_agent=_phase4_claim_questions,
        answer_synthesis_agent=_phase4_answer_synthesis,
        corroboration_agent=_phase4_corroborate,
        fetch_portfolio=_phase4_fetch_portfolio,
        deterministic_tools=_Phase4DeterministicTools(),
        place_order=_phase4_place_order,
        observe_fill=_phase4_observe_fill,
        send_email=_phase4_send_email,
    )


def _gmail_or_unconfigured_email_sender(config: Config) -> EmailSender:
    """Return the Gmail sender, or a sender that fails every send when Gmail credentials cannot be resolved."""
    try:
        return make_gmail_email_sender(config)
    except CredentialResolutionError as error:
        logger.warning("Gmail is not configured; owner emails will be recorded instead of sent: {error}", error=error)
        return make_unconfigured_email_sender(str(error))


def production_deps(config: Config) -> PipelineOverrides:
    """Return production PipelineOverrides wiring the real capital-critical deps and live tool manifest.

    Alpaca credentials are resolved eagerly here so composition fails closed before any run begins.
    """
    credentials = resolve_alpaca_credentials(config)
    return PipelineOverrides(
        fetch_portfolio=portfolio_fetcher_or_default(PipelineOverrides(), config),
        place_order=make_alpaca_write_deps(credentials),
        observe_fill=make_alpaca_fill_observer(credentials),
        send_email=_gmail_or_unconfigured_email_sender(config),
        manifest=live_manifest(credentials),
    )


def portfolio_fetcher_or_default(overrides: PipelineOverrides, config: Config) -> PortfolioFetcher:
    """Return the overridden portfolio fetcher, else the read-only Alpaca fetcher, raising CredentialResolutionError when no override is supplied and the Alpaca secret cannot be resolved."""
    if overrides.fetch_portfolio is not None:
        return overrides.fetch_portfolio
    return make_alpaca_portfolio_fetcher(resolve_alpaca_credentials(config), config)


def deterministic_research_tools_or_default(
    overrides: PipelineOverrides, config: Config
) -> DeterministicResearchTools:
    """Return the overridden deterministic tools, else direct FRED and yfinance fetches keyed from config."""
    if overrides.deterministic_tools is not None:
        return overrides.deterministic_tools
    return _DirectDeterministicTools(config.fred_api_key)


def open_ended_research_tools_or_default(overrides: PipelineOverrides, config: Config) -> OpenEndedResearchTools:
    """Return the overridden open-ended tools, else direct Brave and EDGAR searches keyed from config."""
    if overrides.open_ended_tools is not None:
        return overrides.open_ended_tools
    return _DirectOpenEndedTools(config.brave_api_key, config.owner_recipient)


def thesis_agent_or_default(overrides: PipelineOverrides, config: Config) -> ThesisAgent:
    """Return the overridden thesis agent, else the LLM thesis-judgment agent built from config, raising openai.OpenAIError when no override is supplied and OPENAI_API_KEY is absent from the environment."""
    if overrides.thesis_agent is not None:
        return overrides.thesis_agent
    return make_thesis_judgment_agent(config)


def instrument_facts_resolver_or_default(overrides: PipelineOverrides) -> ResolveInstrumentFacts:
    """Return the overridden instrument-facts resolver, else the yfinance resolver."""
    if overrides.resolve_instrument_facts is not None:
        return overrides.resolve_instrument_facts
    return make_yfinance_instrument_resolver()


def claim_questions_agent_or_default(overrides: PipelineOverrides, config: Config) -> ClaimQuestionsAgent:
    """Return the overridden claim-questions agent, else the LLM claim-questions agent built from config, raising openai.OpenAIError when no override is supplied and OPENAI_API_KEY is absent from the environment."""
    if overrides.claim_questions_agent is not None:
        return overrides.claim_questions_agent
    return make_claim_questions_agent(config)


def answer_synthesis_agent_or_default(overrides: PipelineOverrides, config: Config) -> AnswerSynthesisAgent:
    """Return the overridden answer-synthesis agent, else the LLM agent over the open-ended research tools, raising openai.OpenAIError when no override is supplied and OPENAI_API_KEY is absent from the environment."""
    if overrides.answer_synthesis_agent is not None:
        return overrides.answer_synthesis_agent
    return make_answer_synthesis_agent(open_ended_research_tools_or_default(overrides, config), config)


def corroboration_agent_or_default(overrides: PipelineOverrides) -> CorroborationAgent:
    """Return the overridden corroboration agent, else the LLM corroboration agent."""
    if overrides.corroboration_agent is not None:
        return overrides.corroboration_agent
    return corroborate


@dataclass(frozen=True)
class _CapitalCriticalDeps:
    """The three dependencies that move real capital, resolved and guaranteed non-None."""

    fetch_portfolio: PortfolioFetcher
    place_order: OrderPlacer
    observe_fill: FillObserver
    send_email: EmailSender


def _require_capital_critical_deps(overrides: PipelineOverrides) -> _CapitalCriticalDeps:
    """Return the capital-critical dependencies, raising MissingPipelineDependencyError if any is None."""
    fetch_portfolio = overrides.fetch_portfolio
    place_order = overrides.place_order
    observe_fill = overrides.observe_fill
    send_email = overrides.send_email
    if fetch_portfolio is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="fetch_portfolio"))
    if place_order is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="place_order"))
    if observe_fill is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="observe_fill"))
    if send_email is None:
        raise MissingPipelineDependencyError(_MISSING_DEP_MESSAGE.format(name="send_email"))
    return _CapitalCriticalDeps(
        fetch_portfolio=fetch_portfolio,
        place_order=place_order,
        observe_fill=observe_fill,
        send_email=send_email,
    )


def run_pipeline(
    signals_dir: Path,
    *,
    run_dir: Path | None = None,
    overrides: PipelineOverrides | None = None,
    stop_before_execution: bool = False,
) -> PipelineState:
    """Execute the pipeline on the signals in signals_dir and return the final state, pausing before execution when asked."""
    ov: PipelineOverrides = overrides or PipelineOverrides()
    capital_deps: _CapitalCriticalDeps = _require_capital_critical_deps(ov)

    slug: str = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
    working_dir: Path = run_dir if run_dir is not None else DAILY_SHOW_ROOT / slug
    working_dir.mkdir(parents=True, exist_ok=True)

    signals_out: Path = working_dir / SIGNALS_DIRNAME
    signals_out.mkdir(exist_ok=True)

    for signal_file in signals_dir.glob("*.json"):
        _ = shutil.copy2(signal_file, signals_out / signal_file.name)

    config = load_config()

    graph = build_graph(
        fetch_portfolio=capital_deps.fetch_portfolio,
        corroboration_agent=corroboration_agent_or_default(ov),
        claim_questions_agent=claim_questions_agent_or_default(ov, config),
        answer_synthesis_agent=answer_synthesis_agent_or_default(ov, config),
        deterministic_tools=deterministic_research_tools_or_default(ov, config),
        config=config,
        thesis_agent=thesis_agent_or_default(ov, config),
        resolve_instrument_facts=instrument_facts_resolver_or_default(ov),
        place_order=capital_deps.place_order,
        observe_fill=capital_deps.observe_fill,
        send_email=with_undelivered_record(capital_deps.send_email, working_dir),
        manifest=ov.manifest,
        stop_before_execution=stop_before_execution,
    )

    initial_state: PipelineState = {
        "slug": slug,
        "working_dir": str(working_dir),
        "completed_steps": [],
    }

    return graph.invoke(initial_state, config=thread_config(slug))
