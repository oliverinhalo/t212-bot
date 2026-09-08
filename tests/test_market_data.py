"""Yahoo symbol resolution and the currency cross-check for open-universe mode."""

from __future__ import annotations

import json

import httpx

from t212bot.instruments import Instrument
from t212bot.market_data import SymbolResolver, currencies_match

APPLE = Instrument(
    ticker="AAPL_US_EQ", name="Apple", short_name="AAPL",
    isin="US0378331005", currency="USD", type="STOCK",
)
SAP = Instrument(
    ticker="SAPd_EQ", name="SAP", short_name="SAP",
    isin="DE0007164600", currency="EUR", type="STOCK",
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _search_hit(symbol: str) -> httpx.Response:
    return httpx.Response(200, json={"quotes": [{"quoteType": "EQUITY", "symbol": symbol}]})


def test_currencies_match_treats_gbp_and_gbx_as_equal():
    assert currencies_match("GBP", "GBX")
    assert currencies_match("USD", "USD")
    assert not currencies_match("USD", "EUR")
    assert currencies_match("", "USD")  # unknown -> don't block


def test_override_wins(tmp_path):
    r = SymbolResolver(
        overrides={"AAPL_US_EQ": "AAPL"},
        cache_path=tmp_path / "m.json",
        client=_client(lambda req: httpx.Response(500)),
    )
    assert r.resolve(APPLE) == "AAPL"


def test_disk_cache_is_used_before_the_network(tmp_path):
    cache = tmp_path / "m.json"
    cache.write_text(json.dumps({"US0378331005": "AAPL"}))
    called = []
    r = SymbolResolver(
        cache_path=cache,
        client=_client(lambda req: called.append(1) or httpx.Response(500)),
    )
    assert r.resolve(APPLE) == "AAPL"
    assert not called


def test_isin_search_resolves_and_is_cached(tmp_path):
    cache = tmp_path / "m.json"
    r = SymbolResolver(cache_path=cache, client=_client(lambda req: _search_hit("SAP.DE")))
    assert r.resolve(SAP) == "SAP.DE"
    assert json.loads(cache.read_text())["DE0007164600"] == "SAP.DE"


def test_falls_back_to_deriving_from_the_ticker_suffix(tmp_path):
    r = SymbolResolver(
        cache_path=tmp_path / "m.json",
        search=False,
        client=_client(lambda req: httpx.Response(404)),
    )
    assert r.resolve(APPLE) == "AAPL"     # _US_EQ -> no suffix
    assert r.resolve(SAP) == "SAP.DE"     # d_EQ -> .DE


def test_returns_none_when_nothing_resolves(tmp_path):
    weird = Instrument(
        ticker="WAT_XX_EQ", name="Weird", short_name="", isin="", currency="ZWL", type="STOCK",
    )
    r = SymbolResolver(
        cache_path=tmp_path / "m.json", search=False,
        client=_client(lambda req: httpx.Response(404)),
    )
    assert r.resolve(weird) is None
