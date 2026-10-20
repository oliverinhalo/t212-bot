"""The crypto cash/position/trade bookkeeping in storage.py."""

from __future__ import annotations

from t212bot.models import ZERO, dec


def test_crypto_cash_starts_at_zero_until_seeded(storage):
    assert storage.crypto_cash() == ZERO


def test_seeding_sets_the_cash_once(storage):
    storage.seed_crypto_cash(dec(50))
    assert storage.crypto_cash() == dec(50)
    storage.seed_crypto_cash(dec(999))  # not forced: no-op
    assert storage.crypto_cash() == dec(50)


def test_forcing_the_seed_resets_it(storage):
    storage.seed_crypto_cash(dec(50))
    storage.seed_crypto_cash(dec(10), force=True)
    assert storage.crypto_cash() == dec(10)


def test_adjust_crypto_cash_is_additive(storage):
    storage.seed_crypto_cash(dec(50))
    total = storage.adjust_crypto_cash(dec("-20"))
    assert total == dec(30)
    assert storage.crypto_cash() == dec(30)


def test_no_position_is_none(storage):
    assert storage.crypto_position("BTC/USDT") is None
    assert storage.crypto_positions() == {}


def test_opening_and_reading_a_position(storage):
    storage.open_crypto_position("BTC/USDT", dec("0.01"), dec(60000))
    quantity, average_price, opened_at = storage.crypto_position("BTC/USDT")
    assert quantity == dec("0.01")
    assert average_price == dec(60000)
    assert opened_at is not None
    assert "BTC/USDT" in storage.crypto_positions()


def test_closing_a_position_removes_it(storage):
    storage.open_crypto_position("BTC/USDT", dec("0.01"), dec(60000))
    storage.close_crypto_position("BTC/USDT")
    assert storage.crypto_position("BTC/USDT") is None


def test_record_and_count_trades_today(storage):
    assert storage.crypto_trades_today() == 0
    storage.record_crypto_trade(
        mode="paper", symbol="BTC/USDT", side="buy", quantity=dec("0.01"),
        price=dec(60000), notional=dec(600), strategy="trend", reason="test entry",
    )
    assert storage.crypto_trades_today() == 1


def test_an_errored_trade_does_not_count_toward_the_daily_cap(storage):
    storage.record_crypto_trade(
        mode="live", symbol="BTC/USDT", side="buy", quantity=dec("0.01"),
        price=dec(60000), notional=dec(600), strategy="trend", reason="attempt",
        error="exchange refused it",
    )
    assert storage.crypto_trades_today() == 0


def test_recent_trades_are_newest_first(storage):
    storage.record_crypto_trade(
        mode="paper", symbol="BTC/USDT", side="buy", quantity=dec(1),
        price=dec(1), notional=dec(1), strategy="trend", reason="first",
    )
    storage.record_crypto_trade(
        mode="paper", symbol="ETH/USDT", side="buy", quantity=dec(1),
        price=dec(1), notional=dec(1), strategy="trend", reason="second",
    )
    rows = storage.recent_crypto_trades(limit=10)
    assert [r["reason"] for r in rows] == ["second", "first"]
