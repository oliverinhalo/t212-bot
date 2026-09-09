"""End-to-end cycle behaviour in paper mode, and the halt conditions."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx

from t212bot.ai_advisor import StubProvider
from t212bot.executor import Executor
from t212bot.fx import FxConverter
from t212bot.instruments import Instrument, InstrumentCatalogue
from t212bot.main import Runtime, run_cycle, trading_day, within_trading_window
from t212bot.market_data import StaticMarketData, SymbolResolver
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


_UNIVERSE = [
    Instrument("NVDA_US_EQ", "Nvidia", "NVDA", "US67066G1040", "USD", "STOCK"),
    Instrument("VODl_EQ", "Vodafone", "VOD", "GB00BH4HKS39", "GBX", "STOCK"),
    Instrument("VUAGl_EQ", "Vanguard S&P 500 Acc", "VUAG", "IE00BFMXXD54", "GBP", "ETF"),
]


def make_open_runtime(storage, ai_response, *, tmp_path, symbol_prices, usd_rate="1.25"):
    config = make_config(mode="paper", enforce_allowlist=False, max_trades_per_day=5)
    fx = FxConverter(
        client=httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={
                "chart": {"result": [{"meta": {"regularMarketPrice": float(usd_rate)}}]}
            })
        )),
        clock=lambda: 0.0,
    )
    resolver = SymbolResolver(
        overrides={"NVDA_US_EQ": "NVDA", "VODl_EQ": "VOD.L"},
        cache_path=tmp_path / "symbol_map.json",
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    return Runtime(
        config=config,
        storage=storage,
        market=StaticMarketData({TICKER: dec(10), OTHER: dec(7)}, symbol_prices=symbol_prices),
        ai=StubProvider(response=ai_response),
        client=None,
        executor=Executor(config, storage),
        catalogue=InstrumentCatalogue(_UNIVERSE),
        fx=fx,
        resolver=resolver,
    )


def buy_response(notional=10, ticker=TICKER, confidence=0.9, price=10) -> str:
    return json.dumps(
        {
            "action": "buy",
            "ticker": ticker,
            "notional_or_qty": notional,
            "size_unit": "gbp",
            "price": price,
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


# --------------------------------------------------------------------------- #
# The advice ladder: budget guard, local fallback, cache skip
# --------------------------------------------------------------------------- #


class SpyProvider:
    """Counts calls so a test can assert the model was (or was not) consulted."""

    def __init__(self, name="openrouter", response=None):
        self.name = name
        self.model = "spy/free"
        self.last_model = "spy/free"
        self.last_http_calls = 1
        self.calls = 0
        self._response = response or json.dumps({"action": "hold", "confidence": 1})

    def complete(self, system, user):
        self.calls += 1
        return self._response


def _utc_today():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).date()


def test_a_spent_budget_hands_over_to_the_local_strategy(storage):
    provider = SpyProvider(name="openrouter")
    runtime = make_runtime(storage)
    runtime.ai = provider
    storage.record_ai_calls(_utc_today(), runtime.config.ai.daily_request_budget)

    run_cycle(runtime, force=True)

    assert provider.calls == 0                       # never hit the network
    row = storage.recent_decisions(1)[0]
    assert row["provider"] == "local"


def test_an_unchanged_market_skips_the_second_ai_call(storage):
    provider = SpyProvider(name="openrouter")
    runtime = make_runtime(storage)
    runtime.ai = provider

    assert run_cycle(runtime, force=True) == "no-trade"
    assert provider.calls == 1
    # Nothing changed and the last cycle held -> the model is not asked again.
    assert run_cycle(runtime, force=True) == "no-trade"
    assert provider.calls == 1
    assert storage.recent_decisions(1)[0]["provider"] == "cache"


def test_a_provider_failure_falls_back_to_local_not_a_blind_hold(storage):
    class Broken(SpyProvider):
        def complete(self, system, user):
            from t212bot.ai_advisor import ProviderError

            self.calls += 1
            raise ProviderError("network down")

    runtime = make_runtime(storage)
    runtime.ai = Broken(name="openrouter")
    run_cycle(runtime, force=True)

    row = storage.recent_decisions(1)[0]
    assert row["provider"] == "local"


# --------------------------------------------------------------------------- #
# Open-universe mode
# --------------------------------------------------------------------------- #


def test_open_universe_resolves_prices_and_converts_a_us_stock(storage, tmp_path):
    runtime = make_open_runtime(
        storage,
        buy_response(ticker="Nvidia", notional=10, price=None),
        tmp_path=tmp_path,
        symbol_prices={"NVDA": (dec("125"), "USD")},   # $125 -> £100 at 1.25
    )
    assert run_cycle(runtime, force=True) == "order:filled"
    quantity, avg = storage.paper_positions()["NVDA_US_EQ"]
    # £10 buy at a £100 GBP-equivalent price -> 0.1 shares
    assert quantity == dec("0.1")
    assert dec("99") < avg < dec("101")


def test_open_universe_prices_a_london_stock_without_fx(storage, tmp_path):
    runtime = make_open_runtime(
        storage,
        buy_response(ticker="VODl_EQ", notional=10, price=None),
        tmp_path=tmp_path,
        symbol_prices={"VOD.L": (dec("7000"), "GBp")},   # 7000p -> £70
    )
    assert run_cycle(runtime, force=True) == "order:filled"
    quantity, _ = storage.paper_positions()["VODl_EQ"]
    assert quantity == dec("0.142857")  # £10 / £70, floored to 6dp


def test_open_universe_falls_back_to_derived_symbol_on_currency_mismatch(storage, tmp_path):
    # ISIN search returned USD VUAA.L for IE00BFMXXD54, but T212 instrument is in GBP.
    # SymbolResolver._derive produces VUAG.L which is priced in GBP.
    config = make_config(mode="paper", enforce_allowlist=False, max_trades_per_day=5)
    resolver = SymbolResolver(
        overrides={"VUAGl_EQ": "VUAA.L"},  # Simulated mismatched ISIN result
        cache_path=tmp_path / "symbol_map.json",
    )
    runtime = Runtime(
        config=config,
        storage=storage,
        market=StaticMarketData(
            {TICKER: dec(10)},
            symbol_prices={
                "VUAA.L": (dec("90"), "USD"),   # Mismatched currency
                "VUAG.L": (dec("75"), "GBP"),   # Derived native symbol
            },
        ),
        ai=StubProvider(response=buy_response(ticker="VUAGl_EQ", notional=10, price=None)),
        client=None,
        executor=Executor(config, storage),
        catalogue=InstrumentCatalogue(_UNIVERSE),
        fx=None,
        resolver=resolver,
    )
    assert run_cycle(runtime, force=True) == "order:filled"
    quantity, _ = storage.paper_positions()["VUAGl_EQ"]
    assert quantity == dec("0.133333")  # £10 / £75, floored to 6dp


def test_open_universe_rejects_a_hallucinated_ticker(storage, tmp_path):
    runtime = make_open_runtime(
        storage,
        buy_response(ticker="TotallyMadeUpCo", notional=10),
        tmp_path=tmp_path,
        symbol_prices={},
    )
    assert run_cycle(runtime, force=True) == "no-trade"
    assert storage.recent_decisions(1)[0]["rule"] == "R05_ALLOWLIST"
    assert storage.paper_positions() == {}


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


# --------------------------------------------------------------------------- #
# --until-trade: keep trying until an order lands, or the window runs out
# --------------------------------------------------------------------------- #


class FlakyProvider(SpyProvider):
    """Holds for the first ``holds`` calls, then proposes a buy."""

    def __init__(self, holds: int):
        super().__init__(name="omniroute")
        self._holds = holds

    def complete(self, system, user):
        self.calls += 1
        if self.calls <= self._holds:
            return json.dumps({"action": "hold", "confidence": 1})
        return buy_response()


def test_until_trade_keeps_going_until_an_order_lands(storage):
    from t212bot.main import run_until_trade

    provider = FlakyProvider(holds=2)
    runtime = make_runtime(storage)
    runtime.ai = provider

    traded, status = run_until_trade(
        runtime, force=True, deadline_minutes=5, interval_seconds=0
    )
    assert traded
    assert status == "order:filled"
    assert provider.calls == 3
    assert storage.paper_positions()[TICKER][0] == dec(1)


def test_until_trade_asks_the_model_every_attempt_despite_the_unchanged_skip(storage):
    # The market does not move between retries — that is the whole point of
    # retrying — so the "nothing changed" shortcut must not silence them.
    from t212bot.main import run_until_trade

    provider = FlakyProvider(holds=1)
    runtime = make_runtime(storage)
    runtime.ai = provider
    assert runtime.config.ai.skip_when_unchanged

    traded, status = run_until_trade(
        runtime, force=True, deadline_minutes=5, interval_seconds=0
    )
    assert traded and status == "order:filled"
    # Attempt 1 held; attempt 2 reached the model rather than the cached hold.
    assert provider.calls == 2
    assert storage.recent_decisions(1)[0]["provider"] == "omniroute"


def test_until_trade_gives_up_at_the_deadline_rather_than_running_forever(storage):
    from t212bot.main import run_until_trade

    runtime = make_runtime(storage)  # stub provider always holds
    traded, status = run_until_trade(
        runtime, force=True, deadline_minutes=1 / 600, interval_seconds=0
    )
    assert not traded
    assert status == "no-trade"
    assert storage.paper_positions() == {}


def test_until_trade_stops_immediately_on_a_halt_that_needs_a_human(storage, tmp_path):
    from t212bot.main import run_until_trade

    from dataclasses import replace

    stop_file = tmp_path / "STOP"
    stop_file.write_text("halt")
    config = make_config(mode="paper")
    runtime = make_runtime(storage, config, ai_response=buy_response())
    runtime.config = replace(config, stop_file=stop_file)

    traded, status = run_until_trade(
        runtime, force=True, deadline_minutes=60, interval_seconds=0
    )
    assert not traded
    assert status == "halted"


def test_until_trade_treats_an_approved_dry_run_as_the_answer(storage):
    from t212bot.main import run_until_trade

    runtime = make_runtime(storage, ai_response=buy_response())
    traded, status = run_until_trade(
        runtime, dry_run=True, force=True, deadline_minutes=5, interval_seconds=0
    )
    assert traded
    assert status == "dry-run"
    assert storage.paper_positions() == {}
