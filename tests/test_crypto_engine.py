"""The crypto cycle: signal fetching, sizing, buying, selling, and the loop."""

from __future__ import annotations

import math
import time
from dataclasses import replace
from datetime import datetime, timezone

from t212bot import crypto_engine
from t212bot.config import CryptoConfig
from t212bot.crypto_client import CryptoError
from t212bot.crypto_engine import (
    CryptoRuntime,
    CryptoScheduler,
    _buy,
    _sell,
    fetch_signal,
    run_crypto_cycle,
)
from t212bot.models import dec

from conftest import make_config

BTC = "BTC/USDT"
ETH = "ETH/USDT"


class FakeClient:
    """Duck-types CcxtClient without ever touching a real exchange."""

    def __init__(self, ohlcv=None, balance=dec(0), order_response=None, raise_on=None):
        self.ohlcv = ohlcv or {}
        self.balance = balance
        self.order_response = order_response or {}
        self.raise_on = raise_on or {}
        self.orders: list[tuple] = []

    def fetch_ohlcv(self, symbol, timeframe, limit):
        if symbol in self.raise_on:
            raise self.raise_on[symbol]
        return self.ohlcv.get(symbol, [])

    def fetch_balance(self, currency):
        return self.balance

    def create_market_order(self, symbol, side, amount):
        self.orders.append((symbol, side, amount))
        if symbol in self.raise_on:
            raise self.raise_on[symbol]
        return self.order_response.get(symbol, {"id": "order-1", "average": None})

    def close(self):
        pass


def _candles(fn, n=60, base_price=60000):
    start = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    day_ms = 86_400_000
    return [
        [start + i * day_ms, base_price, base_price, base_price, base_price * fn(i), 1.0]
        for i in range(n)
    ]


def _bullish_candles(n=60):
    # A gentle rise with a wiggle — bullish, low-vol, RSI nowhere near overbought.
    return _candles(lambda i: 1 + i * 0.002 + math.sin(i / 2) * 0.01, n=n)


def _bearish_candles(n=60):
    return _candles(lambda i: 1 - i * 0.004 + math.sin(i / 2) * 0.01, n=n)


def _crypto_config(**overrides) -> CryptoConfig:
    base = dict(
        enabled=True, exchange="binance", mode="paper", pairs=(BTC,), strategy="trend",
        poll_interval_seconds=300, max_capital=dec(100), per_trade_cap_pct=dec(50),
        min_order=dec(10), max_positions=3, max_trades_per_day=10,
        stop_loss_pct=dec(8), take_profit_pct=dec(15), min_entry_score=dec("0.35"),
        max_entry_vol_pct=dec(80),
    )
    base.update(overrides)
    return CryptoConfig(**base)


def _runtime(storage, client, **crypto_overrides) -> CryptoRuntime:
    config = replace(make_config(), crypto=_crypto_config(**crypto_overrides))
    return CryptoRuntime(config=config, storage=storage, client=client)


# --------------------------------------------------------------------------- #
# fetch_signal
# --------------------------------------------------------------------------- #


def test_fetch_signal_builds_a_signal_from_ohlcv():
    client = FakeClient(ohlcv={BTC: _bullish_candles()})
    cfg = _crypto_config()
    signal, quote = fetch_signal(client, BTC, cfg)
    assert signal is not None
    assert quote.ticker == BTC
    assert quote.currency == "USDT"
    assert quote.price == signal.last


def test_fetch_signal_is_none_with_no_candles():
    client = FakeClient(ohlcv={BTC: []})
    signal, quote = fetch_signal(client, BTC, _crypto_config())
    assert signal is None
    assert quote is None


# --------------------------------------------------------------------------- #
# _buy
# --------------------------------------------------------------------------- #


def test_buy_opens_a_paper_position_and_spends_cash(storage):
    storage.seed_crypto_cash(dec(100))
    runtime = _runtime(storage, FakeClient())
    outcome = _buy(runtime, BTC, dec(100), "test entry")
    assert outcome["state"] == "filled"
    position = storage.crypto_position(BTC)
    assert position is not None
    quantity, avg_price, _ = position
    assert avg_price == dec(100)
    assert storage.crypto_cash() == dec(100) - quantity * dec(100)


def test_buy_is_capped_by_the_per_trade_cap(storage):
    storage.seed_crypto_cash(dec(1000))
    runtime = _runtime(storage, FakeClient(), max_capital=dec(1000), per_trade_cap_pct=dec(10))
    _buy(runtime, BTC, dec(100), "test")
    quantity, _, _ = storage.crypto_position(BTC)
    # 10% of 1000 = 100 GBP-equivalent, at price 100 -> 1 unit.
    assert quantity == dec(1)


def test_buy_skips_when_below_the_minimum_order(storage):
    storage.seed_crypto_cash(dec(5))
    runtime = _runtime(storage, FakeClient(), min_order=dec(10))
    outcome = _buy(runtime, BTC, dec(100), "test")
    assert outcome["action"] == "skipped"
    assert storage.crypto_position(BTC) is None


def test_buy_skips_once_max_positions_is_reached(storage):
    storage.seed_crypto_cash(dec(1000))
    runtime = _runtime(storage, FakeClient(), max_positions=1)
    storage.open_crypto_position(ETH, dec(1), dec(50))
    outcome = _buy(runtime, BTC, dec(100), "test")
    assert outcome["action"] == "skipped"
    assert "max concurrent positions" in outcome["reason"]


def test_buy_respects_the_remaining_capital_cap_across_positions(storage):
    storage.seed_crypto_cash(dec(1000))
    runtime = _runtime(storage, FakeClient(), max_capital=dec(150), max_positions=5)
    storage.open_crypto_position(ETH, dec(1), dec(100))  # 100 already deployed
    outcome = _buy(runtime, BTC, dec(100), "test")
    quantity, _, _ = storage.crypto_position(BTC)
    # Only 50 of headroom left under the 150 cap.
    assert quantity == dec("0.5")
    assert outcome["state"] == "filled"


def test_live_buy_places_a_real_order_and_uses_the_fill_price(storage):
    client = FakeClient(balance=dec(1000), order_response={BTC: {"id": "9", "average": "101.5"}})
    runtime = _runtime(storage, client, mode="live", max_capital=dec(1000), per_trade_cap_pct=dec(50))
    outcome = _buy(runtime, BTC, dec(100), "test")
    assert outcome["state"] == "filled"
    assert client.orders == [(BTC, "buy", dec(5))]
    _, avg_price, _ = storage.crypto_position(BTC)
    assert avg_price == dec("101.5")


def test_live_buy_records_an_error_and_opens_nothing_on_exchange_failure(storage):
    client = FakeClient(balance=dec(1000), raise_on={BTC: CryptoError("insufficient funds")})
    runtime = _runtime(storage, client, mode="live", max_capital=dec(1000), per_trade_cap_pct=dec(50))
    outcome = _buy(runtime, BTC, dec(100), "test")
    assert outcome["state"] == "error"
    assert storage.crypto_position(BTC) is None
    assert storage.recent_crypto_trades(1)[0]["error"] == "insufficient funds"


# --------------------------------------------------------------------------- #
# _sell
# --------------------------------------------------------------------------- #


def test_sell_closes_a_paper_position_and_credits_cash(storage):
    storage.seed_crypto_cash(dec(0))
    storage.open_crypto_position(BTC, dec(2), dec(100))
    runtime = _runtime(storage, FakeClient())
    outcome = _sell(runtime, BTC, dec(120), "take-profit")
    assert outcome["state"] == "filled"
    assert outcome["realised_pnl"] == str(dec(40))  # (120-100)*2
    assert storage.crypto_position(BTC) is None
    assert storage.crypto_cash() == dec(240)


def test_sell_at_a_loss_records_a_negative_realised_pnl(storage):
    storage.open_crypto_position(BTC, dec(1), dec(100))
    runtime = _runtime(storage, FakeClient())
    outcome = _sell(runtime, BTC, dec(80), "stop-loss")
    assert outcome["realised_pnl"] == str(dec(-20))


def test_live_sell_places_a_real_order(storage):
    storage.open_crypto_position(BTC, dec(1), dec(100))
    client = FakeClient(order_response={BTC: {"id": "9", "average": "90"}})
    runtime = _runtime(storage, client, mode="live")
    outcome = _sell(runtime, BTC, dec(95), "trend exit")
    assert client.orders == [(BTC, "sell", dec(1))]
    assert outcome["realised_pnl"] == str(dec(-10))  # (90-100)*1


# --------------------------------------------------------------------------- #
# run_crypto_cycle
# --------------------------------------------------------------------------- #


def test_cycle_buys_a_qualifying_bullish_entry(storage):
    storage.seed_crypto_cash(dec(1000))
    client = FakeClient(ohlcv={BTC: _bullish_candles()})
    runtime = _runtime(storage, client, pairs=(BTC,))
    result = run_crypto_cycle(runtime)
    assert "halted" not in result
    assert any(a["action"] == "buy" for a in result["actions"])
    assert storage.crypto_position(BTC) is not None


def test_cycle_does_nothing_for_a_dull_sideways_signal(storage):
    storage.seed_crypto_cash(dec(1000))
    client = FakeClient(ohlcv={BTC: _candles(lambda i: 1)})
    runtime = _runtime(storage, client, pairs=(BTC,))
    result = run_crypto_cycle(runtime)
    assert all(a["action"] == "hold" for a in result["actions"])
    assert storage.crypto_position(BTC) is None


def test_cycle_exits_a_held_position_on_stop_loss_regardless_of_the_signal(storage):
    storage.open_crypto_position(BTC, dec(1), dec(100))
    # A flat/neutral signal that the trend strategy would otherwise just hold.
    client = FakeClient(ohlcv={BTC: _candles(lambda i: 0.70, base_price=100)})  # now 70: -30%
    runtime = _runtime(storage, client, pairs=(BTC,), stop_loss_pct=dec(8))
    result = run_crypto_cycle(runtime)
    action = result["actions"][0]
    assert action["action"] == "sell"
    assert "stop-loss" in action["reason"]
    assert storage.crypto_position(BTC) is None


def test_cycle_takes_profit_on_a_big_winner(storage):
    storage.open_crypto_position(BTC, dec(1), dec(100))
    client = FakeClient(ohlcv={BTC: _candles(lambda i: 1.20, base_price=100)})  # now 120: +20%
    runtime = _runtime(storage, client, pairs=(BTC,), take_profit_pct=dec(15))
    result = run_crypto_cycle(runtime)
    assert result["actions"][0]["action"] == "sell"
    assert "take-profit" in result["actions"][0]["reason"]


def test_cycle_halts_entirely_when_the_kill_switch_is_present(tmp_path, storage):
    client = FakeClient(ohlcv={BTC: _bullish_candles()})
    config = replace(
        make_config(), crypto=_crypto_config(pairs=(BTC,)), stop_file=tmp_path / "STOP"
    )
    (tmp_path / "STOP").touch()
    runtime = CryptoRuntime(config=config, storage=storage, client=client)
    result = run_crypto_cycle(runtime)
    assert "kill switch" in result["halted"]
    assert result["actions"] == []


def test_cycle_records_a_per_symbol_error_and_keeps_going(storage):
    client = FakeClient(
        ohlcv={ETH: _bullish_candles()},
        raise_on={BTC: CryptoError("symbol delisted")},
    )
    storage.seed_crypto_cash(dec(1000))
    runtime = _runtime(storage, client, pairs=(BTC, ETH))
    result = run_crypto_cycle(runtime)
    assert any("symbol delisted" in e for e in result["errors"])
    assert any(a["symbol"] == ETH for a in result["actions"])


def test_cycle_stops_buying_once_the_daily_trade_cap_is_hit(storage):
    storage.seed_crypto_cash(dec(1000))
    for i in range(3):
        storage.record_crypto_trade(
            mode="paper", symbol=f"X{i}/USDT", side="buy", quantity=dec(1),
            price=dec(1), notional=dec(1), strategy="trend", reason="prior",
        )
    client = FakeClient(ohlcv={BTC: _bullish_candles()})
    runtime = _runtime(storage, client, pairs=(BTC,), max_trades_per_day=3)
    result = run_crypto_cycle(runtime)
    assert result["actions"][0]["action"] == "skipped"
    assert "daily trade cap" in result["actions"][0]["reason"]


# --------------------------------------------------------------------------- #
# CryptoScheduler: the background loop the dashboard starts and stops
# --------------------------------------------------------------------------- #


def test_scheduler_runs_a_cycle_and_can_be_stopped(storage, monkeypatch):
    monkeypatch.setattr(crypto_engine, "build_client", lambda config: FakeClient())
    config = replace(
        make_config(),
        crypto=_crypto_config(pairs=(BTC,), poll_interval_seconds=30),
    )
    scheduler = CryptoScheduler(config, storage)
    scheduler.start()
    try:
        for _ in range(50):
            if scheduler.last_result is not None:
                break
            time.sleep(0.05)
        assert scheduler.running is True
        assert scheduler.last_result is not None
    finally:
        scheduler.stop()
    assert scheduler.running is False


def test_starting_an_already_running_scheduler_is_a_no_op(storage, monkeypatch):
    monkeypatch.setattr(crypto_engine, "build_client", lambda config: FakeClient())
    config = replace(make_config(), crypto=_crypto_config(pairs=(BTC,)))
    scheduler = CryptoScheduler(config, storage)
    scheduler.start()
    first_thread = scheduler._thread
    scheduler.start()
    assert scheduler._thread is first_thread
    scheduler.stop()
