"""Loading and validating the crypto section, including the dashboard overlay."""

from __future__ import annotations

import json

import pytest

from t212bot.config import (
    LIVE_CONFIRMATION_VAR,
    ConfigError,
    crypto_overlay_paths,
    load,
)

MINIMAL = """
capital:
  max_capital_gbp: 50.00
  per_trade_cap_pct: 25.0
  max_position_pct: 25.0
  min_order_gbp: 1.00
risk:
  daily_loss_limit_pct: 10.0
  max_trades_per_day: 5
ai:
  provider: stub
storage:
  db_path: {db_path}
watchlist:
  - ticker: VUSAl_EQ
    yahoo: VUSA.L
    name: Vanguard S&P 500
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("MODE", LIVE_CONFIRMATION_VAR, "CRYPTO_API_KEY", "CRYPTO_API_SECRET"):
        monkeypatch.delenv(var, raising=False)


def write_config(tmp_path, crypto_yaml: str = "") -> tuple:
    db_path = tmp_path / "data" / "t.sqlite3"
    text = MINIMAL.format(db_path=db_path) + crypto_yaml
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path, db_path


def test_crypto_is_disabled_by_default(tmp_path):
    path, _ = write_config(tmp_path)
    config = load(path, env_file=None)
    assert config.crypto.enabled is False
    assert config.crypto.pairs == ()


def test_enabling_crypto_without_ccxt_installed_is_a_clear_error(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "ccxt":
            raise ImportError("no module named ccxt")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    path, _ = write_config(
        tmp_path, "crypto:\n  enabled: true\n  pairs: ['BTC/USDT']\n"
    )
    with pytest.raises(ConfigError, match="ccxt"):
        load(path, env_file=None)


def test_enabled_crypto_needs_at_least_one_pair(tmp_path):
    path, _ = write_config(tmp_path, "crypto:\n  enabled: true\n")
    with pytest.raises(ConfigError, match="pairs"):
        load(path, env_file=None)


def test_pairs_must_share_one_quote_currency(tmp_path):
    path, _ = write_config(
        tmp_path, "crypto:\n  enabled: true\n  pairs: ['BTC/USDT', 'ETH/EUR']\n"
    )
    with pytest.raises(ConfigError, match="quote currency"):
        load(path, env_file=None)


def test_a_pair_missing_a_slash_is_rejected(tmp_path):
    path, _ = write_config(tmp_path, "crypto:\n  enabled: true\n  pairs: ['BTCUSDT']\n")
    with pytest.raises(ConfigError, match="BASE/QUOTE"):
        load(path, env_file=None)


def test_live_crypto_needs_an_api_key(tmp_path, monkeypatch):
    monkeypatch.setenv(LIVE_CONFIRMATION_VAR, "yes")
    path, _ = write_config(
        tmp_path, "crypto:\n  enabled: true\n  mode: live\n  pairs: ['BTC/USDT']\n"
    )
    with pytest.raises(ConfigError, match="CRYPTO_API_KEY"):
        load(path, env_file=None)


def test_live_crypto_needs_the_real_money_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("CRYPTO_API_KEY", "k")
    path, _ = write_config(
        tmp_path, "crypto:\n  enabled: true\n  mode: live\n  pairs: ['BTC/USDT']\n"
    )
    with pytest.raises(ConfigError, match="real money"):
        load(path, env_file=None)


def test_live_crypto_starts_with_a_key_and_confirmation(tmp_path, monkeypatch):
    monkeypatch.setenv("CRYPTO_API_KEY", "k")
    monkeypatch.setenv(LIVE_CONFIRMATION_VAR, "yes")
    path, _ = write_config(
        tmp_path, "crypto:\n  enabled: true\n  mode: live\n  pairs: ['BTC/USDT']\n"
    )
    config = load(path, env_file=None)
    assert config.crypto.mode == "live"
    assert config.crypto.is_live is True


def test_paper_crypto_needs_no_key_at_all(tmp_path):
    path, _ = write_config(tmp_path, "crypto:\n  enabled: true\n  pairs: ['BTC/USDT']\n")
    config = load(path, env_file=None)
    assert config.crypto.mode == "paper"
    assert config.crypto.pairs == ("BTC/USDT",)


def test_min_order_over_the_per_trade_cap_is_rejected(tmp_path):
    path, _ = write_config(
        tmp_path,
        "crypto:\n  enabled: true\n  pairs: ['BTC/USDT']\n"
        "  max_capital: 10\n  per_trade_cap_pct: 10\n  min_order: 5\n",
    )
    with pytest.raises(ConfigError, match="per-trade cap"):
        load(path, env_file=None)


# --------------------------------------------------------------------------- #
# The dashboard overlay files
# --------------------------------------------------------------------------- #


def test_the_dashboard_settings_file_overrides_config_yaml(tmp_path):
    path, db_path = write_config(
        tmp_path, "crypto:\n  enabled: true\n  pairs: ['BTC/USDT']\n  strategy: trend\n"
    )
    secrets_path, settings_path = crypto_overlay_paths(db_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps({"strategy": "mean_reversion", "max_capital": "99"}))

    config = load(path, env_file=None)
    assert config.crypto.strategy == "mean_reversion"
    assert config.crypto.max_capital == 99
    # Untouched by the overlay, so config.yaml's value still applies.
    assert config.crypto.pairs == ("BTC/USDT",)


def test_the_dashboard_secrets_file_is_picked_up_and_overrides_the_main_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CRYPTO_API_KEY", "from-main-env")
    path, db_path = write_config(tmp_path, "crypto:\n  enabled: true\n  pairs: ['BTC/USDT']\n")
    secrets_path, _ = crypto_overlay_paths(db_path)
    secrets_path.parent.mkdir(parents=True, exist_ok=True)
    secrets_path.write_text("CRYPTO_API_KEY=from-dashboard\nCRYPTO_API_SECRET=shh\n")

    config = load(path, env_file=None)
    assert config.secrets.crypto_api_key == "from-dashboard"
    assert config.secrets.crypto_api_secret == "shh"


def test_a_malformed_settings_overlay_is_a_clear_error(tmp_path):
    path, db_path = write_config(tmp_path, "crypto:\n  enabled: false\n")
    _, settings_path = crypto_overlay_paths(db_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text("not json")

    with pytest.raises(ConfigError, match="crypto_settings.json"):
        load(path, env_file=None)
