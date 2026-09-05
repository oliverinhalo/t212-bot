"""Find the exact Trading212 ticker for an instrument.

    python -m scripts.list_instruments vusa
    python -m scripts.list_instruments "S&P 500"

T212 tickers are not plain exchange symbols, and an order with a wrong ticker is
rejected at best. Copy the ``ticker`` column into config.yaml's watch-list.

The instruments endpoint is heavily rate-limited and returns several MB, so the
result is cached in data/instruments.json. Pass --refresh to re-fetch.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from t212bot.config import ConfigError, load
from t212bot.t212_client import T212Client, T212Error, find_instrument

CACHE = Path("data/instruments.json")


def main() -> int:
    parser = argparse.ArgumentParser(description="search Trading212 instruments")
    parser.add_argument("query", nargs="?", default="", help="ticker, name or ISIN fragment")
    parser.add_argument("--refresh", action="store_true", help="re-fetch the instrument list")
    parser.add_argument("--limit", type=int, default=25)
    args = parser.parse_args()

    try:
        config = load()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if CACHE.exists() and not args.refresh:
        instruments = json.loads(CACHE.read_text())
        print(f"(using cached {CACHE}, {len(instruments)} instruments — --refresh to update)\n")
    else:
        if not config.secrets.t212_api_key:
            print("T212_API_KEY is not set in .env", file=sys.stderr)
            return 2
        with T212Client(
            base_url=config.t212_base_url,
            api_key=config.secrets.t212_api_key,
            api_secret=config.secrets.t212_api_secret,
            auth_scheme=config.t212_auth_scheme,
        ) as client:
            try:
                instruments = client.instruments()
            except T212Error as exc:
                print(f"could not fetch instruments: {exc}", file=sys.stderr)
                return 1
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(instruments))
        print(f"cached {len(instruments)} instruments to {CACHE}\n")

    if not args.query:
        print("Pass a search term, e.g. 'vusa' or 'FTSE 100'.")
        return 0

    hits = find_instrument(instruments, args.query)
    if not hits:
        print(f"No instrument matches {args.query!r}.")
        return 1

    print(f"{'TICKER':<20} {'TYPE':<10} {'CURRENCY':<9} NAME")
    for item in hits[: args.limit]:
        print(
            f"{item.get('ticker', ''):<20} {item.get('type', ''):<10} "
            f"{item.get('currencyCode', ''):<9} {item.get('name', '')}"
        )
    if len(hits) > args.limit:
        print(f"\n... and {len(hits) - args.limit} more; narrow the search or raise --limit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
