"""Builders for test fixtures.

Every helper takes plain numbers and converts them, so a test reads as the
scenario it describes rather than as Decimal noise.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Iterable, Mapping

import pytest

from t212bot.config import (
    AIConfig,
    AppConfig,
    CapitalConfig,
    DashboardConfig,
    ExecutionConfig,
    LoggingConfig,
    MarketDataConfig,
    RiskConfig,
    ScheduleConfig,
    Secrets,
    StorageConfig,
)
from t212bot.models import (
    AccountState,
    Position,
    Proposal,
    Quote,
    RiskInputs,
    WatchItem,
    dec,
    utcnow,
)

TICKER = "VUSAl_EQ"
OTHER = "ISFl_EQ"


def make_config(
    *,
    mode: str = "paper",
    max_capital: str | float = 50,
    per_trade_cap_pct: str | float = 25,
    max_position_pct: str | float = 25,
    min_order: str | float = 1,
    cash_buffer: str | float = 0,
    daily_loss_limit_pct: str | float = 10,
    max_trades_per_day: int = 5,
    max_price_deviation_pct: str | float = 2,
    max_quote_age_seconds: int = 900,
    min_confidence: str | float = "0.6",
    order_type: str = "market",
    quantity_decimals: int = 6,
    fractional: bool = True,
    paper_slippage_bps: str | float = 0,
    watchlist: Iterable[WatchItem] | None = None,
) -> AppConfig:
    return AppConfig(
        mode=mode,
        capital=CapitalConfig(
            max_capital=dec(max_capital),
            per_trade_cap_pct=dec(per_trade_cap_pct),
            max_position_pct=dec(max_position_pct),
            min_order=dec(min_order),
            cash_buffer=dec(cash_buffer),
        ),
        risk=RiskConfig(
            daily_loss_limit_pct=dec(daily_loss_limit_pct),
            max_trades_per_day=max_trades_per_day,
            max_price_deviation_pct=dec(max_price_deviation_pct),
            max_quote_age_seconds=max_quote_age_seconds,
            min_confidence=dec(min_confidence),
        ),
        execution=ExecutionConfig(
            order_type=order_type,
            limit_offset_bps=dec(25),
            paper_slippage_bps=dec(paper_slippage_bps),
            quantity_decimals=quantity_decimals,
            fractional=fractional,
            max_decision_age_seconds=120,
        ),
        schedule=ScheduleConfig(
            cron="0,30 8-16 * * mon-fri",
            timezone="Europe/London",
            market_open="08:05",
            market_close="16:25",
            trading_days=("mon", "tue", "wed", "thu", "fri"),
        ),
        ai=AIConfig(
            provider="stub",
            timeout_seconds=60,
            max_tokens=1024,
            openrouter_base_url="https://openrouter.ai/api/v1",
            openrouter_model="openrouter/free",
            anthropic_model="claude-haiku-4-5",
            history_days=30,
        ),
        market_data=MarketDataConfig(provider="yahoo", timeout_seconds=20, cache_seconds=60),
        storage=StorageConfig(db_path=Path(":memory:")),
        logging=LoggingConfig(level="INFO", file=None),
        dashboard=DashboardConfig(host="127.0.0.1", port=8080),
        stop_file=Path("/tmp/t212bot-test-stop-does-not-exist"),
        watchlist=tuple(
            watchlist
            or (
                WatchItem(ticker=TICKER, yahoo="VUSA.L", name="Vanguard S&P 500"),
                WatchItem(ticker=OTHER, yahoo="ISF.L", name="iShares FTSE 100"),
            )
        ),
        t212_base_url="https://demo.trading212.com/api/v0",
        t212_auth_scheme="basic",
        secrets=Secrets(),
    )


def make_position(ticker: str = TICKER, quantity=1, average_price=10, current_price=10) -> Position:
    return Position(
        ticker=ticker,
        quantity=dec(quantity),
        average_price=dec(average_price),
        current_price=dec(current_price),
    )


def make_account(cash=50, positions: Iterable[Position] = (), source: str = "paper") -> AccountState:
    return AccountState(
        cash=dec(cash), positions=tuple(positions), as_of=utcnow(), source=source
    )


def make_quote(ticker: str = TICKER, price=10, age_seconds: int = 0) -> Quote:
    return Quote(
        ticker=ticker,
        price=dec(price),
        currency="GBP",
        as_of=utcnow() - timedelta(seconds=age_seconds),
        source="test",
    )


def make_inputs(
    *,
    account: AccountState | None = None,
    quotes: Mapping[str, Quote] | None = None,
    decision_id: str = "decision-1",
    trades_today: int = 0,
    day_start_equity=50,
    breaker_tripped: bool = False,
    known_decision_ids: Iterable[str] = (),
    unresolved_orders: int = 0,
) -> RiskInputs:
    return RiskInputs(
        decision_id=decision_id,
        account=account if account is not None else make_account(),
        quotes=dict(quotes) if quotes is not None else {TICKER: make_quote()},
        trades_today=trades_today,
        day_start_equity=dec(day_start_equity),
        breaker_tripped=breaker_tripped,
        known_decision_ids=frozenset(known_decision_ids),
        unresolved_orders=unresolved_orders,
        now=utcnow(),
    )


def buy(ticker: str = TICKER, notional=10, confidence="0.9", **kwargs) -> Proposal:
    return Proposal(
        action="buy",
        ticker=ticker,
        notional=None if notional is None else dec(notional),
        confidence=dec(confidence),
        reasoning="test",
        **kwargs,
    )


def sell(ticker: str = TICKER, quantity=1, confidence="0.9", **kwargs) -> Proposal:
    return Proposal(
        action="sell",
        ticker=ticker,
        quantity=None if quantity is None else dec(quantity),
        confidence=dec(confidence),
        reasoning="test",
        **kwargs,
    )


@pytest.fixture
def config() -> AppConfig:
    return make_config()


@pytest.fixture
def storage(tmp_path):
    from t212bot.storage import Storage

    store = Storage(tmp_path / "test.sqlite3")
    yield store
    store.close()
