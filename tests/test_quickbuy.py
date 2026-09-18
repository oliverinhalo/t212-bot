"""t212bot.quickbuy: the shared machinery behind the manual, no-AI buy scripts."""

from __future__ import annotations

from decimal import Decimal

import pytest

from t212bot.models import dec
from t212bot.quickbuy import place_with_precision_retry, size_order, to_gbp
from t212bot.t212_client import T212APIError

PRECISION_BODY = (
    '{"type":"/api-errors/quantity-precision-mismatch",'
    '"title":"Error while placing the order","status":400,'
    '"detail":"invalid quantity precision 4","traceId":"60d01e4e"}'
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

    response = place_with_precision_retry(client, "NVDA_US_EQ", dec("0.030383"), dec("164.57"))

    assert response["status"] == "FILLED"
    # Rounded DOWN to 4 dp, so the retry never spends more than the first try.
    assert client.calls == [dec("0.030383"), dec("0.0303")]


def test_a_quantity_the_broker_accepts_is_not_retried() -> None:
    client = FakeClient(max_dp=4)
    place_with_precision_retry(client, "NVDA_US_EQ", dec("0.0303"), dec("164.57"))
    assert client.calls == [dec("0.0303")]


def test_an_amount_too_small_for_that_precision_says_so() -> None:
    """£0.01 of a £164 share is 0.00006 — nothing at all once rounded to 4 dp."""
    client = FakeClient(max_dp=4)

    with pytest.raises(SystemExit, match="rounds to nothing"):
        place_with_precision_retry(client, "NVDA_US_EQ", dec("0.000060"), dec("164.57"))

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
        place_with_precision_retry(client, "NVDA_US_EQ", dec("0.03"), dec("164.57"))
    assert client.calls == 1


# --------------------------------------------------------------------------- #
# Sizing and FX
# --------------------------------------------------------------------------- #


def test_size_order_floors_to_the_broker_precision() -> None:
    # £5 / £164.57 = 0.0303822..., floored to 6 dp, never rounded up past budget.
    assert size_order(dec(5), dec("164.57"), 6) == dec("0.030382")


def test_size_order_at_zero_decimals_means_whole_shares_only() -> None:
    assert size_order(dec(5), dec("164.57"), 0) == dec("0")


def test_to_gbp_is_a_no_op_for_gbp() -> None:
    assert to_gbp(dec("10.50"), "GBP") == dec("10.50")


def test_to_gbp_converts_a_foreign_price() -> None:
    from unittest.mock import MagicMock, patch

    with patch("t212bot.quickbuy.FxConverter") as fx_cls:
        fx = MagicMock()
        fx.rate.return_value = dec("1.25")
        fx_cls.return_value = fx

        price = to_gbp(dec("125.00"), "USD")

        assert price == dec("100.00")
        fx.rate.assert_called_once_with("USD")
        fx.close.assert_called_once()
