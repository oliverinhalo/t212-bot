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

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Mapping, Protocol

import httpx

from .instruments import Instrument
from .models import Bar, PriceHistory, Quote, WatchItem, ZERO, dec, maybe_dec, utcnow

log = logging.getLogger(__name__)

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SEARCH_URL = "https://query1.finance.yahoo.com/v1/finance/search"

# Trading212 ticker suffix -> Yahoo Finance symbol suffix. A last resort, used
# only when neither an override nor the ISIN search resolves a symbol.
_T212_SUFFIX_TO_YAHOO = {
    "_US_EQ": "",
    "l_EQ": ".L",
    "d_EQ": ".DE",
    "p_EQ": ".PA",
    "a_EQ": ".AS",
    "b_EQ": ".BR",
    "m_EQ": ".MC",
    "s_EQ": ".SW",
    "e_EQ": ".DE",
    "h_EQ": ".HE",
    "f_EQ": ".F",
}

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
        return self.fetch_symbol(item.ticker, item.yahoo, history_days)

    def fetch_symbol(
        self, ticker: str, yahoo: str, history_days: int
    ) -> tuple[Quote, PriceHistory]:
        """Quote + daily bars for one instrument, keyed and cached by ``ticker``.

        ``ticker`` is what the rest of the codebase uses (the Trading212
        ticker); ``yahoo`` is only the symbol we ask Yahoo for.
        """
        cached = self._cache.get(ticker)
        if cached and (time.monotonic() - cached[0]) < self._cache_seconds:
            return cached[1], cached[2]

        span = max(history_days + 10, 20)
        response = self._http.get(
            YAHOO_CHART_URL.format(symbol=yahoo),
            params={"range": f"{span}d", "interval": "1d"},
        )
        if response.status_code != 200:
            raise MarketDataError(f"HTTP {response.status_code} for {yahoo}")

        payload = response.json()
        result = (payload.get("chart") or {}).get("result") or []
        if not result:
            error = (payload.get("chart") or {}).get("error")
            raise MarketDataError(f"no data for {yahoo}: {error}")

        quote, history = self._parse(ticker, yahoo, result[0], history_days)
        self._cache[ticker] = (time.monotonic(), quote, history)
        return quote, history

    def _parse(
        self, ticker: str, yahoo: str, result: Mapping, history_days: int
    ) -> tuple[Quote, PriceHistory]:
        meta = result.get("meta") or {}
        raw_price = maybe_dec(meta.get("regularMarketPrice"))
        if raw_price is None or raw_price <= ZERO:
            raise MarketDataError(f"no usable price for {yahoo}")

        raw_currency = str(meta.get("currency", "GBP"))
        price, currency = _normalise(raw_price, raw_currency)

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
            close_price, _ = _normalise(dec(close), raw_currency)
            bars.append(
                Bar(day=datetime.fromtimestamp(int(stamp), tz=timezone.utc).date(), close=close_price)
            )

        return (
            Quote(
                ticker=ticker,
                price=price,
                currency=currency,
                as_of=as_of,
                source="yahoo",
            ),
            PriceHistory(ticker=ticker, bars=tuple(bars[-history_days:])),
        )


class StaticMarketData:
    """Fixed prices. For tests and for --dry-run without network access.

    ``symbol_prices`` (keyed by the Yahoo symbol, or by T212 ticker) lets a test
    exercise the open-universe on-demand path without a network call.
    """

    def __init__(
        self,
        prices: Mapping[str, Decimal],
        as_of: datetime | None = None,
        symbol_prices: Mapping[str, tuple[Decimal, str]] | None = None,
    ):
        self._prices = dict(prices)
        self._as_of = as_of
        self._symbol_prices = dict(symbol_prices or {})

    def fetch_symbol(
        self, ticker: str, yahoo: str, history_days: int = 30
    ) -> tuple[Quote, PriceHistory]:
        entry = self._symbol_prices.get(yahoo) or self._symbol_prices.get(ticker)
        if entry is None:
            raise MarketDataError(f"no static symbol price for {ticker}/{yahoo}")
        price, currency = entry
        price, currency = _normalise(price, currency)
        now = self._as_of or utcnow()
        bars = tuple(
            Bar(day=now.date(), close=price) for _ in range(max(history_days, 1))
        )
        return (
            Quote(ticker=ticker, price=price, currency=currency, as_of=now, source="static"),
            PriceHistory(ticker=ticker, bars=bars),
        )

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


# --------------------------------------------------------------------------- #
# Yahoo symbol resolution for open-universe mode
# --------------------------------------------------------------------------- #


def currencies_match(quote_currency: str, instrument_currency: str) -> bool:
    """True if a Yahoo quote currency is consistent with the T212 instrument.

    GBP and GBX (pence) are the same money; ``_normalise`` has already turned a
    GBX quote into GBP by this point, so both map to ``GBP`` here.
    """
    def canon(cur: str) -> str:
        cur = (cur or "").strip().upper()
        return "GBP" if cur in ("GBP", "GBX", "GBP.", "GBP·", "GBP") else cur

    q, i = canon(quote_currency), canon(instrument_currency)
    return not q or not i or q == i


class SymbolResolver:
    """Maps a T212 :class:`Instrument` to a Yahoo Finance symbol.

    Order: explicit override -> on-disk ISIN cache -> Yahoo ISIN search ->
    derive from short name + exchange suffix. A resolved symbol is written back
    to the cache. Returns ``None`` when nothing is confident enough.
    """

    def __init__(
        self,
        overrides: Mapping[str, str] | None = None,
        cache_path: str | Path = "data/symbol_map.json",
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        search: bool = True,
    ):
        self._overrides = {k.upper(): v for k, v in (overrides or {}).items()}
        self._cache_path = Path(cache_path)
        self._search = search
        self._owns_client = client is None
        self._http = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "Mozilla/5.0 (compatible; t212-bot/0.1)"},
        )
        self._cache: dict[str, str] = {}
        if self._cache_path.exists():
            try:
                self._cache = {
                    str(k): str(v) for k, v in json.loads(self._cache_path.read_text()).items()
                }
            except (json.JSONDecodeError, OSError):  # pragma: no cover - defensive
                self._cache = {}

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def _remember(self, isin: str, symbol: str) -> None:
        if not isin:
            return
        self._cache[isin] = symbol
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(json.dumps(self._cache, indent=0, sort_keys=True))
        except OSError:  # pragma: no cover - cache is best-effort
            log.debug("could not write symbol cache %s", self._cache_path)

    def resolve(self, instrument: Instrument) -> str | None:
        override = self._overrides.get(instrument.ticker.upper()) or self._overrides.get(
            instrument.isin.upper()
        )
        if override:
            return override

        if instrument.isin and instrument.isin in self._cache:
            return self._cache[instrument.isin]

        if self._search and instrument.isin:
            symbol = self._search_by_isin(instrument.isin)
            if symbol:
                self._remember(instrument.isin, symbol)
                return symbol

        derived = self._derive(instrument)
        if derived:
            self._remember(instrument.isin, derived)
            return derived
        return None

    def _search_by_isin(self, isin: str) -> str | None:
        try:
            response = self._http.get(
                YAHOO_SEARCH_URL,
                params={"q": isin, "quotesCount": 5, "newsCount": 0},
            )
        except httpx.HTTPError as exc:
            log.warning("Yahoo symbol search for %s failed: %s", isin, exc)
            return None
        if response.status_code != 200:
            return None
        quotes = (response.json() or {}).get("quotes") or []
        for quote in quotes:
            if quote.get("quoteType") in ("EQUITY", "ETF") and quote.get("symbol"):
                return str(quote["symbol"])
        return None

    @staticmethod
    def _derive(instrument: Instrument) -> str | None:
        if not instrument.short_name:
            return None
        for suffix, yahoo_suffix in _T212_SUFFIX_TO_YAHOO.items():
            if instrument.ticker.endswith(suffix):
                base = re.sub(r"[^A-Za-z0-9.\-]", "", instrument.short_name)
                return f"{base}{yahoo_suffix}" if base else None
        return None


def build_provider(config) -> MarketDataProvider:
    """Construct the configured market data provider."""
    provider = config.market_data.provider
    if provider == "yahoo":
        return YahooMarketData(
            timeout=config.market_data.timeout_seconds,
            cache_seconds=config.market_data.cache_seconds,
        )
    raise MarketDataError(f"unknown market_data.provider: {provider!r}")
