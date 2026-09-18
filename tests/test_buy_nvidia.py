"""The standalone NVIDIA buy script: listing choice and precision handling."""

from __future__ import annotations

from decimal import Decimal

import pytest

from scripts.buy_nvidia import NVIDIA_ISIN, _pick, place
from t212bot.instruments import Instrument, InstrumentCatalogue
from t212bot.market_data import yahoo_symbol_for
from t212bot.models import dec
from t212bot.t212_client import T212APIError

PRECISION_BODY = (
    '{"type":"/api-errors/quantity-precision-mismatch",'
    '"title":"Error while placing the order","status":400,'
    '"detail":"invalid quantity precision 4","traceId":"60d01e4e"}'
)


def _instrument(ticker: str, currency: str) -> Instrument:
    return Instrument(
        ticker=ticker, name="Nvidia", short_name="NVDA",
        isin=NVIDIA_ISIN, currency=currency, type="STOCK",
    )


class FakeClient:
    """Refuses anything over ``max_dp`` the way Trading212 does."""

    def __init__(self, max_dp: int | None = 4):
        self.calls: list[Decimal] = []
        self._max_dp = max_dp

    def place_market_order(self, ticker: str, quantity: Decimal):
        self.calls.append(quantity)
        if self._max_dp is not None and -quantity.as_tuple().exponent > self._max_dp:
            raise T212APIError(400, PRECISION_BODY, "/equity/orders/market")
        return {"id": 1, "status": "FILLED", "ticker": ticker, "filledQuantity": quantity}


# --------------------------------------------------------------------------- #
# Quantity precision
# --------------------------------------------------------------------------- #


def test_precision_refusal_is_retried_at_the_precision_the_broker_named() -> None:
    client = FakeClient(max_dp=4)

    response = place(client, "NVDA_US_EQ", dec("0.030383"), dec("164.57"))

    assert response["status"] == "FILLED"
    # Rounded DOWN to 4 dp, so the retry never spends more than the first try.
    assert client.calls == [dec("0.030383"), dec("0.0303")]


def test_a_quantity_the_broker_accepts_is_not_retried() -> None:
    client = FakeClient(max_dp=4)
    place(client, "NVDA_US_EQ", dec("0.0303"), dec("164.57"))
    assert client.calls == [dec("0.0303")]


def test_an_amount_too_small_for_that_precision_says_so() -> None:
    """£0.01 of a £164 share is 0.00006 — nothing at all once rounded to 4 dp."""
    client = FakeClient(max_dp=4)

    with pytest.raises(SystemExit, match="rounds to nothing"):
        place(client, "NVDA_US_EQ", dec("0.000060"), dec("164.57"))

    assert len(client.calls) == 1  # the retry was never sent


def test_a_refusal_that_is_not_about_precision_is_not_retried() -> None:
    class Broke:
        def __init__(self):
            self.calls = 0

        def place_market_order(self, ticker, quantity):
            self.calls += 1
            raise T212APIError(400, '{"detail":"insufficient funds"}', "/equity/orders/market")

    client = Broke()
    with pytest.raises(T212APIError):
        place(client, "NVDA_US_EQ", dec("0.03"), dec("164.57"))
    assert client.calls == 1


# --------------------------------------------------------------------------- #
# Which listing of the ISIN
# --------------------------------------------------------------------------- #


def test_the_us_line_wins_even_when_it_is_not_first() -> None:
    chosen = _pick([_instrument("NVDd_EQ", "EUR"), _instrument("NVDA_US_EQ", "USD")])
    assert chosen.ticker == "NVDA_US_EQ"


def test_a_gbp_line_is_preferred_over_another_foreign_one() -> None:
    chosen = _pick([_instrument("NVDd_EQ", "EUR"), _instrument("NVDAl_EQ", "GBX")])
    assert chosen.ticker == "NVDAl_EQ"


def test_with_one_listing_there_is_nothing_to_choose() -> None:
    assert _pick([_instrument("NVDd_EQ", "EUR")]).ticker == "NVDd_EQ"


def test_matching_isin_returns_every_listing() -> None:
    """resolve() answers with one; choosing a venue needs them all."""
    catalogue = InstrumentCatalogue(
        [_instrument("NVDd_EQ", "EUR"), _instrument("NVDA_US_EQ", "USD")]
    )
    assert {i.ticker for i in catalogue.matching_isin(NVIDIA_ISIN)} == {
        "NVDd_EQ",
        "NVDA_US_EQ",
    }
    assert catalogue.matching_isin("") == []
    # resolve() keeps only the first, which is exactly the trap this avoids.
    assert catalogue.resolve(NVIDIA_ISIN).ticker == "NVDd_EQ"


# --------------------------------------------------------------------------- #
# Pricing the listing you are actually buying
# --------------------------------------------------------------------------- #


def test_the_german_line_prices_off_the_german_feed() -> None:
    assert yahoo_symbol_for(_instrument("NVDd_EQ", "EUR")) == "NVDA.DE"


def test_the_us_line_prices_off_nasdaq() -> None:
    assert yahoo_symbol_for(_instrument("NVDA_US_EQ", "USD")) == "NVDA"
