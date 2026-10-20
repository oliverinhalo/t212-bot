"""Entry/exit rules for the crypto side: trend-following or mean-reversion.

Both reuse ``indicators.compute_signals`` — the same SMA/RSI/volatility/
momentum maths the stock side's local fallback strategy already runs — rather
than inventing a second scoring system. The two strategies read that one
signal differently:

* **trend**: buy when it says bullish with a high-enough score (the same rule
  ``local_strategy.py`` uses to enter a stock); exit when it turns bearish.
* **mean_reversion**: buy when the price is unusually far *below* its own
  recent average (a negative ``z_score`` — oversold), betting on a reversion
  toward the mean rather than on the trend continuing; exit once it has
  reverted back up to (or past) that average.

Stop-loss and take-profit are not decided here — they only need the entry
price and the current price, not a fresh signal, so crypto_engine.py checks
those directly and overrides whatever this module says.
"""

from __future__ import annotations

from decimal import Decimal

from .config import CryptoConfig
from .indicators import TechnicalSignals

Decision = tuple[str, str]  # ("buy" | "sell" | "hold", reason)

_RSI_OVERBOUGHT = Decimal(78)
_RSI_OVERSOLD = Decimal(35)


def decide(signal: TechnicalSignals, cfg: CryptoConfig, *, held: bool) -> Decision:
    """What to do about one pair, given its signal and whether it is held."""
    if cfg.strategy == "mean_reversion":
        return _decide_mean_reversion(signal, cfg, held=held)
    return _decide_trend(signal, cfg, held=held)


def _decide_trend(signal: TechnicalSignals, cfg: CryptoConfig, *, held: bool) -> Decision:
    if not held:
        if (
            signal.trend == "bullish"
            and signal.score >= cfg.min_entry_score
            and (signal.rsi is None or signal.rsi < _RSI_OVERBOUGHT)
            and (signal.vol_annual_pct is None or signal.vol_annual_pct < cfg.max_entry_vol_pct)
        ):
            return "buy", f"trend-following entry: bullish, score {signal.score:+.2f}"
        return "hold", f"not a qualifying trend entry (trend {signal.trend}, score {signal.score:+.2f})"

    if signal.trend == "bearish":
        return "sell", f"trend-following exit: trend turned bearish (score {signal.score:+.2f})"
    return "hold", f"holding: trend is {signal.trend}, not bearish"


def _decide_mean_reversion(signal: TechnicalSignals, cfg: CryptoConfig, *, held: bool) -> Decision:
    if signal.z_score is None:
        return "hold", "not enough history yet for a z-score"

    if not held:
        if signal.z_score <= cfg.entry_z and (signal.rsi is None or signal.rsi < _RSI_OVERSOLD):
            return "buy", f"mean-reversion entry: z-score {signal.z_score:+.2f} (oversold)"
        return "hold", f"not oversold enough (z-score {signal.z_score:+.2f})"

    if signal.z_score >= cfg.exit_z:
        return "sell", f"mean-reversion exit: reverted to z-score {signal.z_score:+.2f}"
    return "hold", f"holding: z-score {signal.z_score:+.2f} has not reverted yet"


__all__ = ["decide"]
