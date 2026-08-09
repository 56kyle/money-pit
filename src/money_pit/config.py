"""Module containing validated application configuration boundaries."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path  # noqa: TC003 - Pydantic resolves this runtime field type.
from typing import TYPE_CHECKING
from typing import ClassVar
from typing import Self
from typing import TypeVar

import keyring
import tomllib
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import SecretStr
from pydantic import ValidationError
from pydantic import model_validator
from pydantic_settings import BaseSettings
from pydantic_settings import SettingsConfigDict

from money_pit.constants import default_config_path
from money_pit.constants import default_execution_config_path
from money_pit.constants import default_sources_config_path
from money_pit.constants import default_strategy_config_path
from money_pit.schemas.claims import ClaimCategory
from money_pit.schemas.claims import HorizonClass
from money_pit.schemas.execution_policy import BrokerEnvironment
from money_pit.schemas.execution_policy import ExecutionMode
from money_pit.schemas.execution_policy import TradableAssetClass
from money_pit.schemas.instrument import (
    InstrumentExposureClass,  # noqa: TC001 - Pydantic resolves this runtime field type.
)
from money_pit.schemas.sources import SourceRegistryDocument


if TYPE_CHECKING:
    from money_pit.claims.projection import ClaimRefreshPolicy


ENV_PREFIX: str = "MONEY_PIT__"
_DEFAULT_LLM_MODEL: str = "gpt-5"
_BLANK_KEYRING_FIELD_MESSAGE: str = "Blank {field} configured; cannot resolve {credential}."


class ConfigurationError(Exception):
    """Base class for configuration loading failures."""


class ConfigurationFileError(ConfigurationError):
    """Raised when a required TOML document cannot be read or decoded."""


class ConfigurationValidationError(ConfigurationError):
    """Raised when a TOML document does not match its typed contract."""


class CredentialResolutionError(ConfigurationError):
    """Raised when a requested credential cannot be resolved."""


@dataclass(frozen=True)
class AlpacaCredentials:
    """Resolved Alpaca API credentials for one explicit broker environment."""

    api_key: str
    secret_key: SecretStr
    broker_environment: BrokerEnvironment

    @property
    def paper(self) -> bool:
        """Return the alpaca-py environment flag."""
        return self.broker_environment is BrokerEnvironment.PAPER


class Config(BaseSettings):
    """Secrets and provider settings loaded only from the process environment."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(env_prefix=ENV_PREFIX, frozen=True, extra="forbid")

    alpaca_service: str | None = None
    alpaca_username: str | None = None
    fred_api_key: SecretStr | None = None
    brave_search_api_key: SecretStr | None = None
    sec_user_agent: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    youtube_api_key: SecretStr | None = None
    imap_password: SecretStr | None = None
    llm_model: str = _DEFAULT_LLM_MODEL


class HorizonPolicy(BaseModel):
    """Versioned time bounds and review cadence for one investment horizon."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    minimum_days: int = Field(ge=0)
    maximum_days: int | None = Field(default=None, ge=0)
    review_interval_days: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_bounds(self) -> Self:
        """Require the optional upper bound to include the lower bound."""
        if self.maximum_days is not None and self.maximum_days < self.minimum_days:
            raise ValueError("maximum_days must be greater than or equal to minimum_days.")
        return self


class ResearchBudgetConfig(BaseModel):
    """Deterministic per-candidate research limits."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    maximum_rounds: int = Field(default=3, ge=1)
    maximum_queries: int = Field(default=12, ge=1)
    maximum_fetches: int = Field(default=24, ge=1)
    maximum_elapsed_seconds: int = Field(default=600, ge=1)


class QuantitativeScreenConfig(BaseModel):
    """Versioned deterministic output from one externally evaluated screen."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    screen_id: str = Field(min_length=1)
    instruments: tuple[str, ...] = Field(min_length=1)


class ReturnBoundPolicy(StrEnum):
    """Deterministic handling of agent-authored scenario returns outside policy."""

    REJECT = "reject"
    CLAMP = "clamp"


class ClaimFreshnessRuleConfig(BaseModel):
    """Configured review and freshness intervals for one claim category and horizon."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    review_interval_days: int = Field(gt=0)
    freshness_days: int = Field(gt=0)


class ClaimFreshnessPolicyConfig(BaseModel):
    """Complete versioned claim-freshness matrix."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")
    version: str = Field(min_length=1)
    rules: dict[ClaimCategory, dict[HorizonClass, ClaimFreshnessRuleConfig]]

    @model_validator(mode="after")
    def validate_complete_matrix(self) -> Self:
        """Require an explicit rule for every category and horizon."""
        if set(self.rules) != set(ClaimCategory):
            raise ValueError("claim freshness rules must define every claim category.")
        for category, horizons in self.rules.items():
            if set(horizons) != set(HorizonClass):
                raise ValueError(f"claim freshness rules for {category.value} must define every horizon.")
        return self


class StrategyConfig(BaseModel):
    """Versioned capital-sensitive strategy and optimizer configuration."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    portfolio_environment: BrokerEnvironment
    plan_ttl_seconds: int = Field(gt=0)
    watchlist: tuple[str, ...] = ()
    benchmark_constituents: tuple[str, ...] = ()
    benchmark_id: str = Field(min_length=1)
    benchmark_provider: str = Field(min_length=1)
    benchmark_weights: dict[str, float] = Field(min_length=1)
    explicit_proxies: dict[str, str] = Field(default_factory=dict)
    quantitative_screens: tuple[QuantitativeScreenConfig, ...] = ()
    sector_taxonomy: dict[str, str] = Field(min_length=1)
    instrument_asset_classes: dict[str, TradableAssetClass] = Field(default_factory=dict)
    instrument_exposure_classes: dict[str, InstrumentExposureClass] = Field(default_factory=dict)
    factor_loadings: dict[str, dict[str, float]] = Field(min_length=1)
    default_position_weight_limit: float = Field(gt=0, le=1)
    instrument_weight_limits: dict[str, float] = Field(default_factory=dict)
    maximum_equity_exposure: float = Field(gt=0, le=1)
    maximum_single_stock_exposure: float = Field(gt=0, le=1)
    maximum_thematic_etf_exposure: float = Field(gt=0, le=1)
    sector_weight_limit: float = Field(gt=0, le=1)
    factor_weight_limit: float = Field(gt=0, le=1)
    correlated_exposure_limit: float = Field(gt=0, le=1)
    cash_minimum: float = Field(ge=0, le=1)
    turnover_limit: float = Field(ge=0, le=1)
    position_change_limit: float = Field(gt=0, le=1)
    minimum_trade_notional: float = Field(gt=0)
    maximum_slippage_fraction: float = Field(ge=0, lt=1)
    minimum_average_daily_notional: float = Field(gt=0)
    maximum_daily_volume_participation: float = Field(gt=0, le=1)
    market_history_period: str = Field(min_length=1)
    tax_lot_ledger_path: Path
    tax_lot_policy: str = Field(min_length=1)
    specific_tax_lot_ids: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    expected_return_calibration_version: str = Field(min_length=1)
    expected_return_bounds_version: str = Field(min_length=1)
    expected_return_uncertainty_multiplier: float = Field(ge=0, le=1)
    scenario_return_floor: float = Field(ge=-1, le=0)
    scenario_return_ceiling: float = Field(gt=0)
    calibrated_return_floor: float = Field(ge=-1, le=0)
    calibrated_return_ceiling: float = Field(gt=0)
    return_bound_policy: ReturnBoundPolicy
    risk_aversion: float = Field(gt=0)
    turnover_penalty: float = Field(ge=0)
    tax_penalty: float = Field(ge=0)
    accept_optimal_inaccurate: bool = False
    feasibility_tolerance: float = Field(gt=0, le=0.01)
    correlated_exposure_groups: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    horizons: dict[str, HorizonPolicy] = Field(min_length=4, max_length=4)
    research_budget: ResearchBudgetConfig = Field(default_factory=ResearchBudgetConfig)
    claim_freshness: ClaimFreshnessPolicyConfig

    @model_validator(mode="after")
    def validate_horizons_and_universe(self) -> Self:
        """Require canonical horizons and complete capital-universe metadata."""
        expected_horizons: set[str] = {"event", "tactical", "medium_term", "structural"}
        if set(self.horizons) != expected_horizons:
            raise ValueError("horizons must define event, tactical, medium_term, and structural exactly.")
        capital_universe: set[str] = {
            *self.watchlist,
            *self.benchmark_constituents,
            *self.explicit_proxies.values(),
            *(instrument for screen in self.quantitative_screens for instrument in screen.instruments),
        }
        metadata_maps: tuple[tuple[str, set[str]], ...] = (
            ("sector_taxonomy", set(self.sector_taxonomy)),
            ("instrument_asset_classes", set(self.instrument_asset_classes)),
            ("instrument_exposure_classes", set(self.instrument_exposure_classes)),
            ("factor_loadings", set(self.factor_loadings)),
        )
        for name, instruments in metadata_maps:
            if instruments != capital_universe:
                raise ValueError(f"{name} must exactly cover the configured capital universe")
        if not set(self.instrument_weight_limits) <= capital_universe:
            raise ValueError("instrument_weight_limits contains an instrument outside the capital universe")
        if any(weight <= 0 or weight > 1 for weight in self.instrument_weight_limits.values()):
            raise ValueError("instrument_weight_limits values must be in (0, 1]")
        if self.tax_lot_policy == "specific_id" and not self.specific_tax_lot_ids:
            raise ValueError("specific-ID tax policy requires configured ordered lot IDs")
        if set(self.benchmark_weights) != set(self.benchmark_constituents):
            raise ValueError("benchmark weights must exactly cover configured constituents")
        if any(weight <= 0 or weight > 1 for weight in self.benchmark_weights.values()) or not math.isclose(
            sum(self.benchmark_weights.values()), 1.0, abs_tol=1e-9
        ):
            raise ValueError("benchmark weights must be positive and sum to one")
        return self


class StrategyIntelligenceConfig(BaseModel):
    """Non-capital research, freshness, and discovery projection of strategy.toml."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="ignore")
    version: str = Field(min_length=1)
    watchlist: tuple[str, ...] = ()
    benchmark_constituents: tuple[str, ...] = ()
    explicit_proxies: dict[str, str] = Field(default_factory=dict)
    quantitative_screens: tuple[QuantitativeScreenConfig, ...] = ()
    horizons: dict[str, HorizonPolicy] = Field(min_length=4, max_length=4)
    research_budget: ResearchBudgetConfig = Field(default_factory=ResearchBudgetConfig)
    claim_freshness: ClaimFreshnessPolicyConfig

    @model_validator(mode="after")
    def validate_horizons(self) -> Self:
        """Require the canonical investment horizons."""
        if set(self.horizons) != {"event", "tactical", "medium_term", "structural"}:
            raise ValueError("horizons must define event, tactical, medium_term, and structural exactly.")
        return self


class AutonomousEligibilityConfig(BaseModel):
    """Minimum evidence required before autonomous live execution is eligible."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    shadow_trading_days: int = Field(default=60, ge=60)
    executable_shadow_plans: int = Field(default=30, ge=30)
    approved_paper_executions: int = Field(default=30, ge=30)
    approval_required_live_executions: int = Field(default=30, ge=30)
    maximum_critical_control_failures: int = Field(default=0, ge=0, le=0)


class ExecutionConfig(BaseModel):
    """Versioned broker environment, authority, and execution limits."""

    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = Field(min_length=1)
    broker_environment: BrokerEnvironment
    execution_mode: ExecutionMode = ExecutionMode.APPROVAL_REQUIRED
    maximum_order_notional: float = Field(gt=0)
    maximum_daily_turnover: float = Field(ge=0, le=1)
    maximum_market_drift_fraction: float = Field(ge=0, lt=1)
    maximum_quote_age_seconds: float = Field(default=300, gt=0)
    poll_interval_seconds: float = Field(gt=0)
    poll_timeout_seconds: float = Field(gt=0)
    allowed_asset_classes: tuple[TradableAssetClass, ...] = (
        TradableAssetClass.US_EQUITY,
        TradableAssetClass.US_ETF,
    )
    autonomous_eligibility: AutonomousEligibilityConfig = Field(default_factory=AutonomousEligibilityConfig)


@dataclass(frozen=True)
class ApplicationConfig:
    """Complete configuration assembled from secrets and three typed documents."""

    environment: Config
    sources: SourceRegistryDocument
    intelligence: StrategyIntelligenceConfig
    strategy: StrategyConfig | None
    execution: ExecutionConfig | None

    def require_strategy(self) -> StrategyConfig:
        """Return capital strategy or fail at the capability boundary."""
        if self.strategy is None:
            raise ConfigurationValidationError("Portfolio stages require complete capital strategy configuration.")
        return self.strategy

    def require_execution(self) -> ExecutionConfig:
        """Return execution authority or fail at the A6 boundary."""
        if self.execution is None:
            raise ConfigurationValidationError("A6 requires complete execution configuration.")
        return self.execution


class ConfigurationScope(StrEnum):
    """Configuration documents required by an application responsibility."""

    INTELLIGENCE = "intelligence"
    CAPITAL = "capital"
    EXECUTION = "execution"


def _load_toml(path: Path) -> object:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigurationFileError(f"Could not load configuration document {path}.") from error


ModelT = TypeVar("ModelT", bound=BaseModel)


def _load_model(path: Path, model_type: type[ModelT]) -> ModelT:
    """Load and validate one required TOML document."""
    try:
        return model_type.model_validate(_load_toml(path))
    except ValidationError as error:
        raise ConfigurationValidationError(f"Configuration document {path} is invalid.") from error


def load_config(path: Path | None = None) -> Config:
    """Load a fresh secrets-only environment configuration."""
    resolved_path: Path = path or default_config_path()
    _ = load_dotenv(resolved_path)
    return Config()


def load_application_config(
    *,
    environment_path: Path | None = None,
    sources_path: Path | None = None,
    strategy_path: Path | None = None,
    execution_path: Path | None = None,
    scope: ConfigurationScope = ConfigurationScope.EXECUTION,
) -> ApplicationConfig:
    """Load only configuration required by the requested responsibility."""
    strategy_document = _load_toml(strategy_path or default_strategy_config_path())
    return ApplicationConfig(
        environment=load_config(environment_path),
        sources=_load_model(sources_path or default_sources_config_path(), SourceRegistryDocument),
        intelligence=StrategyIntelligenceConfig.model_validate(strategy_document),
        strategy=(
            None if scope is ConfigurationScope.INTELLIGENCE else StrategyConfig.model_validate(strategy_document)
        ),
        execution=(
            _load_model(execution_path or default_execution_config_path(), ExecutionConfig)
            if scope is ConfigurationScope.EXECUTION
            else None
        ),
    )


def canonical_config_hash(config: BaseModel) -> str:
    """Return a stable digest of one validated configuration document."""
    encoded: bytes = json.dumps(
        config.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def claim_refresh_policy(strategy: StrategyConfig | StrategyIntelligenceConfig) -> "ClaimRefreshPolicy":
    """Build runtime claim freshness from validated strategy configuration."""
    from money_pit.claims.projection import ClaimFreshnessRule
    from money_pit.claims.projection import ClaimRefreshPolicy

    return ClaimRefreshPolicy(
        policy_version=strategy.claim_freshness.version,
        rules={
            category: {
                horizon: ClaimFreshnessRule(
                    review_interval=timedelta(days=rule.review_interval_days),
                    freshness_interval=timedelta(days=rule.freshness_days),
                )
                for horizon, rule in horizons.items()
            }
            for category, horizons in strategy.claim_freshness.rules.items()
        },
    )


def _require_non_blank_keyring_field(field: str, value: str | None, credential: str) -> str:
    if value is None or not value.strip():
        raise CredentialResolutionError(_BLANK_KEYRING_FIELD_MESSAGE.format(field=field, credential=credential))
    return value


def resolve_alpaca_credentials(config: Config, broker_environment: BrokerEnvironment) -> AlpacaCredentials:
    """Resolve Alpaca credentials only when the execution boundary requests them."""
    service: str = _require_non_blank_keyring_field("alpaca_service", config.alpaca_service, "Alpaca credentials")
    username: str = _require_non_blank_keyring_field("alpaca_username", config.alpaca_username, "Alpaca credentials")
    secret_key: str | None = keyring.get_password(service, username)
    if secret_key is None:
        raise CredentialResolutionError(f"No Alpaca secret in keyring for service {service!r}, username {username!r}.")
    return AlpacaCredentials(api_key=username, secret_key=SecretStr(secret_key), broker_environment=broker_environment)
