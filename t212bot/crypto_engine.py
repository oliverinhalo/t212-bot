"""One crypto cycle, and the background loop the dashboard starts and stops.

Deliberately much smaller than main.py's cycle: no AI, no circuit breaker, no
account statement from a broker — just OHLCV candles, the same technical-
analysis maths the stock side already uses (indicators.py), and a position
ledger of its own in storage.py. The one thing it shares with the stock side
is the kill switch: ``config.stop_file`` halts both.

A position is opened by exactly one buy and closed by exactly one sell — no
averaging in, no partial exits — so crypto_strategy.decide() never has to
reason about a partially-filled position.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal

from .config import AppConfig, CryptoConfig
from .crypto_client import CcxtClient, CryptoError
from .crypto_strategy import decide
from .indicators import TechnicalSignals, compute_signals
from .models import ZERO, Bar, PriceHistory, Quote, dec, money, utcnow
from .storage import Storage

log = logging.getLogger(__name__)

_TIMEFRAME = "1d"


def _floor(quantity: Decimal, decimals: int) -> Decimal:
    if quantity <= ZERO:
        return ZERO
    step = Decimal(1).scaleb(-decimals)
    return quantity.quantize(step, rounding=ROUND_FLOOR)


def build_client(config: AppConfig) -> CcxtClient:
    return CcxtClient(
        config.crypto.exchange,
        api_key=config.secrets.crypto_api_key,
        api_secret=config.secrets.crypto_api_secret,
        timeout_seconds=config.market_data.timeout_seconds,
    )


@dataclass
class CryptoRuntime:
    config: AppConfig
    storage: Storage
    client: CcxtClient

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("error closing the crypto client", exc_info=True)


def build_runtime(config: AppConfig, storage: Storage) -> CryptoRuntime:
    runtime = CryptoRuntime(config=config, storage=storage, client=build_client(config))
    if config.crypto.mode == "paper":
        storage.seed_crypto_cash(config.crypto.max_capital)
    return runtime


def fetch_signal(client: CcxtClient, symbol: str, cfg: CryptoConfig) -> tuple[TechnicalSignals | None, Quote | None]:
    """The latest technical read for one pair, and the quote it was computed from."""
    raw = client.fetch_ohlcv(symbol, _TIMEFRAME, limit=cfg.indicators.bars_needed + 10)
    if not raw:
        return None, None
    bars = tuple(
        Bar(day=datetime.fromtimestamp(row[0] / 1000, tz=timezone.utc).date(), close=dec(row[4]))
        for row in raw
    )
    history = PriceHistory(ticker=symbol, bars=bars)
    quote = Quote(
        ticker=symbol,
        price=bars[-1].close,
        currency=cfg.quote_currency,
        as_of=utcnow(),
        source=f"ccxt:{cfg.exchange}",
        fetched_at=utcnow(),
    )
    return compute_signals(history, quote, cfg.indicators), quote


# --------------------------------------------------------------------------- #
# Sizing and execution
# --------------------------------------------------------------------------- #


def _deployed_capital(storage: Storage) -> Decimal:
    """Cost basis of every open crypto position — never marked to market, so
    a rally never makes room look tighter than what was actually spent."""
    return sum((qty * avg for qty, avg, _ in storage.crypto_positions().values()), ZERO)


def _available_cash(runtime: CryptoRuntime) -> Decimal:
    cfg = runtime.config.crypto
    if cfg.mode == "paper":
        return runtime.storage.crypto_cash()
    try:
        return runtime.client.fetch_balance(cfg.quote_currency)
    except CryptoError as exc:
        log.warning("could not fetch live %s balance: %s", cfg.quote_currency, exc)
        return ZERO


def _buy(runtime: CryptoRuntime, symbol: str, price: Decimal, reason: str) -> dict:
    cfg = runtime.config.crypto
    storage = runtime.storage

    if len(storage.crypto_positions()) >= cfg.max_positions:
        return {"symbol": symbol, "action": "skipped", "reason": "max concurrent positions reached"}

    deployed = _deployed_capital(storage)
    cash = _available_cash(runtime)
    spendable = min(cfg.per_trade_cap, cash, cfg.max_capital - deployed)
    if spendable < cfg.min_order:
        return {
            "symbol": symbol,
            "action": "skipped",
            "reason": f"only {money(spendable)} {cfg.quote_currency} available, "
            f"below the {money(cfg.min_order)} minimum",
        }

    quantity = _floor(spendable / price, cfg.quantity_decimals)
    if quantity <= ZERO:
        return {"symbol": symbol, "action": "skipped", "reason": "rounds to zero at this precision"}
    notional = quantity * price

    order_id = None
    error = None
    if cfg.mode == "paper":
        fill_price = price
        storage.adjust_crypto_cash(-notional)
    else:
        try:
            response = runtime.client.create_market_order(symbol, "buy", quantity)
        except CryptoError as exc:
            storage.record_crypto_trade(
                mode=cfg.mode, symbol=symbol, side="buy", quantity=quantity, price=price,
                notional=notional, strategy=cfg.strategy, reason=reason, error=str(exc),
            )
            log.error("crypto buy of %s %s failed: %s", symbol, quantity, exc)
            return {"symbol": symbol, "action": "buy", "state": "error", "error": str(exc)}
        fill_price = dec(response.get("average") or response.get("price") or price)
        order_id = str(response.get("id")) if response.get("id") is not None else None

    storage.open_crypto_position(symbol, quantity, fill_price)
    storage.record_crypto_trade(
        mode=cfg.mode, symbol=symbol, side="buy", quantity=quantity, price=fill_price,
        notional=quantity * fill_price, strategy=cfg.strategy, reason=reason, order_id=order_id,
    )
    log.warning(
        "CRYPTO BUY (%s): %s %s at %s (~%s) — %s",
        cfg.mode, quantity, symbol, money(fill_price), money(quantity * fill_price), reason,
    )
    return {
        "symbol": symbol, "action": "buy", "state": "filled",
        "quantity": str(quantity), "price": str(fill_price), "reason": reason,
    }


def _sell(runtime: CryptoRuntime, symbol: str, price: Decimal, reason: str) -> dict:
    cfg = runtime.config.crypto
    storage = runtime.storage
    position = storage.crypto_position(symbol)
    if position is None:  # pragma: no cover - defensive, caller already checked
        return {"symbol": symbol, "action": "skipped", "reason": "no position to sell"}
    quantity, average_price, _ = position

    order_id = None
    if cfg.mode == "paper":
        fill_price = price
        storage.adjust_crypto_cash(quantity * fill_price)
    else:
        try:
            response = runtime.client.create_market_order(symbol, "sell", quantity)
        except CryptoError as exc:
            storage.record_crypto_trade(
                mode=cfg.mode, symbol=symbol, side="sell", quantity=quantity, price=price,
                notional=quantity * price, strategy=cfg.strategy, reason=reason, error=str(exc),
            )
            log.error("crypto sell of %s %s failed: %s", symbol, quantity, exc)
            return {"symbol": symbol, "action": "sell", "state": "error", "error": str(exc)}
        fill_price = dec(response.get("average") or response.get("price") or price)
        order_id = str(response.get("id")) if response.get("id") is not None else None

    realised = (fill_price - average_price) * quantity
    storage.close_crypto_position(symbol)
    storage.record_crypto_trade(
        mode=cfg.mode, symbol=symbol, side="sell", quantity=quantity, price=fill_price,
        notional=quantity * fill_price, strategy=cfg.strategy, reason=reason, order_id=order_id,
        realised_pnl=realised,
    )
    log.warning(
        "CRYPTO SELL (%s): %s %s at %s (~%s), realised %s — %s",
        cfg.mode, quantity, symbol, money(fill_price), money(quantity * fill_price),
        money(realised), reason,
    )
    return {
        "symbol": symbol, "action": "sell", "state": "filled",
        "quantity": str(quantity), "price": str(fill_price), "realised_pnl": str(realised),
        "reason": reason,
    }


# --------------------------------------------------------------------------- #
# The cycle
# --------------------------------------------------------------------------- #


def run_crypto_cycle(runtime: CryptoRuntime) -> dict:
    """One pass over every configured pair. Never raises — errors are per-pair."""
    config, storage = runtime.config, runtime.storage
    cfg = config.crypto
    result: dict = {"started_at": utcnow().isoformat(), "actions": [], "errors": []}

    if config.stop_file.exists():
        result["halted"] = f"kill switch present: {config.stop_file} — delete it to resume"
        return result

    trades_today = storage.crypto_trades_today()
    for symbol in cfg.pairs:
        try:
            signal, quote = fetch_signal(runtime.client, symbol, cfg)
        except CryptoError as exc:
            result["errors"].append(f"{symbol}: {exc}")
            continue
        if signal is None or quote is None:
            result["errors"].append(f"{symbol}: not enough candles yet")
            continue

        position = storage.crypto_position(symbol)
        held = position is not None
        action, reason = decide(signal, cfg, held=held)

        if held:
            quantity, average_price, _ = position
            change_pct = (quote.price - average_price) / average_price * Decimal(100)
            if change_pct <= -cfg.stop_loss_pct:
                action, reason = "sell", f"stop-loss: {change_pct:+.1f}%"
            elif change_pct >= cfg.take_profit_pct:
                action, reason = "sell", f"take-profit: {change_pct:+.1f}%"

        if action == "hold":
            result["actions"].append({"symbol": symbol, "action": "hold", "reason": reason})
            continue
        if action == "buy" and trades_today >= cfg.max_trades_per_day:
            result["actions"].append(
                {"symbol": symbol, "action": "skipped", "reason": "daily trade cap reached"}
            )
            continue

        outcome = _buy(runtime, symbol, quote.price, reason) if action == "buy" else _sell(
            runtime, symbol, quote.price, reason
        )
        result["actions"].append(outcome)
        if outcome.get("state") == "filled":
            trades_today += 1

    return result


def safe_crypto_cycle(runtime: CryptoRuntime) -> dict:
    try:
        return run_crypto_cycle(runtime)
    except Exception as exc:  # noqa: BLE001 - the loop must survive a bad cycle
        log.exception("crypto cycle failed with an unhandled error")
        return {"started_at": utcnow().isoformat(), "actions": [], "errors": [str(exc)]}


# --------------------------------------------------------------------------- #
# The background loop
# --------------------------------------------------------------------------- #


class CryptoScheduler:
    """A thread that calls ``safe_crypto_cycle`` on a fixed interval.

    Not an APScheduler job like the stock side's — this one lives inside the
    dashboard process and is started/stopped by its own buttons, so a plain
    ``threading.Event``-gated loop is simpler and just as correct.
    """

    def __init__(self, config: AppConfig, storage: Storage):
        self._config = config
        self._storage = storage
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_result: dict | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def reconfigure(self, config: AppConfig) -> None:
        """Swap in a new config for the next ``start()``.

        Does nothing to a loop already running — ``_loop`` builds its runtime
        (and therefore its exchange client) once at ``start()``, so a change
        to credentials or the exchange only takes effect after a stop/start.
        """
        self._config = config

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="crypto-engine", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None

    def _loop(self) -> None:
        runtime = build_runtime(self._config, self._storage)
        log.warning(
            "crypto engine started: %s on %s, %s strategy, poll every %ds%s",
            ", ".join(self._config.crypto.pairs),
            self._config.crypto.exchange,
            self._config.crypto.strategy,
            self._config.crypto.poll_interval_seconds,
            "  *** REAL MONEY ***" if self._config.crypto.is_live else "",
        )
        try:
            while not self._stop.is_set():
                result = safe_crypto_cycle(runtime)
                with self._lock:
                    self.last_result = result
                self._stop.wait(self._config.crypto.poll_interval_seconds)
        finally:
            runtime.close()
            log.warning("crypto engine stopped")


__all__ = [
    "CryptoRuntime",
    "CryptoScheduler",
    "build_client",
    "build_runtime",
    "fetch_signal",
    "run_crypto_cycle",
    "safe_crypto_cycle",
]
