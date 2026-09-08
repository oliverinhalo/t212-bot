"""Convert a foreign-currency quote to GBP so the risk manager can size it.

Every cap in ``config.yaml`` is GBP. A quote for a US or EU instrument comes
back in USD/EUR, so before it reaches ``risk_manager`` it is converted here.
Rates come from Yahoo (``GBP{CUR}=X``) — the same free source as the quotes —
and are cached for the cycle. A rate we cannot obtain is fatal for that one
instrument: the cycle rejects it rather than guess a size.

``GBX`` / ``GBp`` (London pence) is not really a currency — it is GBP × 100 —
and is handled without a network call.
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Callable

import httpx

from .models import ZERO, dec

log = logging.getLogger(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

_PENCE = {"GBX", "GBP.", "GBP·", "GBPX", "PENCE"}


class FxError(Exception):
    """A rate could not be obtained; the instrument is untradeable this cycle."""


class FxConverter:
    """GBP conversion with a short per-currency cache."""

    def __init__(
        self,
        timeout: float = 20.0,
        cache_seconds: int = 900,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._owns_client = client is None
        self._http = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; t212-bot/0.1)"},
        )
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cache: dict[str, tuple[float, Decimal]] = {}

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def rate(self, currency: str) -> Decimal:
        """Units of ``currency`` per 1 GBP. GBP -> 1, GBX -> 100."""
        raw = (currency or "GBP").strip()
        if raw == "GBp":  # Yahoo's notation for London pence
            return Decimal(100)
        cur = raw.upper()
        if cur in ("GBP", ""):
            return Decimal(1)
        if cur in _PENCE:
            return Decimal(100)

        cached = self._cache.get(cur)
        if cached and (self._clock() - cached[0]) < self._cache_seconds:
            return cached[1]

        symbol = f"GBP{cur}=X"
        try:
            response = self._http.get(
                YAHOO_CHART_URL.format(symbol=symbol),
                params={"range": "5d", "interval": "1d"},
            )
        except httpx.HTTPError as exc:
            raise FxError(f"FX request for {symbol} failed: {exc}") from exc
        if response.status_code != 200:
            raise FxError(f"FX HTTP {response.status_code} for {symbol}")

        result = (response.json().get("chart") or {}).get("result") or []
        price = None
        if result:
            price = (result[0].get("meta") or {}).get("regularMarketPrice")
        try:
            value = dec(price) if price is not None else None
        except ValueError:
            value = None
        if value is None or value <= ZERO:
            raise FxError(f"no usable rate for {symbol}: {price!r}")

        self._cache[cur] = (self._clock(), value)
        return value

    def to_gbp(self, amount: Decimal, currency: str) -> Decimal:
        """Convert ``amount`` in ``currency`` to GBP."""
        return amount / self.rate(currency)


__all__ = ["FxConverter", "FxError"]
