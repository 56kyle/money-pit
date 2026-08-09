"""Module containing concrete read-only portfolio and market providers."""
# pyright: reportMissingTypeStubs=false

import hashlib
import json
import math
import re
import sqlite3  # noqa: TC003 - sqlite Row is required by runtime validation.
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from collections.abc import Mapping
from datetime import UTC
from datetime import datetime
from itertools import pairwise
from pathlib import Path
from typing import ClassVar
from typing import Protocol
from typing import cast

import numpy as np
import yfinance as yf
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import TypeAdapter

from money_pit.claims.projection import ClaimRefreshPolicy
from money_pit.claims.repository import ClaimRepository
from money_pit.config import AlpacaCredentials
from money_pit.execution_control.errors import StateCheckUnavailableError
from money_pit.execution_control.models import ConfirmedFill
from money_pit.portfolio.decision_repository import DecisionSnapshotRepository
from money_pit.portfolio.eligibility import SupportedInstrumentKind
from money_pit.portfolio.providers import InstrumentLiquidity
from money_pit.portfolio.providers import InstrumentRisk
from money_pit.portfolio.providers import LiquiditySnapshot
from money_pit.portfolio.providers import LiquiditySnapshotPayload
from money_pit.portfolio.providers import RiskSnapshot
from money_pit.portfolio.providers import RiskSnapshotPayload
from money_pit.portfolio.snapshots import MarketQuote
from money_pit.portfolio.snapshots import MarketStatePayload
from money_pit.portfolio.snapshots import MarketStateSnapshot
from money_pit.portfolio.snapshots import PortfolioStatePayload
from money_pit.portfolio.snapshots import PortfolioStatePosition
from money_pit.portfolio.snapshots import PortfolioStateSnapshot
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.portfolio_plan import PlannedLotSelection
from money_pit.schemas.portfolio_plan import PortfolioPlan
from money_pit.schemas.snapshots import DecisionSnapshot  # noqa: TC001
from money_pit.schemas.tax import TaxLot
from money_pit.schemas.tax import TaxLotSnapshot
from money_pit.schemas.tax import WashSaleStatus
from money_pit.storage.database import Database


_JSON_ADAPTER: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_INSTRUMENT_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_ALPACA_READ_PATH_PATTERN = re.compile(r"^/(?:account|positions|orders|assets/[A-Z][A-Z0-9.%_-]{0,29})$")
_MAXIMUM_ALPACA_RESPONSE_BYTES = 5_000_000
_ALLOWED_HISTORY_PERIODS = frozenset({"3mo", "6mo", "1y", "2y", "5y"})
_MAXIMUM_HISTORY_ROWS = 1_300


class PortfolioProviderError(StateCheckUnavailableError):
    """Raised when a read-only state provider cannot produce complete typed state."""


class AlpacaReadTransport(Protocol):
    """Transport exposing only bounded GET requests to the broker API."""

    def get_json(self, path: str, *, query: Mapping[str, str] | None = None) -> object:
        """Return one decoded JSON response."""
        ...


class _ReadableResponse(Protocol):
    def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> None: ...


class _UrlLibAlpacaReadTransport:
    def __init__(self, credentials: AlpacaCredentials, *, timeout_seconds: float = 10.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Alpaca read timeout must be positive")
        self._base_url: str = (
            "https://paper-api.alpaca.markets/v2" if credentials.paper else "https://api.alpaca.markets/v2"
        )
        self._headers: dict[str, str] = {
            "APCA-API-KEY-ID": credentials.api_key,
            "APCA-API-SECRET-KEY": credentials.secret_key.get_secret_value(),
            "Accept": "application/json",
        }
        self._timeout_seconds: float = timeout_seconds

    def get_json(self, path: str, *, query: Mapping[str, str] | None = None) -> object:
        canonical_path = path if path.startswith("/") else f"/{path}"
        if _ALPACA_READ_PATH_PATTERN.fullmatch(canonical_path) is None:
            raise PortfolioProviderError("Alpaca read path is not authorized")
        if query is not None and (canonical_path != "/orders" or set(query) != {"status"}):
            raise PortfolioProviderError("Alpaca read query is not authorized")
        query_text = "" if query is None else f"?{urllib.parse.urlencode(query)}"
        request = urllib.request.Request(  # noqa: S310 - fixed HTTPS authority and GET-only transport.
            f"{self._base_url}{canonical_path}{query_text}",
            headers=self._headers,
            method="GET",
        )
        try:
            response = cast(
                "_ReadableResponse",
                # The constructor validates a fixed HTTPS origin and paths are canonicalized above.
                cast("object", urllib.request.urlopen(request, timeout=self._timeout_seconds)),  # noqa: S310  # nosec B310
            )
            try:
                encoded = response.read(_MAXIMUM_ALPACA_RESPONSE_BYTES + 1)
                if len(encoded) > _MAXIMUM_ALPACA_RESPONSE_BYTES:
                    raise PortfolioProviderError("Alpaca read response exceeds the configured bound")
                payload = encoded.decode("utf-8")
            finally:
                response.close()
            return _JSON_ADAPTER.validate_json(payload)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, urllib.error.HTTPError) as error:
            raise PortfolioProviderError(f"Alpaca read failed for {canonical_path}") from error


class _AlpacaAccount(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")
    id: str
    cash: str
    status: str
    trading_blocked: bool


class _AlpacaPosition(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")
    symbol: str
    qty: str
    current_price: str
    market_value: str


class _AlpacaOrder(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")
    id: str


class _AlpacaAsset(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")
    asset_class: str
    tradable: bool


class _Series(Protocol):
    def tolist(self) -> list[object]: ...


class _Frame(Protocol):
    index: _Series

    def __getitem__(self, name: str) -> _Series: ...


class _Ticker(Protocol):
    def history(self, **kwargs: object) -> _Frame: ...


class _TaxLedgerDocument(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    lots: tuple[TaxLot, ...] = ()
    complete_for_known_accounts: bool = False
    unknown_external_activity: bool = True
    short_term_tax_rate: float | None = None
    long_term_tax_rate: float | None = None
    wash_sale_status: dict[str, WashSaleStatus] = Field(default_factory=dict)


class AlpacaPortfolioStateProvider:
    """Read authoritative account, position, cash, and open-order state from Alpaca."""

    def __init__(
        self,
        credentials: AlpacaCredentials,
        *,
        transport: AlpacaReadTransport | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
        instrument_asset_classes: Mapping[str, TradableAssetClass] | None = None,
    ) -> None:
        """Construct a GET-only paper- or live-scoped broker reader."""
        self._transport: AlpacaReadTransport = transport or _UrlLibAlpacaReadTransport(credentials)
        self._broker_environment: BrokerEnvironment = credentials.broker_environment
        self._clock: Callable[[], datetime] = clock
        configured_asset_classes: Mapping[str, TradableAssetClass] = (
            {} if instrument_asset_classes is None else instrument_asset_classes
        )
        self._instrument_asset_classes: dict[str, TradableAssetClass] = {
            _canonical_instrument(instrument): kind for instrument, kind in configured_asset_classes.items()
        }

    def snapshot(self) -> PortfolioStateSnapshot:
        """Return current broker state as one content-addressed snapshot."""
        try:
            return self._snapshot()
        except (ValueError, TypeError) as error:
            raise PortfolioProviderError("Alpaca portfolio response is malformed") from error

    def _snapshot(self) -> PortfolioStateSnapshot:
        captured_at = self._clock()
        account = _AlpacaAccount.model_validate(self._transport.get_json("/account"))
        raw_positions = TypeAdapter(list[_AlpacaPosition]).validate_python(self._transport.get_json("/positions"))
        raw_orders = TypeAdapter(list[_AlpacaOrder]).validate_python(
            self._transport.get_json("/orders", query={"status": "open"})
        )
        positions: tuple[PortfolioStatePosition, ...] = tuple(
            sorted(
                (
                    PortfolioStatePosition(
                        instrument=str(item.symbol).upper(),
                        quantity=_finite_float(item.qty, "position quantity"),
                        market_price=_finite_float(item.current_price, "position market price"),
                        market_value=_finite_float(item.market_value, "position market value"),
                    )
                    for item in raw_positions
                ),
                key=lambda item: item.instrument,
            )
        )
        payload = PortfolioStatePayload(
            account_id=str(account.id),
            broker_environment=self._broker_environment,
            account_status=account.status,
            trading_blocked=account.trading_blocked,
            captured_at=captured_at,
            available_cash=_finite_float(account.cash, "available cash"),
            positions=positions,
            open_order_ids=tuple(sorted(str(order.id) for order in raw_orders)),
        )
        return PortfolioStateSnapshot.from_payload(payload)

    def instrument_authority(self, instrument: str) -> tuple[SupportedInstrumentKind, bool]:
        """Return broker-authoritative US-equity asset class and tradability."""
        symbol = _canonical_instrument(instrument)
        try:
            asset = _AlpacaAsset.model_validate(
                self._transport.get_json(f"/assets/{urllib.parse.quote(symbol, safe='')}")
            )
        except (ValueError, TypeError) as error:
            raise PortfolioProviderError("Alpaca asset response is malformed") from error
        asset_class = asset.asset_class
        if asset_class != "us_equity":
            return SupportedInstrumentKind.UNSUPPORTED, False
        configured_kind = self._instrument_asset_classes.get(symbol)
        if configured_kind is None:
            return SupportedInstrumentKind.UNSUPPORTED, False
        return SupportedInstrumentKind(configured_kind.value), bool(asset.tradable)


class YFinanceStateProvider:
    """Bounded quote, covariance, factor, and liquidity reads from yfinance."""

    def __init__(
        self,
        *,
        historical_period: str = "1y",
        maximum_participation_rate: float,
        minimum_average_daily_notional: float,
        factor_loadings: Mapping[str, Mapping[str, float]],
        sector_taxonomy: Mapping[str, str],
        timeout_seconds: float = 10.0,
    ) -> None:
        """Bind configured market-history and liquidity policy."""
        if not 0 < maximum_participation_rate <= 1:
            raise ValueError("maximum participation rate must be in (0, 1]")
        if minimum_average_daily_notional <= 0 or timeout_seconds <= 0:
            raise ValueError("market-data bounds must be positive")
        if historical_period not in _ALLOWED_HISTORY_PERIODS:
            raise ValueError("market history period must be one of the bounded supported periods")
        self._historical_period: str = historical_period
        self._participation_rate: float = maximum_participation_rate
        self._minimum_average_daily_notional: float = minimum_average_daily_notional
        self._factor_loadings: dict[str, dict[str, float]] = {
            instrument.strip().upper(): dict(loadings) for instrument, loadings in factor_loadings.items()
        }
        self._sector_taxonomy: dict[str, str] = {
            instrument.strip().upper(): sector for instrument, sector in sector_taxonomy.items()
        }
        self._timeout_seconds: float = timeout_seconds

    def snapshot(self, instruments: tuple[str, ...]) -> MarketStateSnapshot:
        """Return current quotes for the exact canonical universe."""
        try:
            return self._market_snapshot(instruments)
        except PortfolioProviderError:
            raise
        except Exception as error:
            raise PortfolioProviderError("market quote provider is unavailable or malformed") from error

    def _market_snapshot(self, instruments: tuple[str, ...]) -> MarketStateSnapshot:
        canonical = _canonical_instruments(instruments)
        captured_at = datetime.now(tz=UTC)
        quotes: list[MarketQuote] = []
        for instrument in canonical:
            ticker = cast("_Ticker", cast("object", yf.Ticker(instrument)))
            history = ticker.history(period="1d", interval="1m", auto_adjust=False, timeout=self._timeout_seconds)
            dated_closes = _dated_numeric_column(history, "Close")
            closes = tuple(dated_closes.values())
            if not closes:
                raise PortfolioProviderError(f"market price unavailable for {instrument}")
            quotes.append(
                MarketQuote(
                    instrument=instrument,
                    price=closes[-1],
                    observed_at=self._market_timestamp(next(reversed(dated_closes))),
                    source="yfinance",
                )
            )
        return MarketStateSnapshot.from_payload(MarketStatePayload(captured_at=captured_at, quotes=tuple(quotes)))

    @staticmethod
    def _market_timestamp(value: str) -> datetime:
        """Parse an actual provider observation timestamp as an aware instant."""
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise PortfolioProviderError("market quote timestamp is timezone-naive")
        return parsed

    def risk_snapshot(self, instruments: tuple[str, ...]) -> RiskSnapshot:
        """Return annualized historical covariance and configured factor axes."""
        try:
            return self._risk_snapshot(instruments)
        except PortfolioProviderError:
            raise
        except Exception as error:
            raise PortfolioProviderError("risk provider is unavailable or malformed") from error

    def _risk_snapshot(self, instruments: tuple[str, ...]) -> RiskSnapshot:
        canonical = _canonical_instruments(instruments)
        closes_by_instrument: dict[str, dict[str, float]] = {}
        if set(self._factor_loadings) != set(canonical) or set(self._sector_taxonomy) != set(canonical):
            raise PortfolioProviderError("configured factor and sector coverage must exactly match the universe")
        factor_axes = {factor for loadings in self._factor_loadings.values() for factor in loadings}
        if any(set(loadings) != factor_axes for loadings in self._factor_loadings.values()):
            raise PortfolioProviderError("factor-loading rows must cover the same configured axes")
        for instrument in canonical:
            ticker = cast("_Ticker", cast("object", yf.Ticker(instrument)))
            closes = _dated_numeric_column(
                ticker.history(
                    period=self._historical_period,
                    interval="1d",
                    auto_adjust=True,
                    timeout=self._timeout_seconds,
                ),
                "Close",
            )
            if len(closes) < 20:
                raise PortfolioProviderError(f"insufficient return history for {instrument}")
            closes_by_instrument[instrument] = closes
        common_date_set: set[str] = set(next(iter(closes_by_instrument.values())))
        for values in closes_by_instrument.values():
            common_date_set.intersection_update(values)
        common_dates: list[str] = sorted(common_date_set)
        common_dates = common_dates[-_MAXIMUM_HISTORY_ROWS:]
        if len(common_dates) < 20:
            raise PortfolioProviderError("insufficient aligned return dates across the universe")
        returns = {
            instrument: [
                right / left - 1
                for left, right in pairwise([closes_by_instrument[instrument][date] for date in common_dates])
            ]
            for instrument in canonical
        }
        matrix = np.asarray([returns[name] for name in canonical], dtype=np.float64)
        covariance = np.atleast_2d(np.cov(matrix, rowvar=True, ddof=1)) * 252.0
        if covariance.shape != (len(canonical), len(canonical)) or not np.all(np.isfinite(covariance)):
            raise PortfolioProviderError("historical covariance is incomplete or non-finite")
        observations = tuple(
            InstrumentRisk(
                instrument=instrument,
                sector=self._sector_taxonomy[instrument],
                factor_loadings=self._factor_loadings[instrument],
                covariance={
                    other: float(cast("np.float64", covariance[index, other_index]))
                    for other_index, other in enumerate(canonical)
                },
            )
            for index, instrument in enumerate(canonical)
        )
        return RiskSnapshot.from_payload(
            RiskSnapshotPayload(captured_at=datetime.now(tz=UTC), observations=observations)
        )

    def liquidity_snapshot(self, instruments: tuple[str, ...]) -> LiquiditySnapshot:
        """Return average daily notional and a conservative slippage estimate."""
        try:
            return self._liquidity_snapshot(instruments)
        except PortfolioProviderError:
            raise
        except Exception as error:
            raise PortfolioProviderError("liquidity provider is unavailable or malformed") from error

    def _liquidity_snapshot(self, instruments: tuple[str, ...]) -> LiquiditySnapshot:
        canonical = _canonical_instruments(instruments)
        observations: list[InstrumentLiquidity] = []
        for instrument in canonical:
            ticker = cast("_Ticker", cast("object", yf.Ticker(instrument)))
            history = ticker.history(period="3mo", interval="1d", auto_adjust=False, timeout=self._timeout_seconds)
            closes = _dated_numeric_column(history, "Close")
            volumes = _dated_numeric_column(history, "Volume")
            if closes.keys() != volumes.keys():
                raise PortfolioProviderError(f"liquidity close/volume dates differ for {instrument}")
            aligned_dates = tuple(closes)[-_MAXIMUM_HISTORY_ROWS:]
            notionals = [closes[date] * volumes[date] for date in aligned_dates if volumes[date] > 0]
            if not notionals:
                raise PortfolioProviderError(f"liquidity history unavailable for {instrument}")
            average_notional = sum(notionals) / len(notionals)
            if average_notional < self._minimum_average_daily_notional:
                raise PortfolioProviderError(f"average daily notional is below policy for {instrument}")
            observations.append(
                InstrumentLiquidity(
                    instrument=instrument,
                    average_daily_notional=average_notional,
                    maximum_participation_rate=self._participation_rate,
                    estimated_slippage_bps=10000.0 / math.sqrt(max(average_notional, 1.0)),
                    tradable=True,
                )
            )
        return LiquiditySnapshot.from_payload(
            LiquiditySnapshotPayload(captured_at=datetime.now(tz=UTC), observations=tuple(observations))
        )


class ConfiguredTaxLotProvider:
    """Read a configured local tax-lot ledger without inferring broker aggregate basis."""

    def __init__(self, path: Path) -> None:
        """Bind the operator-owned ledger path."""
        self._path: Path = path

    def snapshot(self) -> TaxLotSnapshot:
        """Return known lots, explicitly marking a missing ledger incomplete."""
        captured_at = datetime.now(tz=UTC)
        if not self._path.is_file():
            return _tax_snapshot(captured_at, (), complete=False, unknown=True)
        try:
            document = _TaxLedgerDocument.model_validate_json(self._path.read_text(encoding="utf-8"))
            lots = document.lots
            complete = document.complete_for_known_accounts
            unknown = document.unknown_external_activity
        except (OSError, ValueError, TypeError) as error:
            raise PortfolioProviderError(f"tax-lot ledger is invalid: {self._path}") from error
        return _tax_snapshot(
            captured_at,
            lots,
            complete=complete,
            unknown=unknown,
            short_term_tax_rate=document.short_term_tax_rate,
            long_term_tax_rate=document.long_term_tax_rate,
            wash_sale_status=document.wash_sale_status,
        )


class YFinanceRiskStateProvider:
    """Expose the shared bounded market reader through the risk protocol."""

    def __init__(self, provider: YFinanceStateProvider) -> None:
        """Bind the shared market reader."""
        self._provider: YFinanceStateProvider = provider

    def snapshot(self, instruments: tuple[str, ...]) -> RiskSnapshot:
        """Return current configured risk state."""
        return self._provider.risk_snapshot(instruments)


class YFinanceLiquidityStateProvider:
    """Expose the shared bounded market reader through the liquidity protocol."""

    def __init__(self, provider: YFinanceStateProvider) -> None:
        """Bind the shared market reader."""
        self._provider: YFinanceStateProvider = provider

    def snapshot(self, instruments: tuple[str, ...]) -> LiquiditySnapshot:
        """Return current bounded liquidity state."""
        return self._provider.liquidity_snapshot(instruments)


class DurableEvidenceFreshnessReader:
    """Re-evaluate plan-bound observations from durable verification state."""

    def __init__(
        self,
        database: Database,
        *,
        refresh_policy: ClaimRefreshPolicy,
        clock: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    ) -> None:
        """Bind durable intelligence and an injectable current clock."""
        self._database: Database = database
        self._decisions: DecisionSnapshotRepository = DecisionSnapshotRepository(database)
        self._claims: ClaimRepository = ClaimRepository(database, refresh_policy=refresh_policy)
        self._clock: Callable[[], datetime] = clock

    def is_fresh(self, plan: PortfolioPlan, confirmed_fills: tuple[ConfirmedFill, ...]) -> bool:
        """Require every evidence gate and latest material verification to remain supportive."""
        del confirmed_fills
        if not plan.payload.evidence_gate_results or not all(plan.payload.evidence_gate_results.values()):
            return False
        with self._database.transaction() as connection:
            decision: DecisionSnapshot | None = self._decisions.get(plan.payload.decision_snapshot_id)
            if decision is None or decision.decision_hash != plan.payload.decision_snapshot_hash:
                return False
            observation_ids: tuple[str, ...] = decision.payload.claim_observation_ids
            if not observation_ids:
                return False
            now_text = self._clock().isoformat()
            for observation_id in observation_ids:
                verification: sqlite3.Row | None = cast(
                    "sqlite3.Row | None",
                    connection.execute(
                        """
                    SELECT status, valid_until FROM verification_results
                    WHERE observation_id = ? AND known_at <= ?
                    ORDER BY known_at DESC, checked_at DESC, verification_id DESC LIMIT 1
                    """,
                        (str(observation_id), now_text),
                    ).fetchone(),
                )
                if verification is None or str(cast("object", verification["status"])) != "supported":
                    return False
                valid_until: object = cast("object", verification["valid_until"])
                if valid_until is not None and str(valid_until) <= now_text:
                    return False
            current_projections = {
                item.canonical_claim_key: _model_hash(item.model_dump(mode="json"))
                for item in self._claims.projections_as_of(as_of=self._clock())
            }
            if any(
                current_projections.get(key) != expected_hash
                for key, expected_hash in decision.payload.canonical_projection_hashes.items()
            ):
                return False
        return True


class ExactTaxStateValidator:
    """Require the configured ledger to match exactly before the first fill."""

    def matches(
        self,
        plan: PortfolioPlan,
        bound: TaxLotSnapshot,
        current: TaxLotSnapshot,
        confirmed_fills: tuple[ConfirmedFill, ...],
    ) -> bool:
        """Fail closed after fills until the external lot ledger is reconciled."""
        expected: dict[str, float] = {lot.lot_id: lot.quantity for lot in bound.lots}
        trade_by_instrument = {
            trade.instrument: trade for trade in plan.payload.proposed_trades if trade.side == "sell"
        }
        for fill in confirmed_fills:
            if fill.side == "buy":
                continue
            trade = trade_by_instrument.get(fill.instrument)
            if (
                trade is None
                or not isinstance(trade.lot_selection, PlannedLotSelection)
                or fill.filled_qty > trade.quantity + 1e-9
            ):
                return False
            remaining_fill = fill.filled_qty
            for selected in trade.lot_selection.lots:
                consumed = min(selected.quantity, remaining_fill)
                expected[selected.lot_id] = expected.get(selected.lot_id, 0.0) - consumed
                remaining_fill -= consumed
                if remaining_fill <= 1e-9:
                    break
            if remaining_fill > 1e-9:
                return False
        actual: dict[str, float] = {lot.lot_id: lot.quantity for lot in current.lots}
        expected = {lot_id: quantity for lot_id, quantity in expected.items() if quantity > 1e-9}
        return (
            actual.keys() == expected.keys()
            and all(math.isclose(actual[lot_id], quantity, abs_tol=1e-9) for lot_id, quantity in expected.items())
            and bound.complete_for_known_accounts == current.complete_for_known_accounts
            and bound.unknown_external_activity == current.unknown_external_activity
        )


def _tax_snapshot(
    captured_at: datetime,
    lots: tuple[TaxLot, ...],
    *,
    complete: bool,
    unknown: bool,
    short_term_tax_rate: float | None = None,
    long_term_tax_rate: float | None = None,
    wash_sale_status: Mapping[str, WashSaleStatus] | None = None,
) -> TaxLotSnapshot:
    payload = {
        "captured_at": captured_at.isoformat(),
        "lots": [lot.model_dump(mode="json") for lot in lots],
        "complete_for_known_accounts": complete,
        "unknown_external_activity": unknown,
        "short_term_tax_rate": short_term_tax_rate,
        "long_term_tax_rate": long_term_tax_rate,
        "wash_sale_status": {} if wash_sale_status is None else dict(wash_sale_status),
    }
    snapshot_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return TaxLotSnapshot(
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        lots=lots,
        complete_for_known_accounts=complete,
        unknown_external_activity=unknown,
        short_term_tax_rate=short_term_tax_rate,
        long_term_tax_rate=long_term_tax_rate,
        wash_sale_status={} if wash_sale_status is None else dict(wash_sale_status),
    )


def _canonical_instruments(instruments: tuple[str, ...]) -> tuple[str, ...]:
    canonical = tuple(sorted({_canonical_instrument(item) for item in instruments}))
    if not canonical:
        raise PortfolioProviderError("market provider requires at least one instrument")
    return canonical


def _canonical_instrument(instrument: str) -> str:
    canonical = instrument.strip().upper()
    if _INSTRUMENT_PATTERN.fullmatch(canonical) is None:
        raise PortfolioProviderError("instrument does not match the bounded US ticker syntax")
    return canonical


def _dated_numeric_column(frame: object, name: str) -> dict[str, float]:
    try:
        typed = cast("_Frame", frame)
        dates = typed.index.tolist()
        values = typed[name].tolist()
    except (AttributeError, KeyError, TypeError) as error:
        raise PortfolioProviderError(f"market response lacks {name}") from error
    if len(dates) != len(values) or len({str(item) for item in dates}) != len(dates):
        raise PortfolioProviderError(f"market response has invalid {name} date alignment")
    if any(value is None for value in values):
        raise PortfolioProviderError(f"market response has missing {name} values")
    return {str(date): _finite_float(value, name) for date, value in zip(dates, values, strict=True)}


def _finite_float(value: object, label: str) -> float:
    try:
        number = float(str(value))
    except (TypeError, ValueError) as error:
        raise PortfolioProviderError(f"{label} is not numeric") from error
    if not math.isfinite(number):
        raise PortfolioProviderError(f"{label} is not finite")
    return number


def _model_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()
