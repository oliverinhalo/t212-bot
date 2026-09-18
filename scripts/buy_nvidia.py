"""Buy a fixed amount of NVIDIA, straight through the Trading212 API.

    python -m scripts.buy_nvidia                 # buy £5 of NVIDIA, now
    python -m scripts.buy_nvidia --amount 10     # £10 instead
    python -m scripts.buy_nvidia --dry-run       # show every step, place nothing
    python -m scripts.buy_nvidia --ticker NVDA_US_EQ   # skip the ISIN lookup

No AI, no risk manager, no capital caps, no market-hours gate, no database.
One instrument, one order, printed at every step so a failure says which step
failed and exactly what the broker answered.

    NVIDIA CORP — ISIN US67066G1040

MODE in .env still decides which account this hits: `paper` refuses (there is
no broker to call), `demo` uses the practice account, `live` spends real
money. Nothing here asks for confirmation — that is the point of it.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import ROUND_FLOOR, Decimal

from t212bot.config import ConfigError, load
from t212bot.fx import FxConverter, FxError
from t212bot.instruments import InstrumentCatalogue
from t212bot.market_data import MarketDataError, YahooMarketData
from t212bot.models import ZERO, money
from t212bot.t212_client import (
    T212APIError,
    T212AuthError,
    T212Client,
    T212Error,
    free_cash,
)

NVIDIA_ISIN = "US67066G1040"
NVIDIA_NAME = "NVIDIA CORP"
NVIDIA_YAHOO = "NVDA"


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def resolve_ticker(client: T212Client, config) -> str:
    """The Trading212 ticker for the ISIN, from the cache or the API."""
    catalogue = InstrumentCatalogue.load(config.instruments_path)
    if catalogue is not None:
        found = catalogue.resolve(NVIDIA_ISIN)
        if found is not None:
            print(f"    found in {config.instruments_path}: {found.ticker} ({found.name})")
            return found.ticker
        print(f"    {config.instruments_path} has no {NVIDIA_ISIN}; asking the API")
    else:
        print(f"    no cached catalogue at {config.instruments_path}; asking the API")

    rows = client.instruments()
    print(f"    the API lists {len(rows)} instruments")
    for row in rows:
        if str(row.get("isin", "")).upper() == NVIDIA_ISIN:
            ticker = str(row.get("ticker", ""))
            print(f"    matched ISIN {NVIDIA_ISIN}: {ticker} ({row.get('name')})")
            print(f"    currency {row.get('currencyCode')}, type {row.get('type')}")
            return ticker
    raise SystemExit(
        f"!! {NVIDIA_ISIN} is not in this account's tradable universe.\n"
        "   That usually means the key belongs to a different Trading212 "
        "environment than the one you are looking at in the app."
    )


def gbp_price(ticker: str) -> Decimal:
    """What one share costs in GBP, via Yahoo plus an FX rate."""
    market = YahooMarketData()
    try:
        quote, _ = market.fetch_symbol(ticker, NVIDIA_YAHOO, history_days=5)
    finally:
        market.close()
    print(f"    Yahoo: {quote.price} {quote.currency}")

    if quote.currency.upper() == "GBP":
        return quote.price

    fx = FxConverter()
    try:
        rate = fx.rate(quote.currency)
    finally:
        fx.close()
    price = quote.price / rate
    print(f"    FX: {rate} {quote.currency} per GBP  ->  {money(price)} GBP per share")
    return price


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="buy_nvidia",
        description=f"Buy a fixed GBP amount of {NVIDIA_NAME} ({NVIDIA_ISIN}).",
    )
    parser.add_argument("--amount", type=Decimal, default=Decimal(5),
                        help="how much to spend, in GBP (default: 5)")
    parser.add_argument("--ticker", default=None,
                        help="Trading212 ticker, if you already know it")
    parser.add_argument("--dry-run", action="store_true",
                        help="do everything except place the order")
    args = parser.parse_args(argv)

    if args.amount <= ZERO:
        print("--amount must be positive", file=sys.stderr)
        return 2

    try:
        config = load()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    print(f"buying {money(args.amount)} GBP of {NVIDIA_NAME} ({NVIDIA_ISIN})")
    print(f"mode:        {config.mode}" + ("   *** REAL MONEY ***" if config.is_live else ""))
    print(f"base url:    {config.t212_base_url}")
    print(f"auth scheme: {config.t212_auth_scheme}")

    if not config.places_real_orders:
        print(
            f"\n!! MODE={config.mode} never calls the broker — there is nothing for this "
            "script to do.\n   Set MODE=demo (practice account) or MODE=live in .env.",
            file=sys.stderr,
        )
        return 2
    if not config.secrets.t212_api_key:
        print("\n!! T212_API_KEY is not set in .env.", file=sys.stderr)
        return 2

    try:
        with T212Client(
            base_url=config.t212_base_url,
            api_key=config.secrets.t212_api_key,
            api_secret=config.secrets.t212_api_secret,
            auth_scheme=config.t212_auth_scheme,
        ) as client:
            step(1, "checking the account")
            cash = client.account_cash()
            available = free_cash(cash)
            print(f"    free cash: {money(available)}")
            if available < args.amount:
                print(
                    f"    !! only {money(available)} available — the broker will refuse "
                    f"a {money(args.amount)} order."
                )

            step(2, f"resolving {NVIDIA_ISIN}")
            ticker = args.ticker or resolve_ticker(client, config)

            step(3, "pricing one share")
            price = gbp_price(ticker)

            step(4, "sizing the order")
            step_size = Decimal(1).scaleb(-config.execution.quantity_decimals)
            quantity = (args.amount / price).quantize(step_size, rounding=ROUND_FLOOR)
            print(f"    {money(args.amount)} / {money(price)} = {quantity} shares"
                  f" (rounded down to {config.execution.quantity_decimals} dp)")
            if quantity <= ZERO:
                print(
                    f"\n!! {money(args.amount)} does not buy a tradeable quantity at "
                    f"{money(price)} a share.",
                    file=sys.stderr,
                )
                return 1

            step(5, f"placing a market order: BUY {quantity} {ticker}")
            if args.dry_run:
                print("    --dry-run: nothing sent.")
                return 0

            response = client.place_market_order(ticker, quantity)
            print("    the broker replied:")
            print(json.dumps(response, indent=6, default=str))

            order_id = response.get("id") or response.get("orderId")
            if order_id is not None:
                step(6, f"reading order {order_id} back")
                try:
                    print(json.dumps(client.get_order(order_id), indent=6, default=str))
                except T212Error as exc:
                    print(f"    could not read it back: {exc}")
            print(f"\nDone. Check the Trading212 app for {ticker}.")
            return 0

    except T212AuthError as exc:
        print(
            f"\n!! authentication failed: {exc}\n"
            "   The key, the auth scheme, or the environment is wrong. A key made for\n"
            "   the practice account does not work against live, and the reverse.",
            file=sys.stderr,
        )
        return 1
    except T212APIError as exc:
        print(
            f"\n!! the broker refused it: HTTP {exc.status_code}\n   {exc.body[:800]}",
            file=sys.stderr,
        )
        return 1
    except (MarketDataError, FxError) as exc:
        print(f"\n!! could not price NVIDIA: {exc}", file=sys.stderr)
        return 1
    except T212Error as exc:
        print(f"\n!! Trading212 call failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
