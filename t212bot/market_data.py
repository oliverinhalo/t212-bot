"""Market data for quotes and recent price action.

Trading212's public API has no quote endpoint, so prices come from a separate
free source keyed off the ``yahoo`` symbol in each watch-list entry. Quotes are
only ever used to *value* positions and to sanity-check the AI — the broker
still fills at whatever the real market gives us.

One trap worth knowing about: London-listed ETFs are usually quoted in **pence**
(``GBp``), not pounds. Getting that wrong is a factor-of-100 error in every
capital check, so ``_normalise`` converts to GBP explicitly rather than
assuming.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Protocol

import httpx

from .models import Bar, PriceHistory, Quote, WatchItem, ZERO, dec, maybe_dec, utcnow

log = logging.getLogger(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

# Currencies quoted in minor units, and their divisor.
_MINOR_UNITS = {"GBP": Decimal(1), "GBX": Decimal(100), "GBP.": Decimal(1), "GBp": Decimal(100)}


class MarketDataError(Exception):
    """Could not obtain usable market data for a ticker."""


@dataclass(frozen=True)
class Snapshot:
    """Everything the AI and the risk manager get to see about the market."""

    quotes: dict[str, Quote]
    histories: dict[str, PriceHistory]
    errors: dict[str, str]

    def prices(self) -> dict[str, Decimal]:
        return {ticker: quote.price for ticker, quote in self.quotes.items()}


class MarketDataProvider(Protocol):
    def fetch(self, items: Iterable[WatchItem], history_days: int) -> Snapshot: ...


def _normalise(price: Decimal, currency: str) -> tuple[Decimal, str]:
    """Convert a quote to major currency units (pence -> pounds)."""
    divisor = _MINOR_UNITS.get(currency, Decimal(1))
    if divisor != Decimal(1):
        return price / divisor, "GBP"
    return price, currency.upper()


class YahooMarketData:
    """Free daily bars and a last price, no API key required."""

    def __init__(
        self,
        timeout: float = 20.0,
        cache_seconds: int = 60,
        client: httpx.Client | None = None,
    ):
        self._owns_client = client is None
        self._http = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; t212-bot/0.1)"},
        )
        self._cache_seconds = cache_seconds
        self._cache: dict[str, tuple[float, Quote, PriceHistory]] = {}

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def fetch(self, items: Iterable[WatchItem], history_days: int = 30) -> Snapshot:
        quotes: dict[str, Quote] = {}
        histories: dict[str, PriceHistory] = {}
        errors: dict[str, str] = {}

        for item in items:
            try:
                quote, history = self._fetch_one(item, history_days)
            except Exception as exc:  # noqa: BLE001 - one bad ticker must not kill the cycle
                # A missing quote is not fatal: the risk manager rejects any
                # trade in a ticker it cannot price (rule R06).
                log.warning("market data failed for %s (%s): %s", item.ticker, item.yahoo, exc)
                errors[item.ticker] = str(exc)
                continue
            quotes[item.ticker] = quote
            histories[item.ticker] = history

        return Snapshot(quotes=quotes, histories=histories, errors=errors)

    def _fetch_one(self, item: WatchItem, history_days: int) -> tuple[Quote, PriceHistory]:
        cached = self._cache.get(item.ticker)
        if cached and (time.monotonic() - cached[0]) < self._cache_seconds:
            return cached[1], cached[2]

        span = max(history_days + 10, 20)
        response = self._http.get(
            YAHOO_CHART_URL.format(symbol=item.yahoo),
            params={"range": f"{span}d", "interval": "1d"},
        )
        if response.status_code != 200:
            raise MarketDataError(f"HTTP {response.status_code} for {item.yahoo}")

        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            error = (payload.get("chart") or {}).get("error")
            raise MarketDataError(f"no data for {item.yahoo}: {error}")

        quote, history = self._parse(item, result[0], history_days)
        self._cache[item.ticker] = (time.monotonic(), quote, history)
        return quote, history

    def _parse(
        self, item: WatchItem, result: Mapping, history_days: int
    ) -> tuple[Quote, PriceHistory]:
        meta = result.get("meta") or {}
        raw_price = maybe_dec(meta.get("regularMarketPrice"))
        if raw_price is None or raw_price <= ZERO:
            raise MarketDataError(f"no usable price for {item.yahoo}")

        price, currency = _normalise(raw_price, str(meta.get("currency", "GBP")))

        market_time = meta.get("regularMarketTime")
        as_of = (
            datetime.fromtimestamp(int(market_time), tz=timezone.utc)
            if market_time
            else utcnow()
        )

        bars: list[Bar] = []
        timestamps = result.get("timestamp") or []
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        for stamp, close in zip(timestamps, closes):
            if close is None:
                continue
            close_price, _ = _normalise(dec(close), str(meta.get("currency", "GBP")))
            bars.append(
                Bar(day=datetime.fromtimestamp(int(stamp), tz=timezone.utc).date(), close=close_price)
            )

        return (
            Quote(
                ticker=item.ticker,
                price=price,
                currency=currency,
                as_of=as_of,
                source="yahoo",
            ),
            PriceHistory(ticker=item.ticker, bars=tuple(bars[-history_days:])),
        )


class StaticMarketData:
    """Fixed prices. For tests and for --dry-run without network access."""

    def __init__(self, prices: Mapping[str, Decimal], as_of: datetime | None = None):
        self._prices = dict(prices)
        self._as_of = as_of

    def fetch(self, items: Iterable[WatchItem], history_days: int = 30) -> Snapshot:
        quotes: dict[str, Quote] = {}
        histories: dict[str, PriceHistory] = {}
        errors: dict[str, str] = {}
        now = self._as_of or utcnow()
        for item in items:
            price = self._prices.get(item.ticker)
            if price is None:
                errors[item.ticker] = "no static price configured"
                continue
            quotes[item.ticker] = Quote(
                ticker=item.ticker, price=price, currency="GBP", as_of=now, source="static"
            )
            histories[item.ticker] = PriceHistory(
                ticker=item.ticker, bars=(Bar(day=now.date(), close=price),)
            )
        return Snapshot(quotes=quotes, histories=histories, errors=errors)


def build_provider(config) -> MarketDataProvider:
    """Construct the configured market data provider."""
    provider = config.market_data.provider
    if provider == "yahoo":
        return YahooMarketData(
            timeout=config.market_data.timeout_seconds,
            cache_seconds=config.market_data.cache_seconds,
        )
    raise MarketDataError(f"unknown market_data.provider: {provider!r}")
