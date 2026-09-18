"""Dashboard endpoints, including the force-trade API."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from flask import Flask
from flask.testing import FlaskClient

from t212bot.dashboard import build_state, create_app
from t212bot.models import dec
from t212bot.storage import Storage


@pytest.fixture
def config():
    from conftest import make_config  # noqa: PLC0415 - test helper

    return make_config()


@pytest.fixture
def app(config) -> Flask:
    return create_app(config)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def test_index_returns_200(client: FlaskClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"t212-bot" in resp.data


def test_index_has_force_trade_button(client: FlaskClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Force trade" in resp.data
    assert b"force-trade" in resp.data


def test_api_state_returns_json(client: FlaskClient) -> None:
    resp = client.get("/api/state")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "equity" in data
    assert "positions" in data
    assert "decisions" in data


def test_force_trade_returns_running(client: FlaskClient) -> None:
    """POST /api/force-trade starts a cycle and returns a run id."""
    resp = client.post("/api/force-trade")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "id" in data
    assert data["status"] == "running"


def test_force_trade_concurrency_rejected(client: FlaskClient, storage: Storage, config) -> None:
    """A second force-trade while one is running returns 409."""
    from t212bot.dashboard import _force_trades, _force_lock

    # Seed a running entry so the endpoint sees an active cycle.
    rid = "seed-running"
    with _force_lock:
        _force_trades[rid] = {"status": "running"}
    try:
        resp1 = client.post("/api/force-trade")
        assert resp1.status_code == 409
    finally:
        with _force_lock:
            _force_trades.pop(rid, None)


def test_force_trade_status_unknown_id(client: FlaskClient) -> None:
    resp = client.get("/api/force-trade/status?id=unknown")
    assert resp.status_code == 404


def test_force_trade_status_while_running(client: FlaskClient, storage: Storage, config) -> None:
    """Poll status while the cycle is in progress returns running."""
    from t212bot.dashboard import _force_trades, _force_lock

    rid = "seed-status"
    with _force_lock:
        _force_trades[rid] = {"status": "running"}
    try:
        resp = client.get(f"/api/force-trade/status?id={rid}")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "running"
    finally:
        with _force_lock:
            _force_trades.pop(rid, None)


def test_healthz(client: FlaskClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_build_state_equity_from_snapshot(config) -> None:
    """Equity is read from the latest cycle snapshot."""
    storage = Storage(":memory:")
    try:
        state = build_state(config, storage)
        # Equity defaults to "0.00" when no cycles exist.
        assert state["equity"] in ("0", "0.00")
        assert state["positions"] == []
        assert state["decisions"] == []
    finally:
        storage.close()


def test_dashboard_api_state_serialises_breaker_and_unresolved(config) -> None:
    """/api/state serialises breaker and unresolved orders for the UI."""
    storage = Storage(":memory:")
    try:
        day = datetime.now(timezone.utc).date()
        storage.record_equity(day, dec(40))
        storage.trip_breaker(day, "test", dec(-10))

        state = build_state(config, storage)
        assert state["breaker"] is not None
        assert state["breaker"]["reason"] == "test"
    finally:
        storage.close()


# --------------------------------------------------------------------------- #
# Quick buy: one click, a fixed amount, checks deliberately bypassed
# --------------------------------------------------------------------------- #


class _SharedStorage:
    """The fixture owns this Storage, but Runtime.close() would close it.

    In production build_runtime opens its own connection and closing it is
    right; here the test still needs to read the database afterwards.
    """

    def __init__(self, storage):
        self._storage = storage

    def __getattr__(self, name):
        return getattr(self._storage, name)

    def close(self) -> None:
        pass


def _paper_runtime(storage, config):
    """A runtime that fills against the paper ledger, with static prices."""
    from t212bot.ai_advisor import StubProvider
    from t212bot.executor import Executor
    from t212bot.main import Runtime
    from t212bot.market_data import StaticMarketData

    from conftest import OTHER, TICKER  # noqa: PLC0415 - test helper

    return Runtime(
        config=config,
        storage=_SharedStorage(storage),
        market=StaticMarketData({TICKER: dec(10), OTHER: dec(7)}),
        ai=StubProvider(),
        client=None,
        executor=Executor(config, storage),
    )


def test_quick_buy_places_an_order(storage: Storage, config, monkeypatch) -> None:
    from t212bot import dashboard
    from t212bot.main import trading_day

    from conftest import TICKER  # noqa: PLC0415 - test helper

    storage.seed_paper_account(dec(50))
    monkeypatch.setattr(dashboard, "build_runtime", lambda cfg: _paper_runtime(storage, cfg))

    dashboard._run_quick_buy("run-1", config)

    entry = dashboard._quick_buys["run-1"]
    assert entry["status"] == "done", entry
    assert entry["order_state"] == "filled"
    assert entry["ticker"] == TICKER
    # £5 of a £10 share, and the paper ledger actually moved.
    assert storage.paper_positions()[TICKER][0] == dec("0.5")
    # It is in the audit trail like any other order.
    assert storage.orders_today(trading_day(config))[0].state == "filled"


def test_quick_buy_ignores_the_capital_cap(storage: Storage, monkeypatch) -> None:
    """The whole point: it buys when the scheduled path would refuse (R14)."""
    from t212bot import dashboard

    from conftest import TICKER, make_config  # noqa: PLC0415 - test helper

    # A cap already blown wide open — a normal cycle here is R14_CAPITAL_CAP.
    config = make_config(mode="paper", max_capital=1, min_order=100)
    storage.seed_paper_account(dec(50))
    monkeypatch.setattr(dashboard, "build_runtime", lambda cfg: _paper_runtime(storage, cfg))

    dashboard._run_quick_buy("run-2", config)

    entry = dashboard._quick_buys["run-2"]
    assert entry["status"] == "done", entry
    assert entry["order_state"] == "filled"
    assert storage.paper_positions()[TICKER][0] > dec(0)


def test_quick_buy_still_honours_the_kill_switch(storage: Storage, tmp_path, monkeypatch) -> None:
    """An override, not a bug: STOP still stops it."""
    from dataclasses import replace

    from t212bot import dashboard

    from conftest import make_config  # noqa: PLC0415 - test helper

    stop_file = tmp_path / "STOP"
    stop_file.write_text("halt")
    config = replace(make_config(mode="paper"), stop_file=stop_file)
    monkeypatch.setattr(dashboard, "build_runtime", lambda cfg: _paper_runtime(storage, cfg))

    dashboard._run_quick_buy("run-3", config)

    entry = dashboard._quick_buys["run-3"]
    assert entry["status"] == "error"
    assert "kill switch" in entry["error"]
    assert storage.paper_positions() == {}


def test_quick_buy_refuses_when_nothing_is_priced(storage: Storage, config, monkeypatch) -> None:
    from t212bot import dashboard
    from t212bot.market_data import StaticMarketData

    def unpriced(cfg):
        runtime = _paper_runtime(storage, cfg)
        runtime.market = StaticMarketData({})
        return runtime

    monkeypatch.setattr(dashboard, "build_runtime", unpriced)
    dashboard._run_quick_buy("run-4", config)

    entry = dashboard._quick_buys["run-4"]
    assert entry["status"] == "error"
    assert "no live price" in entry["error"]


def test_top_ranked_picks_the_highest_scoring_instrument(config) -> None:
    from datetime import date, timedelta

    from t212bot.dashboard import _top_ranked
    from t212bot.market_data import Snapshot
    from t212bot.models import Bar, PriceHistory

    from conftest import OTHER, TICKER, make_quote  # noqa: PLC0415 - test helper

    start = date(2026, 1, 1)
    # TICKER climbs steadily, OTHER slides: the ranking should prefer TICKER.
    quotes = {TICKER: make_quote(TICKER, 20), OTHER: make_quote(OTHER, 5)}
    histories = {
        TICKER: PriceHistory(
            ticker=TICKER,
            bars=tuple(Bar(day=start + timedelta(days=i), close=dec(10) + dec(i) / 5)
                       for i in range(60)),
        ),
        OTHER: PriceHistory(
            ticker=OTHER,
            bars=tuple(Bar(day=start + timedelta(days=i), close=dec(20) - dec(i) / 5)
                       for i in range(60)),
        ),
    }
    ticker, quote = _top_ranked(config, Snapshot(quotes=quotes, histories=histories, errors={}))

    assert ticker == TICKER
    assert quote.price == dec(20)



def test_index_has_a_quick_buy_button(client: FlaskClient) -> None:
    resp = client.get("/")
    assert b"quick-buy-btn" in resp.data
    assert b"Buy now" in resp.data


def test_quick_buy_endpoint_returns_running(client: FlaskClient, monkeypatch) -> None:
    from t212bot import dashboard

    monkeypatch.setattr(dashboard, "_run_quick_buy", lambda run_id, config: None)
    resp = client.post("/api/quick-buy")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "running"


def test_quick_buy_is_not_started_twice(client: FlaskClient) -> None:
    from t212bot.dashboard import _force_lock, _quick_buys

    with _force_lock:
        _quick_buys["seed-running"] = {"status": "running"}
    try:
        assert client.post("/api/quick-buy").status_code == 409
    finally:
        with _force_lock:
            _quick_buys.pop("seed-running", None)


def test_quick_buy_status_unknown_id(client: FlaskClient) -> None:
    assert client.get("/api/quick-buy/status?id=nope").status_code == 404
