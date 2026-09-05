"""Confirm Trading212 credentials work, and print the account summary.

    python -m scripts.check_auth

Read-only: it calls the account and portfolio endpoints and nothing else. Run
this first, against demo, before anything else in the project.
"""

from __future__ import annotations

import sys

from t212bot.config import ConfigError, load
from t212bot.models import money
from t212bot.t212_client import (
    T212AuthError,
    T212Client,
    T212Error,
    free_cash,
    positions_from_portfolio,
)


def main() -> int:
    try:
        config = load()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if not config.secrets.t212_api_key:
        print("T212_API_KEY is not set in .env — nothing to check.", file=sys.stderr)
        return 2

    print(f"mode:        {config.mode}")
    print(f"base url:    {config.t212_base_url}")
    print(f"auth scheme: {config.t212_auth_scheme}")
    print()

    with T212Client(
        base_url=config.t212_base_url,
        api_key=config.secrets.t212_api_key,
        api_secret=config.secrets.t212_api_secret,
        auth_scheme=config.t212_auth_scheme,
    ) as client:
        try:
            info = client.account_info()
        except T212AuthError as exc:
            print(f"AUTH FAILED: {exc}", file=sys.stderr)
            print(
                "\nTry the other auth scheme: set T212_AUTH_SCHEME=header (or =basic) in .env "
                "and run this again.",
                file=sys.stderr,
            )
            return 1
        except T212Error as exc:
            print(f"REQUEST FAILED: {exc}", file=sys.stderr)
            return 1

        print("auth OK")
        print(f"  account id: {info.get('id')}")
        print(f"  currency:   {info.get('currencyCode')}")

        cash = client.account_cash()
        print(f"  free cash:  {money(free_cash(cash))}")
        print(f"  invested:   {cash.get('invested')}")
        print(f"  total:      {cash.get('total')}")

        positions = positions_from_portfolio(client.portfolio())
        print(f"  positions:  {len(positions)}")
        for position in positions:
            print(
                f"     {position.ticker}: {position.quantity} @ "
                f"{money(position.average_price)} (now {money(position.current_price)})"
            )

        if info.get("currencyCode") and info["currencyCode"] != "GBP":
            print(
                f"\nWARNING: the account currency is {info['currencyCode']}, but every cap in "
                "config.yaml is denominated in GBP. Fix one or the other before trading."
            )

        missing = [
            item.ticker for item in config.watchlist
        ]
        print(f"\nwatch-list tickers to verify with list_instruments: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
