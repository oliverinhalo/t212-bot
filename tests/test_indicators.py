"""Local technical analysis: the primitives, the classifier, and the regime."""

from __future__ import annotations

from datetime import date, timedelta

from t212bot.config import IndicatorConfig
from t212bot.indicators import (
    _max_drawdown_pct,
    _rsi,
    _sma,
    _roc,
    classify,
    compute_signals,
    market_regime,
    summary_lines,
)
from t212bot.models import Bar, PriceHistory, Quote, WatchItem, dec, utcnow

CFG = IndicatorConfig()


def _history(closes, ticker="X") -> PriceHistory:
    start = date(2026, 1, 1)
    return PriceHistory(
        ticker=ticker,
        bars=tuple(Bar(day=start + timedelta(days=i), close=dec(c)) for i, c in enumerate(closes)),
    )


def _quote(price, ticker="X") -> Quote:
    return Quote(ticker=ticker, price=dec(price), currency="GBP", as_of=utcnow(), source="t")


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def test_sma_is_the_mean_of_the_last_window():
    assert _sma([dec(1), dec(2), dec(3), dec(4)], 2) == dec("3.5")
    assert _sma([dec(1), dec(2), dec(3), dec(4)], 4) == dec("2.5")
    assert _sma([dec(1), dec(2)], 5) is None


def test_roc_is_percent_change_over_n_bars():
    assert _roc([dec(10), dec(11), dec(12)], 2) == dec(20)
    assert _roc([dec(10)], 2) is None


def test_rsi_is_100_for_a_pure_uptrend_and_0_for_a_pure_downtrend():
    up = [dec(10) + dec(i) for i in range(20)]
    down = [dec(40) - dec(i) for i in range(20)]
    assert _rsi(up, 14) == dec(100)
    assert _rsi(down, 14) == dec(0)


def test_rsi_of_a_flat_series_is_neutral():
    assert _rsi([dec(10)] * 20, 14) == dec(50)


def test_max_drawdown_is_the_worst_peak_to_trough():
    # 10 -> 12 (peak) -> 9  ==  -25%
    dd = _max_drawdown_pct([dec(10), dec(12), dec(9), dec(11)])
    assert dd == dec(-25)


# --------------------------------------------------------------------------- #
# Classifier
# --------------------------------------------------------------------------- #


def test_classify_calls_a_clean_uptrend_bullish():
    trend, score, _ = classify(
        last=dec(120), sma_short=dec(115), sma_mid=dec(110), sma_long=dec(100),
        rsi=dec(60), roc_20=dec(8),
    )
    assert trend == "bullish"
    assert score > 0


def test_classify_calls_a_clean_downtrend_bearish():
    trend, score, _ = classify(
        last=dec(80), sma_short=dec(85), sma_mid=dec(90), sma_long=dec(100),
        rsi=dec(35), roc_20=dec(-9),
    )
    assert trend == "bearish"
    assert score < 0


def test_classify_penalises_an_overbought_rsi():
    _, hot, _ = classify(dec(120), dec(115), dec(110), dec(100), dec(85), dec(8))
    _, calm, _ = classify(dec(120), dec(115), dec(110), dec(100), dec(60), dec(8))
    assert hot < calm


# --------------------------------------------------------------------------- #
# compute_signals
# --------------------------------------------------------------------------- #


def test_short_history_yields_no_signals():
    assert compute_signals(_history([10] * 10), _quote(10), CFG) is None
    assert compute_signals(None, _quote(10), CFG) is None


def test_full_signal_read_on_a_rising_series():
    closes = [10 + i * 0.05 for i in range(60)]
    sig = compute_signals(_history(closes), _quote(13), CFG)
    assert sig is not None
    assert sig.trend == "bullish"
    assert sig.sma_short > sig.sma_long
    assert sig.roc_20 is not None and sig.roc_20 > 0
    assert sig.max_drawdown_pct is not None and sig.max_drawdown_pct <= 0
    assert "trend" in sig.as_dict()


def test_signals_use_the_live_quote_as_the_latest_point():
    closes = [10.0] * 60
    flat = compute_signals(_history(closes), _quote(10), CFG)
    popped = compute_signals(_history(closes), _quote(12), CFG)
    assert popped.roc_5 > flat.roc_5


# --------------------------------------------------------------------------- #
# Regime + rendering
# --------------------------------------------------------------------------- #


def test_regime_is_defensive_when_the_proxy_is_weak():
    watch = [WatchItem(ticker="BROAD", yahoo="B", name="broad")]
    falling = [30 - i * 0.2 for i in range(60)]
    sig = {"BROAD": compute_signals(_history(falling, "BROAD"), _quote(18, "BROAD"), CFG)}
    assert market_regime(watch, sig) == "defensive"


def test_regime_is_risk_on_when_the_proxy_trends_up():
    watch = [WatchItem(ticker="BROAD", yahoo="B", name="broad")]
    rising = [10 + i * 0.1 for i in range(60)]
    sig = {"BROAD": compute_signals(_history(rising, "BROAD"), _quote(16, "BROAD"), CFG)}
    assert market_regime(watch, sig) == "risk_on"


def test_regime_is_neutral_with_no_usable_proxy():
    assert market_regime([WatchItem(ticker="X", yahoo="x", name="x")], {}) == "neutral"


def test_summary_lines_are_empty_without_signals():
    assert summary_lines({}, "neutral") == []


def test_summary_lines_rank_best_first():
    watch_a = compute_signals(_history([10 + i * 0.1 for i in range(60)], "A"), _quote(16, "A"), CFG)
    watch_b = compute_signals(_history([20 - i * 0.1 for i in range(60)], "B"), _quote(14, "B"), CFG)
    lines = summary_lines({"A": watch_a, "B": watch_b}, "risk_on")
    assert any("ranking (best first): A > B" in ln for ln in lines)
