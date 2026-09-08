"""The deterministic fallback advisor."""

from __future__ import annotations

import math
from datetime import date, timedelta

from t212bot.indicators import compute_all
from t212bot.local_strategy import advise_locally
from t212bot.market_data import Snapshot
from t212bot.models import Bar, PriceHistory, dec

from conftest import OTHER, TICKER, make_account, make_config, make_position, make_quote


def _bars(fn, n=60):
    start = date(2026, 1, 1)
    return tuple(Bar(day=start + timedelta(days=i), close=dec(fn(i))) for i in range(n))


def _snapshot(prices, series):
    quotes = {t: make_quote(t, p) for t, p in prices.items()}
    histories = {t: PriceHistory(ticker=t, bars=series.get(t, _bars(lambda i: 10))) for t in prices}
    return Snapshot(quotes=quotes, histories=histories, errors={})


def _signals(config, snap):
    return compute_all(config.watchlist, snap.quotes, snap.histories, config.ai.indicators)


def _advise(config, account, snap, regime="neutral", trades_today=0):
    return advise_locally(
        config, account, snap, _signals(config, snap), regime,
        trades_today=trades_today, day_pnl=dec(0),
    )


def test_a_deep_loser_triggers_a_stop_loss_exit():
    config = make_config()
    account = make_account(
        cash=20, positions=[make_position(TICKER, quantity=2, average_price=10, current_price=8)]
    )
    snap = _snapshot({TICKER: 8, OTHER: 7}, {})
    result = _advise(config, account, snap)
    assert result.provider == "local"
    assert result.http_calls == 0
    assert result.proposal.action == "sell"
    assert result.proposal.ticker == TICKER
    assert "Stop-loss" in result.proposal.reasoning


def test_a_big_winner_is_trimmed_by_take_profit():
    config = make_config()
    account = make_account(
        cash=5, positions=[make_position(TICKER, quantity=3, average_price=10, current_price=12)]
    )
    snap = _snapshot({TICKER: 12, OTHER: 7}, {})
    result = _advise(config, account, snap)
    assert result.proposal.action == "sell"
    # half of 3 * £12 = £18 -> trim ~£18
    assert result.proposal.notional is not None and result.proposal.notional > dec(15)


def test_an_entry_needs_a_bullish_low_vol_name_and_a_calm_regime():
    config = make_config()
    trend = lambda i: 8 + i * 0.02 + math.sin(i / 2) * 0.3
    rising = {TICKER: _bars(trend), OTHER: _bars(lambda i: 7)}
    snap = _snapshot({TICKER: 9.1, OTHER: 7}, rising)
    account = make_account(cash=50)

    entry = _advise(config, account, snap, regime="risk_on")
    assert entry.proposal.action == "buy"
    assert entry.proposal.ticker == TICKER

    blocked = _advise(config, account, snap, regime="defensive")
    assert blocked.proposal.action == "hold"


def test_nothing_qualifying_is_a_hold():
    config = make_config()
    snap = _snapshot({TICKER: 10, OTHER: 7}, {})
    result = _advise(config, make_account(cash=50), snap)
    assert result.proposal.action == "hold"


def test_it_never_proposes_selling_something_not_held():
    config = make_config()
    falling = {TICKER: _bars(lambda i: 30 - i * 0.3)}
    snap = _snapshot({TICKER: 12}, falling)
    result = _advise(config, make_account(cash=50), snap)
    # bearish, but we hold nothing -> cannot be a sell
    assert result.proposal.action != "sell"


def test_the_frequency_cap_blocks_a_fallback_entry():
    config = make_config(max_trades_per_day=1)
    rising = {TICKER: _bars(lambda i: 8 + i * 0.05), OTHER: _bars(lambda i: 7)}
    snap = _snapshot({TICKER: 11, OTHER: 7}, rising)
    result = _advise(config, make_account(cash=50), snap, regime="risk_on", trades_today=1)
    assert result.proposal.action == "hold"


def test_the_raw_response_round_trips_through_the_shared_parser():
    config = make_config()
    account = make_account(
        cash=20, positions=[make_position(TICKER, quantity=2, average_price=10, current_price=8)]
    )
    snap = _snapshot({TICKER: 8, OTHER: 7}, {})
    result = _advise(config, account, snap)
    assert result.raw_response.startswith("{")
    assert result.proposal.action == "sell"  # parsed back out of that JSON


def test_an_entry_allows_stacking_when_under_position_cap():
    config = make_config(max_position_pct=25, max_capital=400, per_trade_cap_pct=2.5, min_order=5)
    trend = lambda i: 8 + i * 0.02 + math.sin(i / 2) * 0.3
    rising = {TICKER: _bars(trend), OTHER: _bars(lambda i: 7)}
    snap = _snapshot({TICKER: 9.1, OTHER: 7}, rising)
    # Already hold £10 of TICKER (under £100 max position cap)
    account = make_account(
        cash=100,
        positions=[make_position(TICKER, quantity=dec("1.0989"), average_price=dec("9.1"), current_price=dec("9.1"))]
    )

    result = _advise(config, account, snap, regime="risk_on")
    assert result.proposal.action == "buy"
    assert result.proposal.ticker == TICKER
    assert result.proposal.notional == dec(10)  # 2.5% of 400


def test_an_entry_is_blocked_when_position_cap_is_reached():
    config = make_config(max_position_pct=25, max_capital=400, per_trade_cap_pct=2.5, min_order=5)
    trend = lambda i: 8 + i * 0.02 + math.sin(i / 2) * 0.3
    rising = {TICKER: _bars(trend), OTHER: _bars(lambda i: 7)}
    snap = _snapshot({TICKER: 9.1, OTHER: 7}, rising)
    # Already hold £100 of TICKER (at £100 max position cap)
    account = make_account(
        cash=100,
        positions=[make_position(TICKER, quantity=dec("10.989"), average_price=dec("9.1"), current_price=dec("9.1"))]
    )

    result = _advise(config, account, snap, regime="risk_on")
    assert result.proposal.action == "hold"

