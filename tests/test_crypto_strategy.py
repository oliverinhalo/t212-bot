"""The trend-following and mean-reversion entry/exit rules."""

from __future__ import annotations

from decimal import Decimal

from t212bot.config import CryptoConfig
from t212bot.crypto_strategy import decide
from t212bot.indicators import TechnicalSignals
from t212bot.models import dec


def _signal(*, trend="neutral", score="0", rsi=None, vol=None, z=None) -> TechnicalSignals:
    return TechnicalSignals(
        ticker="BTC/USDT", last=dec(100), bars=60,
        sma_short=None, sma_mid=None, sma_long=None,
        rsi=dec(rsi) if rsi is not None else None,
        vol_annual_pct=dec(vol) if vol is not None else None,
        max_drawdown_pct=None, roc_5=None, roc_10=None, roc_20=None,
        dist_from_high_pct=None, dist_from_low_pct=None,
        z_score=dec(z) if z is not None else None,
        trend=trend, score=dec(score), notes=(),
    )


def _cfg(**kwargs) -> CryptoConfig:
    return CryptoConfig(**kwargs)


# --------------------------------------------------------------------------- #
# trend
# --------------------------------------------------------------------------- #


def test_trend_buys_a_qualifying_bullish_entry():
    cfg = _cfg(strategy="trend", min_entry_score=Decimal("0.35"), max_entry_vol_pct=Decimal(80))
    action, _ = decide(_signal(trend="bullish", score="0.5", rsi=60, vol=20), cfg, held=False)
    assert action == "buy"


def test_trend_does_not_buy_a_neutral_signal():
    cfg = _cfg(strategy="trend")
    action, _ = decide(_signal(trend="neutral", score="0.5", rsi=60, vol=20), cfg, held=False)
    assert action == "hold"


def test_trend_does_not_buy_below_the_minimum_score():
    cfg = _cfg(strategy="trend", min_entry_score=Decimal("0.35"))
    action, _ = decide(_signal(trend="bullish", score="0.2", rsi=60, vol=20), cfg, held=False)
    assert action == "hold"


def test_trend_does_not_buy_an_overbought_pair():
    cfg = _cfg(strategy="trend", min_entry_score=Decimal("0.35"))
    action, _ = decide(_signal(trend="bullish", score="0.5", rsi=80, vol=20), cfg, held=False)
    assert action == "hold"


def test_trend_does_not_buy_when_volatility_is_too_high():
    cfg = _cfg(strategy="trend", min_entry_score=Decimal("0.35"), max_entry_vol_pct=Decimal(50))
    action, _ = decide(_signal(trend="bullish", score="0.5", rsi=60, vol=90), cfg, held=False)
    assert action == "hold"


def test_trend_never_buys_when_already_held():
    cfg = _cfg(strategy="trend")
    action, _ = decide(_signal(trend="bullish", score="0.9"), cfg, held=True)
    assert action == "hold"


def test_trend_exits_a_held_position_once_bearish():
    cfg = _cfg(strategy="trend")
    action, reason = decide(_signal(trend="bearish", score="-0.5"), cfg, held=True)
    assert action == "sell"
    assert "bearish" in reason


def test_trend_holds_a_position_that_is_merely_neutral():
    cfg = _cfg(strategy="trend")
    action, _ = decide(_signal(trend="neutral", score="0"), cfg, held=True)
    assert action == "hold"


# --------------------------------------------------------------------------- #
# mean reversion
# --------------------------------------------------------------------------- #


def test_mean_reversion_buys_an_oversold_dip():
    cfg = _cfg(strategy="mean_reversion", entry_z=Decimal("-1.5"))
    action, _ = decide(_signal(z=-2, rsi=20), cfg, held=False)
    assert action == "buy"


def test_mean_reversion_does_not_buy_without_being_oversold_enough():
    cfg = _cfg(strategy="mean_reversion", entry_z=Decimal("-1.5"))
    action, _ = decide(_signal(z=-0.5, rsi=50), cfg, held=False)
    assert action == "hold"


def test_mean_reversion_does_not_buy_with_no_z_score_yet():
    cfg = _cfg(strategy="mean_reversion")
    action, reason = decide(_signal(z=None), cfg, held=False)
    assert action == "hold"
    assert "z-score" in reason


def test_mean_reversion_requires_oversold_rsi_too():
    cfg = _cfg(strategy="mean_reversion", entry_z=Decimal("-1.5"))
    action, _ = decide(_signal(z=-2, rsi=60), cfg, held=False)
    assert action == "hold"


def test_mean_reversion_exits_once_reverted():
    cfg = _cfg(strategy="mean_reversion", exit_z=Decimal(0))
    action, reason = decide(_signal(z=0.2), cfg, held=True)
    assert action == "sell"
    assert "reverted" in reason


def test_mean_reversion_holds_until_reverted():
    cfg = _cfg(strategy="mean_reversion", exit_z=Decimal(0))
    action, _ = decide(_signal(z=-0.8), cfg, held=True)
    assert action == "hold"
