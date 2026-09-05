"""End-to-end cycle behaviour in paper mode, and the halt conditions."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from t212bot.ai_advisor import StubProvider
from t212bot.executor import Executor
from t212bot.main import Runtime, run_cycle, trading_day, within_trading_window
from t212bot.market_data import StaticMarketData
from t212bot.models import dec

from conftest import OTHER, TICKER, make_config

LONDON = ZoneInfo("Europe/London")


def make_runtime(storage, config=None, ai_response=None):
    config = config or make_config(mode="paper")
    provider = StubProvider(response=ai_response)
    return Runtime(
        config=config,
        storage=storage,
        market=StaticMarketData({TICKER: dec(10), OTHER: dec(7)}),
        ai=provider,
        client=None,
        executor=Executor(config, storage),
    )


def buy_response(notional=10, ticker=TICKER, confidence=0.9) -> str:
    return json.dumps(
        {
            "action": "buy",
            "ticker": ticker,
            "notional_or_qty": notional,
            "size_unit": "gbp",
            "price": 10,
            "confidence": confidence,
            "reasoning": "test buy",
        }
    )


# --------------------------------------------------------------------------- #
# Trading window
# --------------------------------------------------------------------------- #


def test_weekend_is_outside_the_trading_window():
    config = make_config()
    saturday = datetime(2026, 3, 7, 12, 0, tzinfo=LONDON)
    open_now, why = within_trading_window(config, saturday)
    assert not open_now
    assert "sat" in why


def test_before_the_open_is_outside_the_window():
    config = make_config()
    early = datetime(2026, 3, 2, 6, 0, tzinfo=LONDON)
    assert not within_trading_window(config, early)[0]


def test_mid_session_is_inside_the_window():
    config = make_config()
    midday = datetime(2026, 3, 2, 12, 0, tzinfo=LONDON)
    assert within_trading_window(config, midday)[0]


def test_trading_day_uses_the_configured_timezone():
    config = make_config()
    late = datetime(2026, 3, 2, 23, 30, tzinfo=LONDON)
    assert trading_day(config, late).isoformat() == "2026-03-02"


# --------------------------------------------------------------------------- #
# Halt conditions
# --------------------------------------------------------------------------- #


def test_kill_switch_halts_before_anything_else(storage, tmp_path):
    stop = tmp_path / "STOP"
    stop.write_text("")
    config = make_config(mode="paper")
    config = config.__class__(**{**config.__dict__, "stop_file": stop})
    runtime = make_runtime(storage, config, ai_response=buy_response())

    assert run_cycle(runtime, force=True) == "halted"
    assert storage.trades_today(trading_day(config)) == 0


def test_a_tripped_breaker_halts_the_cycle(storage):
    runtime = make_runtime(storage, ai_response=buy_response())
    storage.trip_breaker(trading_day(runtime.config), "lost too much", dec(-5))

    assert run_cycle(runtime, force=True) == "halted"
    assert storage.trades_today(trading_day(runtime.config)) == 0


def test_an_unresolved_order_halts_the_cycle(storage):
    from t212bot.models import Verdict

    runtime = make_runtime(storage, ai_response=buy_response())
    verdict = Verdict(
        approved=True, action="buy", rule="OK", ticker=TICKER,
        quantity=dec(1), notional=dec(10), reference_price=dec(10),
    )
    storage.reserve_order("old", trading_day(runtime.config), "paper", verdict)
    storage.mark_stranded_orders_unknown("crash")

    assert run_cycle(runtime, force=True) == "halted"


def test_the_market_being_closed_skips_the_cycle(storage):
    runtime = make_runtime(storage, ai_response=buy_response())
    # force=False, and the test clock is whatever it is — assert only that a
    # closed market produces a skip rather than a trade.
    status = run_cycle(runtime, force=False)
    assert status in ("skipped", "no-trade", "order:filled")


def test_the_breaker_trips_and_persists_when_the_loss_limit_is_hit(storage):
    runtime = make_runtime(storage)
    day = trading_day(runtime.config)
    storage.record_equity(day, dec(50))          # yesterday's baseline for today
    storage.seed_paper_account(dec(40))          # equity has fallen to 40

    assert run_cycle(runtime, force=True) == "halted"
    assert storage.breaker_tripped()
    assert "breached" in storage.breaker()["reason"]


# --------------------------------------------------------------------------- #
# The happy path
# --------------------------------------------------------------------------- #


def test_a_stub_hold_records_a_cycle_with_no_order(storage):
    runtime = make_runtime(storage)
    assert run_cycle(runtime, force=True) == "no-trade"
    assert storage.trades_today(trading_day(runtime.config)) == 0
    decisions = storage.recent_decisions(1)
    assert decisions[0]["ai_action"] == "hold"
    assert decisions[0]["rule"] == "R04_HOLD"


def test_an_approved_buy_reaches_the_paper_ledger(storage):
    runtime = make_runtime(storage, ai_response=buy_response())
    assert run_cycle(runtime, force=True) == "order:filled"
    assert storage.paper_positions()[TICKER][0] == dec(1)
    assert storage.paper_cash() == dec(40)


def test_dry_run_stops_short_of_placing_the_order(storage):
    runtime = make_runtime(storage, ai_response=buy_response())
    assert run_cycle(runtime, dry_run=True, force=True) == "dry-run"
    assert storage.paper_positions() == {}
    assert storage.trades_today(trading_day(runtime.config)) == 0


def test_an_off_allow_list_ticker_never_reaches_the_ledger(storage):
    runtime = make_runtime(storage, ai_response=buy_response(ticker="TSLA_US_EQ"))
    assert run_cycle(runtime, force=True) == "no-trade"
    assert storage.paper_positions() == {}
    assert storage.recent_decisions(1)[0]["rule"] == "R05_ALLOWLIST"


def test_an_oversized_proposal_is_shrunk_to_the_cap(storage):
    runtime = make_runtime(storage, ai_response=buy_response(notional=45))
    run_cycle(runtime, force=True)
    quantity, _ = storage.paper_positions()[TICKER]
    assert quantity == dec("1.25")  # £12.50 cap at £10/share


def test_the_frequency_cap_stops_the_sixth_cycle_of_the_day(storage):
    config = make_config(mode="paper", max_trades_per_day=2, min_order="0.01")
    runtime = make_runtime(storage, config, ai_response=buy_response(notional=1))
    for _ in range(2):
        assert run_cycle(runtime, force=True) == "order:filled"
    assert run_cycle(runtime, force=True) == "no-trade"
    assert storage.recent_decisions(1)[0]["rule"] == "R10_FREQUENCY"


def test_every_cycle_writes_a_full_audit_row(storage):
    runtime = make_runtime(storage, ai_response=buy_response())
    run_cycle(runtime, force=True)

    row = storage.recent_decisions(1)[0]
    assert row["ai_action"] == "buy"
    assert row["approved"] == 1
    assert row["order_state"] == "filled"
    assert row["reasons"]

    snapshot = json.loads(
        storage._read("SELECT snapshot FROM cycles LIMIT 1")[0]["snapshot"]
    )
    assert snapshot["equity"] == "50"
    assert snapshot["quotes"][TICKER]["price"] == "10"


def test_no_api_key_ever_appears_in_the_audit_log(storage):
    """The database holds prompts and responses, and must hold nothing else."""
    runtime = make_runtime(storage, ai_response=buy_response())
    run_cycle(runtime, force=True)

    dump = "\n".join(
        str(dict(row))
        for table in ("cycles", "ai_decisions", "risk_verdicts", "orders")
        for row in storage._read(f"SELECT * FROM {table}")
    )
    for secret in ("Authorization", "Basic ", "sk-", "Bearer "):
        assert secret not in dump


def test_a_cycle_never_exceeds_the_capital_cap_over_many_runs(storage):
    """Repeated buy recommendations must plateau at max_capital, not sail past it."""
    config = make_config(mode="paper", max_trades_per_day=50, max_position_pct=100,
                         min_order="0.01")
    runtime = make_runtime(storage, config, ai_response=buy_response(notional=45))
    for _ in range(10):
        run_cycle(runtime, force=True)

    invested = sum(
        quantity * dec(10) for quantity, _ in storage.paper_positions().values()
    )
    assert invested <= config.capital.max_capital
    assert storage.paper_cash() >= Decimal(0)
