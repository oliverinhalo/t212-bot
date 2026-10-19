"""Thin CCXT wrapper for the crypto side — the same role t212_client.py plays
for Trading212, at a much smaller scale: balance, OHLCV, and a market order.

CCXT is only imported inside ``CcxtClient.__init__``, not at module load time,
because it is an optional dependency — most of this file (and every test
against it) runs fine without it installed; only actually talking to an
exchange needs the package.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from .models import dec

log = logging.getLogger(__name__)


class CryptoError(Exception):
    """Base class for all crypto-exchange client failures."""


class CryptoAuthError(CryptoError):
    """Bad API key/secret, or a key without trading permission."""


class CryptoAPIError(CryptoError):
    """The exchange rejected the request (bad symbol, insufficient funds, ...)."""


class CryptoTransportError(CryptoError):
    """Connection failed, timed out, or the exchange is unreachable."""


class CcxtClient:
    """One exchange, one set of credentials. Spot market orders only."""

    def __init__(
        self,
        exchange_id: str,
        *,
        api_key: str = "",
        api_secret: str = "",
        timeout_seconds: float = 20.0,
    ):
        try:
            import ccxt
        except ImportError as exc:  # pragma: no cover - config.load() already checked this
            raise CryptoError("the 'ccxt' package is not installed — pip install ccxt") from exc

        if not hasattr(ccxt, exchange_id):
            raise CryptoError(f"{exchange_id!r} is not a CCXT exchange id")
        self._ccxt = ccxt
        self._exchange = getattr(ccxt, exchange_id)(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "enableRateLimit": True,
                "timeout": int(timeout_seconds * 1000),
            }
        )

    def close(self) -> None:
        close = getattr(self._exchange, "close", None)
        if callable(close):  # pragma: no branch - always true for ccxt exchanges
            close()

    def _wrap(self, fn, *args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except self._ccxt.AuthenticationError as exc:
            raise CryptoAuthError(str(exc)) from exc
        except (self._ccxt.NetworkError, self._ccxt.ExchangeNotAvailable) as exc:
            raise CryptoTransportError(str(exc)) from exc
        except self._ccxt.BaseError as exc:
            raise CryptoAPIError(str(exc)) from exc

    # ------------------------------------------------------------------ reads
    def fetch_balance(self, currency: str) -> Decimal:
        """Free (spendable) balance of one currency, e.g. "USDT"."""
        balance = self._wrap(self._exchange.fetch_balance)
        free = (balance.get("free") or {}).get(currency)
        return dec(free) if free is not None else Decimal(0)

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int) -> list[list[float]]:
        """Daily (or whatever ``timeframe`` is) candles, oldest first.

        Each row is ``[timestamp_ms, open, high, low, close, volume]`` — CCXT's
        own uniform shape, the same across every exchange it supports.
        """
        return self._wrap(self._exchange.fetch_ohlcv, symbol, timeframe=timeframe, limit=limit)

    # ----------------------------------------------------------------- orders
    def create_market_order(self, symbol: str, side: str, amount: Decimal) -> dict:
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be buy|sell, got {side!r}")
        return self._wrap(self._exchange.create_order, symbol, "market", side, float(amount))


__all__ = [
    "CcxtClient",
    "CryptoError",
    "CryptoAuthError",
    "CryptoAPIError",
    "CryptoTransportError",
]
