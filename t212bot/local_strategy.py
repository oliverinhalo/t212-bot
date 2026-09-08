"""A deterministic, rule-based advisor used when the LLM cannot be.

This exists so the bot is never blind: if OpenRouter is down, refusing, or the
daily free-tier budget is spent, ``advise_locally`` still produces a proposal
that flows through the *exact same* risk pipeline as an LLM reply. It emits a
JSON object identical in shape to the model contract in ``ai_advisor`` and runs
it back through ``parse_proposal`` so the audit trail is uniform.

The rules are conservative and capital-preservation first. One action per
cycle, evaluated in priority order:

    stop-loss  ->  trend exit  ->  take-profit  ->  cautious entry  ->  hold
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Mapping

from .config import AppConfig
from .indicators import Regime, TechnicalSignals
from .market_data import Snapshot
from .models import ZERO, AccountState, AIResult, money
from .ai_advisor import parse_proposal

MODEL = "rules-v1"


def _result(prompt_lines: list[str], payload: dict, started: float) -> AIResult:
    raw = json.dumps(payload)
    return AIResult(
        proposal=parse_proposal(raw),
        provider="local",
        model=MODEL,
        prompt="\n".join(prompt_lines),
        raw_response=raw,
        latency_ms=int((time.monotonic() - started) * 1000),
        error=None,
        http_calls=0,
    )


def _hold(prompt_lines: list[str], started: float, reasoning: str) -> AIResult:
    return _result(
        prompt_lines,
        {
            "action": "hold",
            "ticker": None,
            "notional_or_qty": None,
            "size_unit": "gbp",
            "price": None,
            "confidence": 1.0,
            "reasoning": reasoning[:280],
        },
        started,
    )


def advise_locally(
    config: AppConfig,
    account: AccountState,
    snapshot: Snapshot,
    signals: Mapping[str, TechnicalSignals],
    regime: Regime,
    *,
    trades_today: int,
    day_pnl: Decimal,
) -> AIResult:
    """Run the fallback strategy once. Never raises; degrades to ``hold``."""
    started = time.monotonic()
    strat = config.ai.local_strategy
    lines = [
        "LOCAL RULE-BASED ADVISOR (LLM unavailable or budget spent)",
        f"regime={regime} cash={money(account.cash)} equity={money(account.equity)} "
        f"day_pnl={money(day_pnl)} trades_today={trades_today}",
        f"thresholds: stop -{strat.stop_loss_pct}% / take +{strat.take_profit_pct}% / "
        f"max entry vol {strat.max_entry_vol_pct}%/yr / min score {strat.min_entry_score}",
    ]

    allowed = config.allowed_tickers

    # 1 + 2 + 3: anything we already hold, worst case first.
    for position in sorted(
        account.positions, key=lambda p: p.unrealised_pnl / (p.value or Decimal(1))
    ):
        if position.ticker not in allowed:
            continue
        quote = snapshot.quotes.get(position.ticker)
        if quote is None or quote.price <= ZERO:
            continue
        cost = position.average_price * position.quantity
        pnl_pct = (
            (position.value - cost) / cost * Decimal(100) if cost > ZERO else ZERO
        )
        sig = signals.get(position.ticker)
        lines.append(
            f"  hold {position.ticker}: pnl {pnl_pct:+.1f}% "
            f"trend {sig.trend if sig else 'n/a'}"
        )

        if pnl_pct <= -strat.stop_loss_pct:
            lines.append("  -> STOP-LOSS: full exit")
            return _result(
                lines,
                _payload("sell", position.ticker, None, None, quote.price, 0.9,
                         f"Stop-loss: {position.ticker} at {pnl_pct:+.1f}% vs cost."),
                started,
            )

        if (
            strat.trend_exit
            and sig is not None
            and sig.trend == "bearish"
            and sig.sma_mid is not None
            and quote.price < sig.sma_mid
        ):
            lines.append("  -> TREND EXIT: full exit")
            return _result(
                lines,
                _payload("sell", position.ticker, None, None, quote.price, 0.7,
                         f"Trend exit: {position.ticker} bearish and below mid SMA."),
                started,
            )

        if pnl_pct >= strat.take_profit_pct:
            take = money(position.value / 2)
            if take >= config.capital.min_order:
                lines.append(f"  -> TAKE-PROFIT: trim ~{take}")
                return _result(
                    lines,
                    _payload("sell", position.ticker, float(take), "gbp",
                             quote.price, 0.65,
                             f"Take-profit: trim half of {position.ticker} at {pnl_pct:+.1f}%."),
                    started,
                )

    # 4: a single cautious entry, only in a non-defensive backdrop.
    if regime == "defensive":
        return _hold(lines, started, "Defensive regime: no new positions.")
    if trades_today >= config.risk.max_trades_per_day:
        return _hold(lines, started, "Daily trade budget spent.")

    spendable = account.cash - config.capital.cash_buffer
    if spendable < config.capital.min_order:
        return _hold(lines, started, "No spendable cash for an entry.")

    candidates = [
        sig
        for ticker, sig in signals.items()
        if ticker in allowed
        and (account.value_of(ticker) + config.capital.min_order <= config.position_cap_for(ticker))
        and sig.trend == "bullish"
        and sig.score >= strat.min_entry_score
        and (sig.rsi is None or sig.rsi < Decimal(78))
        and (sig.vol_annual_pct is None or sig.vol_annual_pct < strat.max_entry_vol_pct)
        and ticker in snapshot.quotes
    ]
    if not candidates:
        return _hold(lines, started, "No instrument meets the entry rules.")

    best = max(candidates, key=lambda s: s.score)
    headroom = config.position_cap_for(best.ticker) - account.value_of(best.ticker)
    notional = money(min(spendable, config.capital.per_trade_cap, max(ZERO, headroom)))
    if notional < config.capital.min_order:
        return _hold(lines, started, "Remaining position headroom or spendable cash below min order.")
    confidence = min(Decimal("0.9"), Decimal("0.6") + Decimal("0.3") * best.score)
    rsi_note = f", RSI {best.rsi:.0f}" if best.rsi is not None else ""
    reasoning = f"Entry: {best.ticker} bullish (score {best.score:+.2f}){rsi_note}."
    lines.append(
        f"  -> ENTRY: buy ~{notional} {best.ticker} (score {best.score:+.2f})"
    )
    return _result(
        lines,
        _payload("buy", best.ticker, float(notional), "gbp",
                 float(best.last), round(float(confidence), 2), reasoning),
        started,
    )


def _payload(
    action: str,
    ticker: str | None,
    size: float | None,
    unit: str | None,
    price: float | Decimal | None,
    confidence: float,
    reasoning: str,
) -> dict:
    return {
        "action": action,
        "ticker": ticker,
        "notional_or_qty": size,
        "size_unit": unit or "gbp",
        "price": float(price) if price is not None else None,
        "confidence": confidence,
        "reasoning": reasoning[:280],
    }


__all__ = ["advise_locally", "MODEL"]
