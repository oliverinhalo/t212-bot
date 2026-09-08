"""Local technical analysis over the daily closes we already fetch.

None of this costs an API call. The point is to hand the LLM (and the local
fallback strategy) a compact, deterministic read of each instrument's structure
— trend, momentum, stretch, volatility — instead of a bare list of numbers.

Everything is ``Decimal`` and close-based: Yahoo's daily bars give us closes,
and a hobby bot rebalancing a few times a day has no use for intraday data.
Indicators return ``None`` rather than guessing when the history is too short.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .config import IndicatorConfig
from .models import ZERO, PriceHistory, Quote, WatchItem

_TRADING_DAYS = Decimal(252)

Trend = str  # "bullish" | "neutral" | "bearish"
Regime = str  # "risk_on" | "neutral" | "defensive"


@dataclass(frozen=True)
class TechnicalSignals:
    """A deterministic snapshot of one instrument's recent behaviour."""

    ticker: str
    last: Decimal
    bars: int
    sma_short: Decimal | None
    sma_mid: Decimal | None
    sma_long: Decimal | None
    rsi: Decimal | None
    vol_annual_pct: Decimal | None
    max_drawdown_pct: Decimal | None
    roc_5: Decimal | None
    roc_10: Decimal | None
    roc_20: Decimal | None
    dist_from_high_pct: Decimal | None
    dist_from_low_pct: Decimal | None
    z_score: Decimal | None
    trend: Trend
    score: Decimal  # [-1, 1]; positive = constructive, negative = deteriorating
    notes: tuple[str, ...]

    @property
    def above_mid(self) -> bool:
        return self.sma_mid is not None and self.last > self.sma_mid

    def as_dict(self) -> dict[str, object]:
        """Compact JSON-safe view for the cycle snapshot and the dashboard."""
        return {
            "trend": self.trend,
            "score": f"{self.score:+.2f}",
            "rsi": None if self.rsi is None else f"{self.rsi:.0f}",
            "vol_annual_pct": (
                None if self.vol_annual_pct is None else f"{self.vol_annual_pct:.0f}"
            ),
            "roc_20": None if self.roc_20 is None else f"{self.roc_20:+.1f}",
            "bars": self.bars,
        }


# --------------------------------------------------------------------------- #
# Primitives
# --------------------------------------------------------------------------- #


def _sma(closes: Sequence[Decimal], window: int) -> Decimal | None:
    """Simple mean of the last ``window`` closes, or None if too short."""
    if window <= 0 or len(closes) < window:
        return None
    return sum(closes[-window:], ZERO) / Decimal(window)


def _stdev(values: Sequence[Decimal]) -> Decimal | None:
    """Sample standard deviation (n-1). None for fewer than two points."""
    n = len(values)
    if n < 2:
        return None
    mean = sum(values, ZERO) / Decimal(n)
    variance = sum(((v - mean) ** 2 for v in values), ZERO) / Decimal(n - 1)
    if variance <= ZERO:
        return ZERO
    try:
        return variance.sqrt()
    except InvalidOperation:  # pragma: no cover - defensive
        return None


def _daily_returns(closes: Sequence[Decimal]) -> list[Decimal]:
    out: list[Decimal] = []
    for older, newer in zip(closes, closes[1:]):
        if older > ZERO:
            out.append((newer - older) / older)
    return out


def _rsi(closes: Sequence[Decimal], period: int) -> Decimal | None:
    """Classic RSI over the last ``period`` deltas (simple averaging).

    100 when there were no down days in the window, 0 when there were no up
    days. Uses simple (not Wilder) averaging — the smoothing choice does not
    matter for a coarse trend read and simple is easier to test.
    """
    if period < 1 or len(closes) < period + 1:
        return None
    deltas = [b - a for a, b in zip(closes[-(period + 1):], closes[-period:])]
    gains = sum((d for d in deltas if d > ZERO), ZERO) / Decimal(period)
    losses = sum((-d for d in deltas if d < ZERO), ZERO) / Decimal(period)
    if losses == ZERO:
        return Decimal(100) if gains > ZERO else Decimal(50)
    rs = gains / losses
    return Decimal(100) - (Decimal(100) / (Decimal(1) + rs))


def _roc(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Rate of change over ``n`` bars, in percent."""
    if n < 1 or len(closes) < n + 1:
        return None
    old = closes[-(n + 1)]
    if old <= ZERO:
        return None
    return (closes[-1] - old) / old * Decimal(100)


def _max_drawdown_pct(closes: Sequence[Decimal]) -> Decimal | None:
    """Largest peak-to-trough decline in the series, as a negative percent."""
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = ZERO
    for close in closes:
        if close > peak:
            peak = close
        if peak > ZERO:
            drawdown = (close - peak) / peak * Decimal(100)
            if drawdown < worst:
                worst = drawdown
    return worst


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #


def _clamp(value: Decimal, low: Decimal = Decimal(-1), high: Decimal = Decimal(1)) -> Decimal:
    return max(low, min(high, value))


def classify(
    last: Decimal,
    sma_short: Decimal | None,
    sma_mid: Decimal | None,
    sma_long: Decimal | None,
    rsi: Decimal | None,
    roc_20: Decimal | None,
) -> tuple[Trend, Decimal, list[str]]:
    """Turn the primitives into a trend label and a score in [-1, 1].

    The rules are intentionally shallow and additive so the audit log can be
    read back and understood:

      +0.35  price above the mid SMA           -0.35  below it
      +0.25  short SMA above the long SMA       -0.25  below it
      +0.20  20-day ROC positive               -0.20  negative
      +0.10  RSI in the constructive 45-68 band
      -0.25  RSI > 78 (overbought, mean-reversion risk)
      -0.15  RSI < 25 (falling knife)
    """
    score = ZERO
    notes: list[str] = []

    if sma_mid is not None:
        if last > sma_mid:
            score += Decimal("0.35")
            notes.append("price above mid SMA")
        else:
            score -= Decimal("0.35")
            notes.append("price below mid SMA")

    if sma_short is not None and sma_long is not None:
        if sma_short > sma_long:
            score += Decimal("0.25")
            notes.append("short SMA above long SMA")
        else:
            score -= Decimal("0.25")
            notes.append("short SMA below long SMA")

    if roc_20 is not None:
        if roc_20 > ZERO:
            score += Decimal("0.20")
        else:
            score -= Decimal("0.20")
        notes.append(f"20d ROC {roc_20:+.1f}%")

    if rsi is not None:
        if Decimal(45) <= rsi <= Decimal(68):
            score += Decimal("0.10")
        elif rsi > Decimal(78):
            score -= Decimal("0.25")
            notes.append(f"RSI {rsi:.0f} overbought")
        elif rsi < Decimal(25):
            score -= Decimal("0.15")
            notes.append(f"RSI {rsi:.0f} oversold")

    score = _clamp(score)
    if score >= Decimal("0.35"):
        trend = "bullish"
    elif score <= Decimal("-0.35"):
        trend = "bearish"
    else:
        trend = "neutral"
    return trend, score, notes


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #


def compute_signals(
    history: PriceHistory | None,
    quote: Quote,
    cfg: IndicatorConfig,
) -> TechnicalSignals | None:
    """Full technical read for one ticker, or None when history is too short."""
    closes: list[Decimal] = [bar.close for bar in history.bars] if history else []
    if len(closes) < cfg.min_bars:
        return None

    last = quote.price if quote.price > ZERO else closes[-1]
    # Keep the live quote in the series so momentum reflects the latest tick.
    series = closes if closes[-1] == last else closes + [last]

    sma_short = _sma(series, cfg.sma_short)
    sma_mid = _sma(series, cfg.sma_mid)
    sma_long = _sma(series, cfg.sma_long)
    rsi = _rsi(series, cfg.rsi_period)

    returns = _daily_returns(series)
    daily_vol = _stdev(returns)
    vol_annual_pct = (
        daily_vol * _TRADING_DAYS.sqrt() * Decimal(100) if daily_vol is not None else None
    )

    window = series[-cfg.sma_long:] if len(series) >= cfg.sma_long else series
    high = max(window)
    low = min(window)
    dist_high = (last - high) / high * Decimal(100) if high > ZERO else None
    dist_low = (last - low) / low * Decimal(100) if low > ZERO else None

    z_score = None
    if sma_mid is not None:
        band = _stdev(series[-cfg.sma_mid:])
        if band and band > ZERO:
            z_score = (last - sma_mid) / band

    roc_20 = _roc(series, 20)
    trend, score, notes = classify(last, sma_short, sma_mid, sma_long, rsi, roc_20)

    return TechnicalSignals(
        ticker=quote.ticker,
        last=last,
        bars=len(closes),
        sma_short=sma_short,
        sma_mid=sma_mid,
        sma_long=sma_long,
        rsi=rsi,
        vol_annual_pct=vol_annual_pct,
        max_drawdown_pct=_max_drawdown_pct(series),
        roc_5=_roc(series, 5),
        roc_10=_roc(series, 10),
        roc_20=roc_20,
        dist_from_high_pct=dist_high,
        dist_from_low_pct=dist_low,
        z_score=z_score,
        trend=trend,
        score=score,
        notes=tuple(notes),
    )


def compute_all(
    watchlist: Sequence[WatchItem],
    quotes: Mapping[str, Quote],
    histories: Mapping[str, PriceHistory],
    cfg: IndicatorConfig,
) -> dict[str, TechnicalSignals]:
    out: dict[str, TechnicalSignals] = {}
    for item in watchlist:
        quote = quotes.get(item.ticker)
        if quote is None:
            continue
        signals = compute_signals(histories.get(item.ticker), quote, cfg)
        if signals is not None:
            out[item.ticker] = signals
    return out


def market_regime(
    watchlist: Sequence[WatchItem],
    signals: Mapping[str, TechnicalSignals],
) -> Regime:
    """Coarse risk backdrop, read off the broadest instrument available.

    The first watch-list entry is taken as the market proxy (put a broad
    all-world / S&P tracker first). ``defensive`` when that proxy is below its
    long SMA with negative 20-day momentum; ``risk_on`` when it is clearly
    trending up; ``neutral`` otherwise or when there is no usable proxy.
    """
    proxy = None
    for item in watchlist:
        if item.ticker in signals:
            proxy = signals[item.ticker]
            break
    if proxy is None:
        return "neutral"

    below_long = proxy.sma_long is not None and proxy.last < proxy.sma_long
    weak_mom = proxy.roc_20 is not None and proxy.roc_20 < ZERO
    if below_long and weak_mom:
        return "defensive"
    if proxy.trend == "bullish" and not below_long:
        return "risk_on"
    return "neutral"


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


def _fmt(value: Decimal | None, spec: str = "+.2f") -> str:
    return format(value, spec) if value is not None else "n/a"


def summary_lines(
    signals: Mapping[str, TechnicalSignals],
    regime: Regime,
) -> list[str]:
    """Compact prompt block. Empty when there is nothing to say."""
    if not signals:
        return []
    lines = [f"MARKET REGIME: {regime.replace('_', ' ')}", "TECHNICAL SIGNALS (close-based)"]
    ranked = sorted(signals.values(), key=lambda s: s.score, reverse=True)
    for s in ranked:
        parts = [
            f"trend {s.trend} (score {s.score:+.2f})",
            f"RSI {_fmt(s.rsi, '.0f')}",
            f"vol {_fmt(s.vol_annual_pct, '.0f')}%/yr",
            f"ROC 5/10/20 {_fmt(s.roc_5, '+.1f')}/{_fmt(s.roc_10, '+.1f')}/{_fmt(s.roc_20, '+.1f')}%",
            f"from {s.bars}d high {_fmt(s.dist_from_high_pct, '+.1f')}%",
            f"max DD {_fmt(s.max_drawdown_pct, '.1f')}%",
        ]
        lines.append(f"  {s.ticker}: " + ", ".join(parts))
    lines.append(
        "  ranking (best first): " + " > ".join(s.ticker for s in ranked)
    )
    return lines


__all__ = [
    "TechnicalSignals",
    "classify",
    "compute_signals",
    "compute_all",
    "market_regime",
    "summary_lines",
]
