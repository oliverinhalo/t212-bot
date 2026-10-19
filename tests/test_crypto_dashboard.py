"""The dashboard's crypto settings/start/stop/status routes."""

from __future__ import annotations

from dataclasses import replace

import pytest

from flask import Flask
from flask.testing import FlaskClient

from t212bot import crypto_engine
from t212bot.config import StorageConfig
from t212bot.dashboard import create_app
from t212bot.models import dec

from conftest import make_config


class FakeClient:
    """No real exchange — the dashboard tests only need start/stop to work."""

    def __init__(self, *_args, **_kwargs):
        pass

    def fetch_ohlcv(self, symbol, timeframe, limit):
        return []

    def fetch_balance(self, currency):
        return 0

    def create_market_order(self, symbol, side, amount):
        return {"id": "1"}

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_real_exchange(monkeypatch):
    monkeypatch.setattr(crypto_engine, "build_client", lambda config: FakeClient())


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "data" / "test.sqlite3"
    return replace(make_config(), storage=StorageConfig(db_path=db_path))


@pytest.fixture
def app(config) -> Flask:
    return create_app(config)


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
        c.post("/api/crypto/stop")  # nothing should still be running once a test ends


def test_settings_defaults_are_disabled_with_no_key_saved(client: FlaskClient):
    resp = client.get("/api/crypto/settings")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["enabled"] is False
    assert data["pairs"] == []
    assert data["has_api_key"] is False


def test_saving_settings_persists_and_echoes_them_back(client: FlaskClient):
    resp = client.post(
        "/api/crypto/settings",
        json={"exchange": "kraken", "pairs": ["BTC/USDT", "ETH/USDT"], "strategy": "mean_reversion"},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["exchange"] == "kraken"
    assert data["pairs"] == ["BTC/USDT", "ETH/USDT"]
    assert data["strategy"] == "mean_reversion"

    # And a fresh read agrees — it was actually written, not just echoed.
    again = client.get("/api/crypto/settings").get_json()
    assert again["exchange"] == "kraken"


def test_saving_an_api_key_is_reflected_in_has_api_key_but_never_echoed(client: FlaskClient):
    resp = client.post("/api/crypto/settings", json={"api_key": "secret-key", "api_secret": "s"})
    assert resp.status_code == 200
    data = resp.get_json()
    assert "api_key" not in data
    assert data["has_api_key"] is True


def test_invalid_settings_are_rejected_with_a_clear_error(client: FlaskClient):
    resp = client.post(
        "/api/crypto/settings", json={"enabled": True, "pairs": ["BTC/USDT", "ETH/EUR"]}
    )
    assert resp.status_code == 400
    assert "quote currency" in resp.get_json()["error"]


def test_saving_mismatched_pairs_without_enabling_is_not_validated_yet(client: FlaskClient):
    """Validation of ``pairs`` only bites once ``enabled`` is true — saving a
    draft (e.g. mid-edit) does not need every field to already be consistent."""
    resp = client.post("/api/crypto/settings", json={"pairs": ["BTC/USDT", "ETH/EUR"]})
    assert resp.status_code == 200
    # But starting it now surfaces exactly the same problem.
    start = client.post("/api/crypto/start")
    assert start.status_code == 400
    assert "quote currency" in start.get_json()["error"]


def test_start_refuses_without_any_pairs_configured(client: FlaskClient):
    resp = client.post("/api/crypto/start")
    assert resp.status_code == 400
    assert "pairs" in resp.get_json()["error"]


def test_start_then_stop_round_trip(client: FlaskClient):
    client.post("/api/crypto/settings", json={"pairs": ["BTC/USDT"]})

    started = client.post("/api/crypto/start")
    assert started.status_code == 200
    assert started.get_json()["running"] is True

    status = client.get("/api/crypto/status").get_json()
    assert status["running"] is True
    assert status["enabled"] is True

    stopped = client.post("/api/crypto/stop")
    assert stopped.status_code == 200
    assert stopped.get_json()["running"] is False
    assert client.get("/api/crypto/status").get_json()["enabled"] is False


def test_status_reports_positions_and_trades(client: FlaskClient, config):
    from t212bot.storage import Storage

    storage = Storage(config.storage.db_path)
    storage.open_crypto_position("BTC/USDT", dec("0.01"), dec(60000))
    storage.record_crypto_trade(
        mode="paper", symbol="BTC/USDT", side="buy", quantity=dec("0.01"),
        price=dec(60000), notional=dec(600), strategy="trend", reason="entry",
    )
    storage.close()

    status = client.get("/api/crypto/status").get_json()
    assert len(status["positions"]) == 1
    assert status["positions"][0]["symbol"] == "BTC/USDT"
    assert len(status["recent_trades"]) == 1


def test_the_dashboard_page_still_renders_with_the_crypto_section(client: FlaskClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Crypto" in resp.data
    assert b"crypto-start-btn" in resp.data
