"""Shared machinery for the manual, no-AI buy scripts.

Two things worth factoring out of a "one instrument, one order" script:

  - the precision-retry dance around ``place_market_order`` (Trading212
    rejects a quantity with too many decimal places and does not publish the
    allowance anywhere except that refusal)
  - converting a Yahoo quote to GBP

Both scripts using this print their own step numbers; this module only prints
the lines that are themselves informative (what the broker said, what the FX
rate was), never step headers, so it reads the same embedded in either.
"""

from __future__ import annotations

import re
from decimal import ROUND_FLOOR, Decimal

from .fx import FxConverter
from .models import ZERO, money
from .t212_client import T212APIError, T212Client

_PRECISION_RE = re.compile(r"precision\s+(\d+)")


def to_gbp(price: Decimal, currency: str) -> Decimal:
    """Convert a price already in hand to GBP. Never refetches a quote."""
    if currency.upper() == "GBP":
        return price
    fx = FxConverter()
    try:
        rate = fx.rate(currency)
    finally:
        fx.close()
    converted = price / rate
    print(f"    FX: {rate} {currency} per GBP  ->  {money(converted)} GBP per share")
    return converted


def size_order(amount: Decimal, price: Decimal, quantity_decimals: int) -> Decimal:
    """Shares ``amount`` GBP buys at ``price``, floored to the broker's precision.

    Flooring only ever shrinks the order, never grows it past what was asked.
    """
    step = Decimal(1).scaleb(-quantity_decimals)
    return (amount / price).quantize(step, rounding=ROUND_FLOOR)


def place_with_precision_retry(
    client: T212Client, ticker: str, quantity: Decimal, price: Decimal
) -> dict:
    """Place the order, and re-round once if the broker names a precision.

    Trading212 rejects a quantity with more decimal places than the
    instrument allows ("invalid quantity precision 4") and the allowance is
    not in the public metadata, so the refusal itself is the only place it is
    stated. Re-rounding *down* to what it asked for and trying again spends
    no more money than the first attempt. A 400 is a definite refusal — no
    order was created — so this is not the retry of an order whose fate is
    unknown.
    """
    try:
        return client.place_market_order(ticker, quantity)
    except T212APIError as exc:
        match = _PRECISION_RE.search(exc.body or "")
        if not match or "precision" not in (exc.body or ""):
            raise
        places = int(match.group(1))
        retried = quantity.quantize(Decimal(1).scaleb(-places), rounding=ROUND_FLOOR)
        print(f"    broker wants {places} dp: {quantity} -> {retried}")
        if retried <= ZERO:
            raise SystemExit(
                f"!! at {places} dp the order rounds to nothing. "
                f"Raise the amount: one share is {money(price)}."
            ) from exc
        print(f"    retrying: BUY {retried} {ticker} (~{money(retried * price)})")
        return client.place_market_order(ticker, retried)


__all__ = ["to_gbp", "size_order", "place_with_precision_retry"]
