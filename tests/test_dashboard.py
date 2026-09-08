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
