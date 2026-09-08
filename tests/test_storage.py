"""Audit trail, duplicate-order guard, and the paper ledger."""

from __future__ import annotations

from datetime import date

import pytest

from t212bot.models import AIResult, Proposal, Verdict, dec
from t212bot.storage import (
    STATE_CANCELLED,
    STATE_FILLED,
    STATE_UNKNOWN,
    Storage,
    positions_from_paper,
)

DAY = date(2026, 3, 2)
TICKER = "VUSAl_EQ"


def approved_verdict(quantity="1", notional="10") -> Verdict:
    return Verdict(
        approved=True,
        action="buy",
        rule="OK",
        ticker=TICKER,
        quantity=dec(quantity),
        notional=dec(notional),
        reference_price=dec(10),
    )


# --------------------------------------------------------------------------- #
# Duplicate-order guard
# --------------------------------------------------------------------------- #


def test_reserving_the_same_decision_twice_is_refused(storage):
    assert storage.reserve_order("d1", DAY, "paper", approved_verdict())
    assert not storage.reserve_order("d1", DAY, "paper", approved_verdict())


def test_the_guard_survives_a_restart(tmp_path):
    """A new process must see the same claim — this is why it lives in SQLite."""
    path = tmp_path / "db.sqlite3"
    with Storage(path) as first:
        assert first.reserve_order("d1", DAY, "paper", approved_verdict())
    with Storage(path) as second:
        assert not second.reserve_order("d1", DAY, "paper", approved_verdict())


def test_known_decision_ids_reports_every_claim(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.reserve_order("d2", DAY, "paper", approved_verdict())
    assert storage.known_decision_ids() == frozenset({"d1", "d2"})


def test_settling_an_order_does_not_release_the_claim(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.settle_order("d1", STATE_FILLED, fill_price=dec(10), fill_quantity=dec(1))
    assert not storage.reserve_order("d1", DAY, "paper", approved_verdict())


# --------------------------------------------------------------------------- #
# Crash recovery
# --------------------------------------------------------------------------- #


def test_stranded_in_flight_orders_become_unknown(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.mark_submitting("d1")
    assert storage.mark_stranded_orders_unknown("restart") == 1
    assert storage.get_order("d1").state == STATE_UNKNOWN
    assert [o.decision_id for o in storage.unresolved_orders()] == ["d1"]


def test_settled_orders_are_not_disturbed_by_recovery(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.settle_order("d1", STATE_FILLED)
    assert storage.mark_stranded_orders_unknown("restart") == 0
    assert storage.unresolved_orders() == []


def test_resolving_an_unknown_order_unblocks_trading(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.mark_stranded_orders_unknown("restart")
    assert storage.resolve_order("d1", "checked the app; no order was placed")
    assert storage.unresolved_orders() == []


def test_resolving_an_unknown_id_is_a_no_op(storage):
    assert not storage.resolve_order("nope", "note")


# --------------------------------------------------------------------------- #
# Daily counters
# --------------------------------------------------------------------------- #


def test_trades_today_counts_attempts_not_fills(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.settle_order("d1", STATE_FILLED)
    storage.reserve_order("d2", DAY, "paper", approved_verdict())
    storage.settle_order("d2", "rejected", error="broker said no")
    assert storage.trades_today(DAY) == 2


def test_cancelled_orders_do_not_consume_a_daily_slot(storage):
    """Withdrawn before submission, so it never reached the broker."""
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    storage.settle_order("d1", STATE_CANCELLED, error="pre-submit re-check")
    assert storage.trades_today(DAY) == 0


def test_trades_are_counted_per_day(storage):
    storage.reserve_order("d1", DAY, "paper", approved_verdict())
    assert storage.trades_today(date(2026, 3, 3)) == 0


# --------------------------------------------------------------------------- #
# Equity baseline and the breaker
# --------------------------------------------------------------------------- #


def test_day_start_equity_is_set_once_and_never_moves(storage):
    assert storage.record_equity(DAY, dec(50)) == dec(50)
    assert storage.record_equity(DAY, dec(44)) == dec(50)
    assert storage.day_start_equity(DAY) == dec(50)


def test_each_day_gets_its_own_baseline(storage):
    storage.record_equity(DAY, dec(50))
    assert storage.record_equity(date(2026, 3, 3), dec(44)) == dec(44)


def test_breaker_persists_and_resets(storage):
    assert not storage.breaker_tripped()
    storage.trip_breaker(DAY, "lost 10%", dec(-5))
    assert storage.breaker_tripped()
    assert storage.breaker()["reason"] == "lost 10%"
    assert storage.reset_breaker()
    assert not storage.breaker_tripped()


def test_tripping_twice_keeps_the_first_reason(storage):
    storage.trip_breaker(DAY, "first", dec(-5))
    storage.trip_breaker(DAY, "second", dec(-9))
    assert storage.breaker()["reason"] == "first"


# --------------------------------------------------------------------------- #
# Paper ledger
# --------------------------------------------------------------------------- #


def test_paper_account_is_seeded_once(storage):
    assert storage.seed_paper_account(dec(50)) == dec(50)
    storage.apply_paper_fill(TICKER, dec(1), dec(10), DAY)
    assert storage.seed_paper_account(dec(50)) == dec(40)  # not re-seeded


def test_force_seeding_clears_positions(storage):
    storage.seed_paper_account(dec(50))
    storage.apply_paper_fill(TICKER, dec(1), dec(10), DAY)
    assert storage.seed_paper_account(dec(50), force=True) == dec(50)
    assert storage.paper_positions() == {}


def test_paper_buy_moves_cash_into_a_position(storage):
    storage.seed_paper_account(dec(50))
    storage.apply_paper_fill(TICKER, dec(2), dec(10), DAY)
    assert storage.paper_cash() == dec(30)
    assert storage.paper_positions()[TICKER] == (dec(2), dec(10))


def test_paper_buys_average_the_cost_basis(storage):
    storage.seed_paper_account(dec(50))
    storage.apply_paper_fill(TICKER, dec(1), dec(10), DAY)
    storage.apply_paper_fill(TICKER, dec(1), dec(20), DAY)
    quantity, average = storage.paper_positions()[TICKER]
    assert quantity == dec(2)
    assert average == dec(15)


def test_paper_sell_realises_profit(storage):
    storage.seed_paper_account(dec(50))
    storage.apply_paper_fill(TICKER, dec(2), dec(10), DAY)
    realised = storage.apply_paper_fill(TICKER, dec(-2), dec(12), DAY)
    assert realised == dec(4)
    assert storage.paper_cash() == dec(54)
    assert storage.paper_positions() == {}
    assert dec(storage.daily_row(DAY)["realised_pnl"]) == dec(4)


def test_paper_sell_realises_a_loss(storage):
    storage.seed_paper_account(dec(50))
    storage.apply_paper_fill(TICKER, dec(2), dec(10), DAY)
    assert storage.apply_paper_fill(TICKER, dec(-1), dec(8), DAY) == dec(-2)


def test_paper_buy_cannot_overdraw_the_virtual_account(storage):
    storage.seed_paper_account(dec(50))
    with pytest.raises(ValueError, match="exceeds virtual cash"):
        storage.apply_paper_fill(TICKER, dec(10), dec(10), DAY)


def test_paper_sell_of_nothing_is_refused(storage):
    storage.seed_paper_account(dec(50))
    with pytest.raises(ValueError, match="nothing held"):
        storage.apply_paper_fill(TICKER, dec(-1), dec(10), DAY)


def test_paper_positions_are_marked_to_market():
    positions = positions_from_paper({TICKER: (dec(2), dec(10))}, {TICKER: dec(12)})
    assert positions[0].value == dec(24)
    assert positions[0].unrealised_pnl == dec(4)


def test_paper_positions_fall_back_to_cost_without_a_quote():
    positions = positions_from_paper({TICKER: (dec(2), dec(10))}, {})
    assert positions[0].current_price == dec(10)
    assert positions[0].unrealised_pnl == dec(0)


# --------------------------------------------------------------------------- #
# Audit trail
# --------------------------------------------------------------------------- #


def test_a_full_cycle_is_recorded_and_readable(storage):
    storage.start_cycle("d1", DAY, "paper")
    storage.record_snapshot("d1", {"equity": "50.00"})
    storage.record_ai(
        "d1",
        AIResult(
            proposal=Proposal(action="buy", ticker=TICKER, notional=dec(10),
                              confidence=dec("0.8"), reasoning="looks cheap"),
            provider="stub",
            model="stub",
            prompt="PROMPT",
            raw_response='{"action": "buy"}',
            latency_ms=12,
        ),
    )
    storage.record_verdict("d1", approved_verdict())
    storage.finish_cycle("d1", "completed")

    decisions = storage.recent_decisions(10)
    assert len(decisions) == 1
    assert decisions[0]["ai_action"] == "buy"
    assert decisions[0]["approved"] == 1
    assert decisions[0]["reasoning"] == "looks cheap"


def test_the_prompt_and_raw_response_are_kept_verbatim(storage):
    storage.start_cycle("d1", DAY, "paper")
    storage.record_ai(
        "d1",
        AIResult(
            proposal=Proposal(action="hold"),
            provider="openrouter",
            model="openrouter/free",
            prompt="the full prompt",
            raw_response="the raw reply",
            latency_ms=1,
        ),
    )
    row = storage._read("SELECT prompt, raw_response FROM ai_decisions")[0]
    assert row["prompt"] == "the full prompt"
    assert row["raw_response"] == "the raw reply"


# --------------------------------------------------------------------------- #
# AI budget accounting and the cycle cache
# --------------------------------------------------------------------------- #


def test_ai_calls_accumulate_per_day(storage):
    assert storage.ai_calls_today(DAY) == 0
    storage.record_ai_calls(DAY, 1)
    storage.record_ai_calls(DAY, 2)
    assert storage.ai_calls_today(DAY) == 3
    storage.record_ai_calls(DAY, 0)          # no-op
    storage.record_ai_calls(DAY, -5)         # never decrements
    assert storage.ai_calls_today(DAY) == 3
    assert storage.ai_calls_today(date(2026, 3, 3)) == 0


def test_the_cycle_fingerprint_round_trips_and_is_singular(storage):
    assert storage.last_cycle_fingerprint() is None
    storage.record_cycle_fingerprint(DAY, "abc123", "hold")
    assert storage.last_cycle_fingerprint() == ("abc123", "hold")
    storage.record_cycle_fingerprint(DAY, "def456", "buy")
    assert storage.last_cycle_fingerprint() == ("def456", "buy")
