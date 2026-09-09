"""Config and secret loading.

Two sources, deliberately kept apart:
  * config.yaml — behaviour and limits. Safe to read, log and version.
  * .env        — secrets and MODE. Loaded via python-dotenv, never logged.

``load()`` returns an ``AppConfig`` and raises ``ConfigError`` on anything
questionable. It is strict on purpose: a typo'd cap silently defaulting to
something generous is exactly the failure this project cannot afford.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

import yaml
from dotenv import load_dotenv

from .models import WatchItem, dec, maybe_dec

MODES = ("paper", "demo", "live")
LIVE_CONFIRMATION_VAR = "I_UNDERSTAND_THIS_IS_REAL_MONEY"
LIVE_CONFIRMATION_VALUE = "yes"

DEFAULT_DEMO_BASE_URL = "https://demo.trading212.com/api/v0"
DEFAULT_LIVE_BASE_URL = "https://live.trading212.com/api/v0"

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class ConfigError(Exception):
    """Configuration is missing, malformed, or unsafe."""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CapitalConfig:
    max_capital: Decimal
    per_trade_cap_pct: Decimal
    max_position_pct: Decimal
    min_order: Decimal
    cash_buffer: Decimal

    @property
    def per_trade_cap(self) -> Decimal:
        return self.max_capital * self.per_trade_cap_pct / Decimal(100)

    @property
    def max_position_value(self) -> Decimal:
        return self.max_capital * self.max_position_pct / Decimal(100)


@dataclass(frozen=True)
class RiskConfig:
    daily_loss_limit_pct: Decimal
    max_trades_per_day: int
    max_price_deviation_pct: Decimal
    # How long ago the quote may have been *fetched* by us. 0 or less disables
    # the check entirely.
    max_quote_age_seconds: int
    min_confidence: Decimal
    # When True the AI may only act on watch-list tickers. When False it may
    # name any Trading212 instrument (resolved against data/instruments.json and
    # priced in GBP on demand); an unknown ticker is still rejected.
    enforce_allowlist: bool = True
    # How far behind the market the quote's own exchange timestamp may be.
    # Free feeds are delayed ~15 minutes as a matter of course, which says
    # nothing about whether our copy is current, so this is disabled (0) by
    # default. Set it to e.g. 900 to refuse to trade on a delayed feed.
    max_quote_delay_seconds: int = 0

    def daily_loss_limit(self, max_capital: Decimal) -> Decimal:
        """The P&L level (negative) at or below which trading halts."""
        return -(max_capital * self.daily_loss_limit_pct / Decimal(100))


@dataclass(frozen=True)
class ExecutionConfig:
    order_type: str
    limit_offset_bps: Decimal
    paper_slippage_bps: Decimal
    quantity_decimals: int
    fractional: bool
    max_decision_age_seconds: int


@dataclass(frozen=True)
class ScheduleConfig:
    cron: str
    timezone: str
    market_open: str
    market_close: str
    trading_days: tuple[str, ...]


@dataclass(frozen=True)
class IndicatorConfig:
    """Windows for the local technical analysis. Bars are daily closes."""

    sma_short: int = 10
    sma_mid: int = 20
    sma_long: int = 50
    rsi_period: int = 14
    min_bars: int = 30
    lookback_days: int = 60

    @property
    def bars_needed(self) -> int:
        return max(self.sma_long, self.rsi_period + 1, self.min_bars)


@dataclass(frozen=True)
class LocalStrategyConfig:
    """The deterministic fallback advisor's thresholds, all in percent."""

    stop_loss_pct: Decimal = Decimal(8)
    trend_exit: bool = True
    take_profit_pct: Decimal = Decimal(15)
    max_entry_vol_pct: Decimal = Decimal(35)
    min_entry_score: Decimal = Decimal("0.35")


@dataclass(frozen=True)
class AIConfig:
    provider: str
    timeout_seconds: int
    max_tokens: int
    openrouter_base_url: str
    openrouter_model: str
    anthropic_model: str
    history_days: int
    # Fallback chain of OpenRouter model slugs, tried in order until one
    # answers. The single ``openrouter_model`` above is kept for compatibility
    # and is always the first entry unless a list is given.
    openrouter_models: tuple[str, ...] = ("openrouter/free",)
    # OmniRoute: a self-hosted/private OpenAI-compatible router, same wire
    # format as OpenRouter. base_url is required when ai.provider is
    # "omniroute" (e.g. http://127.0.0.1:20128/v1 or https://omni.example/v1).
    omniroute_base_url: str = ""
    omniroute_model: str = ""
    omniroute_models: tuple[str, ...] = ()
    # Stop calling OpenRouter once this many HTTP calls have been made today
    # (UTC). The free tier is ~50/day; 45 leaves headroom. Past this the local
    # strategy takes over.
    daily_request_budget: int = 45
    # When the LLM fails or the budget is spent, fall back to the local
    # rule-based advisor rather than degrading straight to "hold".
    local_fallback: bool = True
    # Skip the LLM call entirely when nothing material changed since the last
    # cycle and that cycle held.
    skip_when_unchanged: bool = True
    # A price move smaller than this (percent) does not count as "material".
    min_price_move_pct: Decimal = Decimal("0.5")
    indicators: IndicatorConfig = field(default_factory=IndicatorConfig)
    local_strategy: LocalStrategyConfig = field(default_factory=LocalStrategyConfig)


@dataclass(frozen=True)
class MarketDataConfig:
    provider: str
    timeout_seconds: int
    cache_seconds: int
    # Cache lifetime for a fetched GBP FX rate (open-universe mode only).
    fx_cache_seconds: int = 900
    # Allow the Yahoo ISIN search when resolving a symbol for an off-list
    # instrument. Turn off to rely solely on overrides + the derived symbol.
    symbol_search: bool = True


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: Path | None


@dataclass(frozen=True)
class DashboardConfig:
    host: str
    port: int


@dataclass(frozen=True)
class Secrets:
    """Never rendered, never logged, never stored."""

    t212_api_key: str = ""
    t212_api_secret: str = ""
    openrouter_api_key: str = ""
    omniroute_api_key: str = ""
    anthropic_api_key: str = ""

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return "Secrets(<redacted>)"


@dataclass(frozen=True)
class AppConfig:
    mode: str
    capital: CapitalConfig
    risk: RiskConfig
    execution: ExecutionConfig
    schedule: ScheduleConfig
    ai: AIConfig
    market_data: MarketDataConfig
    storage: StorageConfig
    logging: LoggingConfig
    dashboard: DashboardConfig
    stop_file: Path
    watchlist: tuple[WatchItem, ...]
    t212_base_url: str
    t212_auth_scheme: str
    secrets: Secrets = field(default=Secrets(), repr=False)
    # T212 ticker or ISIN -> Yahoo Finance symbol, to pin symbol resolution for
    # instruments where the automatic lookup is wrong or ambiguous.
    symbol_overrides: Mapping[str, str] = field(default_factory=dict)
    instruments_path: Path = Path("data/instruments.json")

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def max_history_days(self) -> int:
        """Calendar days of history to request.

        Yahoo's ``range`` is in calendar days but only trading days come back
        (~5 in 7), so the trading-bar requirement is inflated to leave enough
        for the long SMA plus a margin.
        """
        bars = max(self.ai.history_days, self.ai.indicators.bars_needed)
        return int(bars * 1.5) + 15

    @property
    def places_real_orders(self) -> bool:
        """paper mode never calls an order endpoint; demo and live do."""
        return self.mode in ("demo", "live")

    @property
    def allowed_tickers(self) -> frozenset[str]:
        return frozenset(item.ticker for item in self.watchlist)

    def watch_item(self, ticker: str) -> WatchItem | None:
        for item in self.watchlist:
            if item.ticker == ticker:
                return item
        return None

    def position_cap_for(self, ticker: str) -> Decimal:
        """Per-ticker position cap in GBP, honouring any per-item override."""
        item = self.watch_item(ticker)
        if item is not None and item.max_position_pct is not None:
            return self.capital.max_capital * item.max_position_pct / Decimal(100)
        return self.capital.max_position_value


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #


def _section(data: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"config section '{name}' must be a mapping, got {type(value).__name__}")
    return value


def _req(section: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in section or section[key] is None:
        raise ConfigError(f"missing required config key: {where}.{key}")
    return section[key]


def _dec(section: Mapping[str, Any], key: str, where: str, default: Any = None) -> Decimal:
    raw = section.get(key, default)
    if raw is None:
        raise ConfigError(f"missing required config key: {where}.{key}")
    try:
        return dec(raw)
    except ValueError as exc:
        raise ConfigError(f"{where}.{key}: {exc}") from exc


def _int(section: Mapping[str, Any], key: str, where: str, default: Any = None) -> int:
    raw = section.get(key, default)
    if raw is None:
        raise ConfigError(f"missing required config key: {where}.{key}")
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where}.{key} must be an integer, got {raw!r}") from exc


def _positive(value: Decimal, where: str) -> Decimal:
    if value <= 0:
        raise ConfigError(f"{where} must be greater than zero (got {value})")
    return value


def _pct(value: Decimal, where: str, *, upper: Decimal = Decimal(100)) -> Decimal:
    if value <= 0 or value > upper:
        raise ConfigError(f"{where} must be a percentage in (0, {upper}] (got {value})")
    return value


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #


def load_watchlist(raw: Any) -> tuple[WatchItem, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("watchlist must be a non-empty list — the bot has nothing it may trade")

    items: list[WatchItem] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        where = f"watchlist[{index}]"
        if not isinstance(entry, Mapping):
            raise ConfigError(f"{where} must be a mapping")
        ticker = str(_req(entry, "ticker", where)).strip()
        yahoo = str(_req(entry, "yahoo", where)).strip()
        if not ticker or not yahoo:
            raise ConfigError(f"{where}: ticker and yahoo must both be non-empty")
        if ticker in seen:
            raise ConfigError(f"{where}: duplicate ticker {ticker!r} in watchlist")
        seen.add(ticker)

        override = maybe_dec(entry.get("max_position_pct"))
        if override is not None:
            _pct(override, f"{where}.max_position_pct")

        items.append(
            WatchItem(
                ticker=ticker,
                yahoo=yahoo,
                name=str(entry.get("name") or ticker),
                max_position_pct=override,
            )
        )
    return tuple(items)


def _load_secrets() -> Secrets:
    return Secrets(
        t212_api_key=os.getenv("T212_API_KEY", "").strip(),
        t212_api_secret=os.getenv("T212_API_SECRET", "").strip(),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
        omniroute_api_key=os.getenv("OMNIROUTE_API_KEY", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
    )


def resolve_mode() -> str:
    """Read MODE from the environment and enforce the live-mode confirmation.

    Default is paper. Live mode additionally requires the exact string "yes" in
    I_UNDERSTAND_THIS_IS_REAL_MONEY — this is the only place that check lives.
    """
    mode = os.getenv("MODE", "paper").strip().lower()
    if mode not in MODES:
        raise ConfigError(f"MODE must be one of {', '.join(MODES)} (got {mode!r})")

    if mode == "live":
        confirmation = os.getenv(LIVE_CONFIRMATION_VAR, "").strip().lower()
        if confirmation != LIVE_CONFIRMATION_VALUE:
            raise ConfigError(
                "MODE=live refuses to start without "
                f"{LIVE_CONFIRMATION_VAR}={LIVE_CONFIRMATION_VALUE} in the environment. "
                "This is real money."
            )
    return mode


def resolve_provider(configured: str, secrets: Secrets) -> str:
    """Pick the AI provider, honouring 'auto'."""
    provider = (configured or "auto").strip().lower()
    if provider not in ("auto", "openrouter", "omniroute", "anthropic", "stub"):
        raise ConfigError(
            f"ai.provider must be auto|openrouter|omniroute|anthropic|stub (got {provider!r})"
        )

    if provider == "auto":
        # OmniRoute first: self-hosted and unmetered, so it is not subject to
        # the OpenRouter free-tier daily budget — the better choice whenever
        # the bot needs to call the model on every cycle.
        if secrets.omniroute_api_key:
            return "omniroute"
        if secrets.anthropic_api_key:
            return "anthropic"
        if secrets.openrouter_api_key:
            return "openrouter"
        raise ConfigError(
            "ai.provider is 'auto' but none of ANTHROPIC_API_KEY, OPENROUTER_API_KEY, or "
            "OMNIROUTE_API_KEY is set. Set one in .env, or set ai.provider: stub to run the "
            "pipeline without an LLM."
        )

    if provider == "anthropic" and not secrets.anthropic_api_key:
        raise ConfigError("ai.provider is 'anthropic' but ANTHROPIC_API_KEY is not set")
    if provider == "openrouter" and not secrets.openrouter_api_key:
        raise ConfigError("ai.provider is 'openrouter' but OPENROUTER_API_KEY is not set")
    if provider == "omniroute" and not secrets.omniroute_api_key:
        raise ConfigError("ai.provider is 'omniroute' but OMNIROUTE_API_KEY is not set")
    return provider


def load(path: str | Path | None = None, *, env_file: str | Path | None = ".env") -> AppConfig:
    """Load .env then config.yaml into a validated AppConfig."""
    if env_file is not None and Path(env_file).exists():
        load_dotenv(env_file, override=False)

    config_path = Path(path or os.getenv("T212BOT_CONFIG", "config.yaml"))
    if not config_path.exists():
        raise ConfigError(
            f"config file not found: {config_path} — copy config.yaml.example to config.yaml"
        )

    with config_path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, Mapping):
        raise ConfigError(f"{config_path} must contain a YAML mapping at the top level")

    mode = resolve_mode()
    secrets = _load_secrets()

    cap_raw = _section(data, "capital")
    capital = CapitalConfig(
        max_capital=_positive(_dec(cap_raw, "max_capital_gbp", "capital"), "capital.max_capital_gbp"),
        per_trade_cap_pct=_pct(
            _dec(cap_raw, "per_trade_cap_pct", "capital", 25), "capital.per_trade_cap_pct"
        ),
        max_position_pct=_pct(
            _dec(cap_raw, "max_position_pct", "capital", 25), "capital.max_position_pct"
        ),
        min_order=_positive(_dec(cap_raw, "min_order_gbp", "capital", 1), "capital.min_order_gbp"),
        cash_buffer=_dec(cap_raw, "cash_buffer_gbp", "capital", 0),
    )
    if capital.cash_buffer < 0:
        raise ConfigError("capital.cash_buffer_gbp must not be negative")
    if capital.min_order > capital.per_trade_cap:
        raise ConfigError(
            f"capital.min_order_gbp ({capital.min_order}) exceeds the per-trade cap "
            f"({capital.per_trade_cap}) — no order could ever be approved"
        )

    risk_raw = _section(data, "risk")
    max_trades = _int(risk_raw, "max_trades_per_day", "risk", 5)
    if max_trades < 0:
        raise ConfigError("risk.max_trades_per_day must not be negative")
    risk = RiskConfig(
        daily_loss_limit_pct=_pct(
            _dec(risk_raw, "daily_loss_limit_pct", "risk", 10), "risk.daily_loss_limit_pct"
        ),
        max_trades_per_day=max_trades,
        max_price_deviation_pct=_pct(
            _dec(risk_raw, "max_price_deviation_pct", "risk", 2), "risk.max_price_deviation_pct"
        ),
        max_quote_age_seconds=_int(risk_raw, "max_quote_age_seconds", "risk", 900),
        min_confidence=_dec(risk_raw, "min_confidence", "risk", "0.6"),
        enforce_allowlist=bool(risk_raw.get("enforce_allowlist", True)),
        max_quote_delay_seconds=_int(risk_raw, "max_quote_delay_seconds", "risk", 0),
    )
    if not (Decimal(0) <= risk.min_confidence <= Decimal(1)):
        raise ConfigError("risk.min_confidence must be between 0 and 1")

    exec_raw = _section(data, "execution")
    order_type = str(exec_raw.get("order_type", "market")).strip().lower()
    if order_type not in ("market", "limit"):
        raise ConfigError(f"execution.order_type must be market|limit (got {order_type!r})")
    quantity_decimals = _int(exec_raw, "quantity_decimals", "execution", 6)
    if not (0 <= quantity_decimals <= 12):
        raise ConfigError("execution.quantity_decimals must be between 0 and 12")
    execution = ExecutionConfig(
        order_type=order_type,
        limit_offset_bps=_dec(exec_raw, "limit_offset_bps", "execution", 25),
        paper_slippage_bps=_dec(exec_raw, "paper_slippage_bps", "execution", 10),
        quantity_decimals=quantity_decimals,
        fractional=bool(exec_raw.get("fractional", True)),
        max_decision_age_seconds=_int(exec_raw, "max_decision_age_seconds", "execution", 120),
    )

    sched_raw = _section(data, "schedule")
    days = sched_raw.get("trading_days") or ["mon", "tue", "wed", "thu", "fri"]
    if not isinstance(days, list) or not days:
        raise ConfigError("schedule.trading_days must be a non-empty list")
    trading_days = tuple(str(d).strip().lower()[:3] for d in days)
    for day in trading_days:
        if day not in _WEEKDAYS:
            raise ConfigError(f"schedule.trading_days: unknown day {day!r}")
    schedule = ScheduleConfig(
        cron=str(sched_raw.get("cron", "0,30 8-16 * * mon-fri")),
        timezone=str(sched_raw.get("timezone", "Europe/London")),
        market_open=str(sched_raw.get("market_open", "08:05")),
        market_close=str(sched_raw.get("market_close", "16:25")),
        trading_days=trading_days,
    )

    ai_raw = _section(data, "ai")
    openrouter_raw = _section(ai_raw, "openrouter")
    omniroute_raw = _section(ai_raw, "omniroute")
    anthropic_raw = _section(ai_raw, "anthropic")
    ind_raw = _section(ai_raw, "indicators")
    strat_raw = _section(ai_raw, "local_strategy")

    openrouter_model = str(openrouter_raw.get("model", "openrouter/free"))
    models_raw = openrouter_raw.get("models")
    if models_raw is None:
        openrouter_models: tuple[str, ...] = (openrouter_model,)
    elif isinstance(models_raw, list) and models_raw:
        openrouter_models = tuple(str(m).strip() for m in models_raw if str(m).strip())
    else:
        raise ConfigError("ai.openrouter.models must be a non-empty list of model slugs")
    if not openrouter_models:
        raise ConfigError("ai.openrouter.models resolved to an empty list")

    omniroute_model = str(omniroute_raw.get("model", "")).strip()
    omniroute_models_raw = omniroute_raw.get("models")
    if omniroute_models_raw is None:
        omniroute_models: tuple[str, ...] = (omniroute_model,) if omniroute_model else ()
    elif isinstance(omniroute_models_raw, list) and omniroute_models_raw:
        omniroute_models = tuple(str(m).strip() for m in omniroute_models_raw if str(m).strip())
    else:
        raise ConfigError("ai.omniroute.models must be a non-empty list of model slugs")

    indicators = IndicatorConfig(
        sma_short=_int(ind_raw, "sma_short", "ai.indicators", 10),
        sma_mid=_int(ind_raw, "sma_mid", "ai.indicators", 20),
        sma_long=_int(ind_raw, "sma_long", "ai.indicators", 50),
        rsi_period=_int(ind_raw, "rsi_period", "ai.indicators", 14),
        min_bars=_int(ind_raw, "min_bars", "ai.indicators", 30),
        lookback_days=_int(ind_raw, "lookback_days", "ai.indicators", 60),
    )
    if not (0 < indicators.sma_short < indicators.sma_mid < indicators.sma_long):
        raise ConfigError(
            "ai.indicators SMA windows must be increasing and positive "
            f"(got {indicators.sma_short}/{indicators.sma_mid}/{indicators.sma_long})"
        )
    if indicators.rsi_period < 2:
        raise ConfigError("ai.indicators.rsi_period must be at least 2")

    local_strategy = LocalStrategyConfig(
        stop_loss_pct=_pct(_dec(strat_raw, "stop_loss_pct", "ai.local_strategy", 8),
                           "ai.local_strategy.stop_loss_pct"),
        trend_exit=bool(strat_raw.get("trend_exit", True)),
        take_profit_pct=_pct(_dec(strat_raw, "take_profit_pct", "ai.local_strategy", 15),
                             "ai.local_strategy.take_profit_pct", upper=Decimal(1000)),
        max_entry_vol_pct=_pct(_dec(strat_raw, "max_entry_vol_pct", "ai.local_strategy", 35),
                               "ai.local_strategy.max_entry_vol_pct", upper=Decimal(1000)),
        min_entry_score=_dec(strat_raw, "min_entry_score", "ai.local_strategy", "0.35"),
    )
    if not (Decimal(0) < local_strategy.min_entry_score <= Decimal(1)):
        raise ConfigError("ai.local_strategy.min_entry_score must be in (0, 1]")

    ai = AIConfig(
        provider=resolve_provider(str(ai_raw.get("provider", "auto")), secrets),
        timeout_seconds=_int(ai_raw, "timeout_seconds", "ai", 60),
        # Reasoning models spend most of their budget before they emit a single
        # character of the answer; 1024 left the JSON truncated mid-field.
        max_tokens=_int(ai_raw, "max_tokens", "ai", 4096),
        openrouter_base_url=str(
            openrouter_raw.get("base_url", "https://openrouter.ai/api/v1")
        ).rstrip("/"),
        openrouter_model=openrouter_model,
        anthropic_model=str(anthropic_raw.get("model", "claude-haiku-4-5")),
        history_days=_int(ai_raw, "history_days", "ai", 30),
        openrouter_models=openrouter_models,
        omniroute_base_url=os.getenv(
            "OMNIROUTE_BASE_URL", str(omniroute_raw.get("base_url", ""))
        ).rstrip("/"),
        omniroute_model=omniroute_model,
        omniroute_models=omniroute_models,
        daily_request_budget=_int(ai_raw, "daily_request_budget", "ai", 45),
        local_fallback=bool(ai_raw.get("local_fallback", True)),
        skip_when_unchanged=bool(ai_raw.get("skip_when_unchanged", True)),
        min_price_move_pct=_dec(ai_raw, "min_price_move_pct", "ai", "0.5"),
        indicators=indicators,
        local_strategy=local_strategy,
    )
    if ai.daily_request_budget < 0:
        raise ConfigError("ai.daily_request_budget must not be negative")
    if ai.min_price_move_pct < 0:
        raise ConfigError("ai.min_price_move_pct must not be negative")
    if ai.provider == "omniroute":
        if not ai.omniroute_models:
            raise ConfigError(
                "ai.provider is 'omniroute' but ai.omniroute.model (or .models) is not set"
            )
        if not ai.omniroute_base_url:
            raise ConfigError("ai.provider is 'omniroute' but ai.omniroute.base_url is not set")

    md_raw = _section(data, "market_data")
    market_data = MarketDataConfig(
        provider=str(md_raw.get("provider", "yahoo")).strip().lower(),
        timeout_seconds=_int(md_raw, "timeout_seconds", "market_data", 20),
        cache_seconds=_int(md_raw, "cache_seconds", "market_data", 60),
        fx_cache_seconds=_int(md_raw, "fx_cache_seconds", "market_data", 900),
        symbol_search=bool(md_raw.get("symbol_search", True)),
    )

    overrides_raw = md_raw.get("symbol_overrides") or {}
    if not isinstance(overrides_raw, Mapping):
        raise ConfigError("market_data.symbol_overrides must be a mapping")
    symbol_overrides = {str(k).strip(): str(v).strip() for k, v in overrides_raw.items()}

    store_raw = _section(data, "storage")
    storage = StorageConfig(db_path=Path(str(store_raw.get("db_path", "./data/t212bot.sqlite3"))))
    instruments_path = Path(
        str(store_raw.get("instruments_path", storage.db_path.parent / "instruments.json"))
    )

    if not risk.enforce_allowlist and not instruments_path.exists():
        raise ConfigError(
            "risk.enforce_allowlist is false but the instrument catalogue "
            f"{instruments_path} is missing. Run it once:\n"
            "  python -m scripts.list_instruments --refresh"
        )

    log_raw = _section(data, "logging")
    log_file = log_raw.get("file")
    logging_cfg = LoggingConfig(
        level=str(log_raw.get("level", "INFO")).upper(),
        file=Path(str(log_file)) if log_file else None,
    )

    dash_raw = _section(data, "dashboard")
    dashboard = DashboardConfig(
        host=str(dash_raw.get("host", "127.0.0.1")),
        port=_int(dash_raw, "port", "dashboard", 8080),
    )

    stop_file = Path(str(_section(data, "kill_switch").get("stop_file", "./STOP")))
    watchlist = load_watchlist(data.get("watchlist"))

    auth_scheme = os.getenv("T212_AUTH_SCHEME", "basic").strip().lower()
    if auth_scheme not in ("basic", "header"):
        raise ConfigError(f"T212_AUTH_SCHEME must be basic|header (got {auth_scheme!r})")

    if mode == "live":
        base_url = os.getenv("T212_LIVE_BASE_URL", DEFAULT_LIVE_BASE_URL).rstrip("/")
    else:
        base_url = os.getenv("T212_DEMO_BASE_URL", DEFAULT_DEMO_BASE_URL).rstrip("/")

    if mode != "paper" and not secrets.t212_api_key:
        raise ConfigError(f"MODE={mode} requires T212_API_KEY in .env")

    return AppConfig(
        mode=mode,
        capital=capital,
        risk=risk,
        execution=execution,
        schedule=schedule,
        ai=ai,
        market_data=market_data,
        storage=storage,
        logging=logging_cfg,
        dashboard=dashboard,
        stop_file=stop_file,
        watchlist=watchlist,
        t212_base_url=base_url,
        t212_auth_scheme=auth_scheme,
        secrets=secrets,
        symbol_overrides=symbol_overrides,
        instruments_path=instruments_path,
    )
