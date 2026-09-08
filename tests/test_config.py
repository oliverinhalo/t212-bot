"""Config loading, and the guard rails that live in it."""

from __future__ import annotations

import pytest

from t212bot.config import (
    ConfigError,
    LIVE_CONFIRMATION_VAR,
    Secrets,
    load,
    load_watchlist,
    resolve_mode,
    resolve_provider,
)

MINIMAL = """
capital:
  max_capital_gbp: 50.00
  per_trade_cap_pct: 25.0
  max_position_pct: 25.0
  min_order_gbp: 1.00
  cash_buffer_gbp: 0.50
risk:
  daily_loss_limit_pct: 10.0
  max_trades_per_day: 5
ai:
  provider: stub
watchlist:
  - ticker: VUSAl_EQ
    yahoo: VUSA.L
    name: Vanguard S&P 500
"""


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in (
        "MODE",
        LIVE_CONFIRMATION_VAR,
        "T212_API_KEY",
        "T212_API_SECRET",
        "T212_AUTH_SCHEME",
        "OPENROUTER_API_KEY",
        "OMNIROUTE_API_KEY",
        "OMNIROUTE_BASE_URL",
        "ANTHROPIC_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def write_config(tmp_path, text=MINIMAL):
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return path


# --------------------------------------------------------------------------- #
# Mode
# --------------------------------------------------------------------------- #


def test_mode_defaults_to_paper():
    assert resolve_mode() == "paper"


def test_live_mode_refuses_without_the_confirmation(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    with pytest.raises(ConfigError, match="real money"):
        resolve_mode()


def test_live_mode_refuses_a_near_miss_confirmation(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv(LIVE_CONFIRMATION_VAR, "y")
    with pytest.raises(ConfigError):
        resolve_mode()


def test_live_mode_starts_with_the_exact_confirmation(monkeypatch):
    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv(LIVE_CONFIRMATION_VAR, "yes")
    assert resolve_mode() == "live"


def test_unknown_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("MODE", "yolo")
    with pytest.raises(ConfigError):
        resolve_mode()


def test_demo_mode_does_not_need_the_confirmation(monkeypatch):
    monkeypatch.setenv("MODE", "demo")
    assert resolve_mode() == "demo"


def test_base_url_follows_the_mode(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    assert "demo.trading212.com" in load(path, env_file=None).t212_base_url

    monkeypatch.setenv("MODE", "live")
    monkeypatch.setenv(LIVE_CONFIRMATION_VAR, "yes")
    monkeypatch.setenv("T212_API_KEY", "k")
    assert "live.trading212.com" in load(path, env_file=None).t212_base_url


def test_non_paper_mode_requires_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("MODE", "demo")
    with pytest.raises(ConfigError, match="T212_API_KEY"):
        load(write_config(tmp_path), env_file=None)


# --------------------------------------------------------------------------- #
# Provider selection
# --------------------------------------------------------------------------- #


def test_auto_prefers_omniroute_when_its_key_is_set():
    secrets = Secrets(omniroute_api_key="m", anthropic_api_key="a", openrouter_api_key="o")
    assert resolve_provider("auto", secrets) == "omniroute"


def test_auto_falls_back_to_anthropic_then_openrouter():
    assert resolve_provider("auto", Secrets(anthropic_api_key="a", openrouter_api_key="o")) == "anthropic"
    assert resolve_provider("auto", Secrets(openrouter_api_key="o")) == "openrouter"


def test_pinning_omniroute_without_its_key_is_an_error():
    with pytest.raises(ConfigError, match="OMNIROUTE_API_KEY"):
        resolve_provider("omniroute", Secrets())


def test_auto_with_no_keys_is_an_error():
    with pytest.raises(ConfigError, match="stub"):
        resolve_provider("auto", Secrets())


def test_pinning_a_provider_without_its_key_is_an_error():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        resolve_provider("anthropic", Secrets(openrouter_api_key="o"))


def test_stub_needs_no_key():
    assert resolve_provider("stub", Secrets()) == "stub"


# --------------------------------------------------------------------------- #
# Watch-list
# --------------------------------------------------------------------------- #


def test_watchlist_must_not_be_empty():
    with pytest.raises(ConfigError, match="nothing it may trade"):
        load_watchlist([])


def test_watchlist_entries_need_a_ticker_and_a_symbol():
    with pytest.raises(ConfigError):
        load_watchlist([{"ticker": "X"}])


def test_duplicate_tickers_are_rejected():
    entries = [
        {"ticker": "X", "yahoo": "X.L"},
        {"ticker": "X", "yahoo": "X.L"},
    ]
    with pytest.raises(ConfigError, match="duplicate"):
        load_watchlist(entries)


def test_per_ticker_override_must_be_a_percentage():
    with pytest.raises(ConfigError):
        load_watchlist([{"ticker": "X", "yahoo": "X.L", "max_position_pct": 500}])


# --------------------------------------------------------------------------- #
# Cap validation
# --------------------------------------------------------------------------- #


def test_impossible_cap_combination_is_rejected(tmp_path):
    text = MINIMAL.replace("min_order_gbp: 1.00", "min_order_gbp: 20.00")
    with pytest.raises(ConfigError, match="no order could ever be approved"):
        load(write_config(tmp_path, text), env_file=None)


def test_zero_capital_is_rejected(tmp_path):
    text = MINIMAL.replace("max_capital_gbp: 50.00", "max_capital_gbp: 0")
    with pytest.raises(ConfigError, match="greater than zero"):
        load(write_config(tmp_path, text), env_file=None)


def test_a_cap_over_one_hundred_percent_is_rejected(tmp_path):
    text = MINIMAL.replace("per_trade_cap_pct: 25.0", "per_trade_cap_pct: 150")
    with pytest.raises(ConfigError, match="percentage"):
        load(write_config(tmp_path, text), env_file=None)


def test_a_missing_config_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="config.yaml.example"):
        load(tmp_path / "nope.yaml", env_file=None)


# --------------------------------------------------------------------------- #
# Derived values
# --------------------------------------------------------------------------- #


def test_derived_caps(tmp_path):
    config = load(write_config(tmp_path), env_file=None)
    assert config.capital.per_trade_cap == 125 / 10  # £12.50
    assert config.capital.max_position_value == 125 / 10
    assert config.risk.daily_loss_limit(config.capital.max_capital) == -5


def test_money_amounts_are_decimals_not_floats(tmp_path):
    from decimal import Decimal

    config = load(write_config(tmp_path), env_file=None)
    assert isinstance(config.capital.max_capital, Decimal)
    assert config.capital.max_capital == Decimal("50.00")


def test_paper_mode_does_not_place_real_orders(tmp_path):
    config = load(write_config(tmp_path), env_file=None)
    assert config.mode == "paper"
    assert not config.places_real_orders
    assert not config.is_live


def test_secrets_are_not_rendered(tmp_path):
    secrets = Secrets(t212_api_key="super-secret", anthropic_api_key="also-secret")
    assert "super-secret" not in repr(secrets)
    assert "redacted" in repr(secrets)


def test_config_repr_does_not_leak_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret-value")
    text = MINIMAL.replace("provider: stub", "provider: openrouter")
    config = load(write_config(tmp_path, text), env_file=None)
    assert "sk-or-secret-value" not in repr(config)


# --------------------------------------------------------------------------- #
# Open-universe toggle
# --------------------------------------------------------------------------- #


def test_enforce_allowlist_defaults_to_true(tmp_path):
    config = load(write_config(tmp_path), env_file=None)
    assert config.risk.enforce_allowlist is True


def _open_universe_config(tmp_path) -> str:
    return MINIMAL.replace(
        "  max_trades_per_day: 5",
        "  max_trades_per_day: 5\n  enforce_allowlist: false",
    ).replace(
        "ai:\n  provider: stub",
        f"ai:\n  provider: stub\nstorage:\n  db_path: {tmp_path / 'data' / 't.sqlite3'}",
    )


def test_disabling_the_allowlist_needs_the_instrument_catalogue(tmp_path):
    (tmp_path / "data").mkdir()
    with pytest.raises(ConfigError, match="instrument catalogue"):
        load(write_config(tmp_path, _open_universe_config(tmp_path)), env_file=None)


def test_disabling_the_allowlist_works_when_the_catalogue_is_present(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "instruments.json").write_text("[]")
    config = load(write_config(tmp_path, _open_universe_config(tmp_path)), env_file=None)
    assert config.risk.enforce_allowlist is False
    assert config.instruments_path == tmp_path / "data" / "instruments.json"


# --------------------------------------------------------------------------- #
# OmniRoute provider
# --------------------------------------------------------------------------- #


def test_omniroute_provider_needs_a_model(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "k")
    text = MINIMAL.replace(
        "ai:\n  provider: stub",
        "ai:\n  provider: omniroute\n  omniroute:\n    base_url: http://127.0.0.1:20128/v1",
    )
    with pytest.raises(ConfigError, match="ai.omniroute.model"):
        load(write_config(tmp_path, text), env_file=None)


def test_omniroute_provider_needs_a_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "k")
    text = MINIMAL.replace(
        "ai:\n  provider: stub",
        "ai:\n  provider: omniroute\n  omniroute:\n    model: default",
    )
    with pytest.raises(ConfigError, match="ai.omniroute.base_url"):
        load(write_config(tmp_path, text), env_file=None)


def test_omniroute_provider_loads_with_model_and_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "k")
    text = MINIMAL.replace(
        "ai:\n  provider: stub",
        "ai:\n  provider: omniroute\n  omniroute:\n    base_url: http://127.0.0.1:20128/v1\n"
        "    model: default",
    )
    config = load(write_config(tmp_path, text), env_file=None)
    assert config.ai.provider == "omniroute"
    assert config.ai.omniroute_base_url == "http://127.0.0.1:20128/v1"
    assert config.ai.omniroute_models == ("default",)


def test_omniroute_base_url_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIROUTE_API_KEY", "k")
    monkeypatch.setenv("OMNIROUTE_BASE_URL", "https://omni.jacoblevy.co.uk/v1")
    text = MINIMAL.replace(
        "ai:\n  provider: stub",
        "ai:\n  provider: omniroute\n  omniroute:\n    base_url: http://127.0.0.1:20128/v1\n"
        "    model: default",
    )
    config = load(write_config(tmp_path, text), env_file=None)
    assert config.ai.omniroute_base_url == "https://omni.jacoblevy.co.uk/v1"


def test_symbol_overrides_are_parsed(tmp_path):
    text = MINIMAL.replace(
        "watchlist:",
        "market_data:\n  symbol_overrides:\n    AAPL_US_EQ: AAPL\nwatchlist:",
    )
    config = load(write_config(tmp_path, text), env_file=None)
    assert config.symbol_overrides["AAPL_US_EQ"] == "AAPL"
