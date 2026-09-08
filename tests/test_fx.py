"""GBP FX conversion for open-universe instruments."""

from __future__ import annotations

import httpx
import pytest

from t212bot.fx import FxConverter, FxError
from t212bot.models import dec


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _rate_response(symbol: str, price):
    return httpx.Response(
        200,
        json={"chart": {"result": [{"meta": {"regularMarketPrice": price, "currency": symbol[3:6]}}]}},
    )


def test_gbp_and_pence_need_no_network():
    fx = FxConverter(client=_client(lambda r: httpx.Response(500)))
    assert fx.rate("GBP") == dec(1)
    assert fx.rate("GBX") == dec(100)
    assert fx.rate("GBp") == dec(100)
    assert fx.to_gbp(dec(500), "GBX") == dec(5)


def test_usd_rate_is_fetched_and_applied():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "GBPUSD=X" in str(request.url)
        return _rate_response("GBPUSD=X", 1.25)

    fx = FxConverter(client=_client(handler))
    assert fx.rate("USD") == dec("1.25")
    assert fx.to_gbp(dec(125), "USD") == dec(100)


def test_a_rate_is_cached():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return _rate_response("GBPEUR=X", 1.16)

    fx = FxConverter(client=_client(handler), cache_seconds=999, clock=lambda: 0.0)
    fx.rate("EUR")
    fx.rate("EUR")
    assert len(calls) == 1


def test_an_unavailable_rate_raises():
    fx = FxConverter(client=_client(lambda r: httpx.Response(404)))
    with pytest.raises(FxError):
        fx.rate("USD")

    fx2 = FxConverter(
        client=_client(lambda r: _rate_response("GBPUSD=X", 0)),
    )
    with pytest.raises(FxError):
        fx2.rate("USD")
