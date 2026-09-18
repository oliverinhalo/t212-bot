"""No AI, no confidence gate: screen a fixed universe of well-known large-cap
stocks with the same maths the bot's local strategy already uses, rank by
trend/momentum, and buy the top qualifying pick.

    python -m scripts.algo_buy                    # buy £5 of the best pick
    python -m scripts.algo_buy --list              # just show the ranking
    python -m scripts.algo_buy --amount 20
    python -m scripts.algo_buy --dry-run
    python -m scripts.algo_buy --force             # buy #1 even if it fails the filter
    python -m scripts.algo_buy --universe mine.txt # one T212 ticker per line

WHAT "ALGORITHM" MEANS HERE: the same deterministic scoring
t212bot/indicators.py and t212bot/local_strategy.py already use for the
scheduled bot's own rule-based fallback — SMA trend, RSI, annualised
volatility, momentum — turned into one score per instrument. A candidate is
"buyable" when local_strategy's own entry rule would take it: bullish trend,
score above ai.local_strategy.min_entry_score (override with --min-score),
RSI under 78, volatility under ai.local_strategy.max_entry_vol_pct (override
with --max-vol). This is not a prediction and it is not new maths — it is the
one rule-based system already in this repo, run once, by hand, outside the
scheduler.

THE UNIVERSE is a short, static list of well-known large, liquid US stocks
(below) — an approximation of "the big, well-known names," not a live
top-100-by-market-cap feed pulled from anywhere. Point --universe at a text
file (one Trading212 ticker per line, e.g. TSLA_US_EQ) to screen your own
list instead.

ENTRY ONLY. "1-14 day" is what local_strategy's own thresholds are tuned for
(8% stop-loss, 15% take-profit, trend exit), but that logic only runs on
tickers in config.yaml's watch-list during a scheduled cycle. A ticker bought
here and left off the watch-list is not managed by anything — no stop-loss,
no take-profit — until you add it there or close it by hand in the app. This
script says so again at the end, after it buys.

No risk manager, no capital caps, no market-hours gate, no database. MODE in
.env decides the account: paper refuses, demo is the practice account, live
spends real money. No confirmation prompt.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from t212bot.config import ConfigError, load
from t212bot.fx import FxError
from t212bot.indicators import compute_signals
from t212bot.market_data import MarketDataError, YahooMarketData
from t212bot.models import ZERO, Quote, money
from t212bot.quickbuy import place_with_precision_retry, size_order, to_gbp
from t212bot.t212_client import (
    T212APIError,
    T212AuthError,
    T212Client,
    T212Error,
    free_cash,
)

# Trading212 ticker, Yahoo symbol, display name. All US-listed and USD-quoted,
# so pricing is one FX leg (USD -> GBP) for every candidate — see the module
# docstring for what this list is and is not.
DEFAULT_UNIVERSE: tuple[tuple[str, str, str], ...] = (
    ("AAPL_US_EQ", "AAPL", "Apple"),
    ("MSFT_US_EQ", "MSFT", "Microsoft"),
    ("GOOGL_US_EQ", "GOOGL", "Alphabet"),
    ("AMZN_US_EQ", "AMZN", "Amazon"),
    ("NVDA_US_EQ", "NVDA", "Nvidia"),
    ("META_US_EQ", "META", "Meta"),
    ("TSLA_US_EQ", "TSLA", "Tesla"),
    ("JPM_US_EQ", "JPM", "JPMorgan Chase"),
    ("V_US_EQ", "V", "Visa"),
    ("MA_US_EQ", "MA", "Mastercard"),
    ("UNH_US_EQ", "UNH", "UnitedHealth"),
    ("JNJ_US_EQ", "JNJ", "Johnson & Johnson"),
    ("XOM_US_EQ", "XOM", "Exxon Mobil"),
    ("WMT_US_EQ", "WMT", "Walmart"),
    ("PG_US_EQ", "PG", "Procter & Gamble"),
    ("HD_US_EQ", "HD", "Home Depot"),
    ("CVX_US_EQ", "CVX", "Chevron"),
    ("MRK_US_EQ", "MRK", "Merck"),
    ("ABBV_US_EQ", "ABBV", "AbbVie"),
    ("COST_US_EQ", "COST", "Costco"),
    ("KO_US_EQ", "KO", "Coca-Cola"),
    ("PEP_US_EQ", "PEP", "PepsiCo"),
    ("BAC_US_EQ", "BAC", "Bank of America"),
    ("NFLX_US_EQ", "NFLX", "Netflix"),
    ("AMD_US_EQ", "AMD", "AMD"),
    ("ADBE_US_EQ", "ADBE", "Adobe"),
    ("CRM_US_EQ", "CRM", "Salesforce"),
    ("ORCL_US_EQ", "ORCL", "Oracle"),
    ("CSCO_US_EQ", "CSCO", "Cisco"),
    ("AVGO_US_EQ", "AVGO", "Broadcom"),
    ("QCOM_US_EQ", "QCOM", "Qualcomm"),
    ("INTC_US_EQ", "INTC", "Intel"),
    ("DIS_US_EQ", "DIS", "Disney"),
    ("MCD_US_EQ", "MCD", "McDonald's"),
    ("NKE_US_EQ", "NKE", "Nike"),
    ("SBUX_US_EQ", "SBUX", "Starbucks"),
    ("IBM_US_EQ", "IBM", "IBM"),
    ("CAT_US_EQ", "CAT", "Caterpillar"),
    ("BA_US_EQ", "BA", "Boeing"),
    ("GS_US_EQ", "GS", "Goldman Sachs"),
    ("MS_US_EQ", "MS", "Morgan Stanley"),
    ("AXP_US_EQ", "AXP", "American Express"),
    ("PFE_US_EQ", "PFE", "Pfizer"),
    ("LLY_US_EQ", "LLY", "Eli Lilly"),
    ("TMO_US_EQ", "TMO", "Thermo Fisher"),
    ("PYPL_US_EQ", "PYPL", "PayPal"),
    ("UBER_US_EQ", "UBER", "Uber"),
    ("GE_US_EQ", "GE", "General Electric"),
    ("HON_US_EQ", "HON", "Honeywell"),
)


def load_universe(path: str | None) -> list[tuple[str, str, str]]:
    """The built-in list, or one ticker per line from ``path``.

    A custom file only gives tickers, so the Yahoo symbol is guessed as the
    ticker's US-suffix-stripped form (``TSLA_US_EQ`` -> ``TSLA``), and the
    display name is just the ticker. That guess is wrong for a non-US line —
    the same caution as buy_nvidia's ISIN resolution applies here, this
    script just doesn't do it, so keep custom universes to ``_US_EQ`` tickers.
    """
    if not path:
        return list(DEFAULT_UNIVERSE)
    lines = [ln.strip() for ln in Path(path).read_text().splitlines()]
    tickers = [ln for ln in lines if ln and not ln.startswith("#")]
    if not tickers:
        raise SystemExit(f"!! {path} has no tickers (one per line).")
    return [(t, t.removesuffix("_US_EQ"), t) for t in tickers]


class Candidate:
    def __init__(self, ticker: str, yahoo: str, name: str):
        self.ticker = ticker
        self.yahoo = yahoo
        self.name = name
        self.quote: Quote | None = None
        self.signal = None
        self.skip_reason: str | None = None


def screen(candidates: list[Candidate], config, *, quiet: bool = False) -> None:
    """Fetch and score every candidate, in place. Failures are recorded, not raised."""
    market = YahooMarketData()
    try:
        for i, c in enumerate(candidates, 1):
            if not quiet:
                print(f"  [{i}/{len(candidates)}] {c.ticker:<14} ...", end="", flush=True)
            try:
                quote, history = market.fetch_symbol(
                    c.ticker, c.yahoo, history_days=config.max_history_days
                )
            except MarketDataError as exc:
                c.skip_reason = f"no data ({exc})"
                if not quiet:
                    print(f" skip: {c.skip_reason}")
                continue
            c.quote = quote
            c.signal = compute_signals(history, quote, config.ai.indicators)
            if c.signal is None:
                c.skip_reason = "not enough daily closes yet"
                if not quiet:
                    print(f" skip: {c.skip_reason}")
                continue
            if not quiet:
                print(
                    f" {c.signal.trend:<8} score {c.signal.score:+.2f}  "
                    f"rsi {c.signal.rsi if c.signal.rsi is not None else '-'}"
                )
    finally:
        market.close()


def passes_filter(c: Candidate, min_score: Decimal, max_vol: Decimal) -> bool:
    s = c.signal
    if s is None:
        return False
    return (
        s.trend == "bullish"
        and s.score >= min_score
        and (s.rsi is None or s.rsi < Decimal(78))
        and (s.vol_annual_pct is None or s.vol_annual_pct < max_vol)
    )


def print_ranking(candidates: list[Candidate], min_score: Decimal, max_vol: Decimal) -> None:
    scored = sorted(
        (c for c in candidates if c.signal is not None),
        key=lambda c: c.signal.score,
        reverse=True,
    )
    print(f"\n{'#':<3} {'Ticker':<14} {'Name':<20} {'Trend':<9} {'Score':>6}  "
          f"{'RSI':>5}  {'Vol%':>6}  Buyable")
    for rank, c in enumerate(scored, 1):
        s = c.signal
        buyable = "yes" if passes_filter(c, min_score, max_vol) else ""
        rsi = f"{s.rsi:.0f}" if s.rsi is not None else "-"
        vol = f"{s.vol_annual_pct:.0f}" if s.vol_annual_pct is not None else "-"
        print(f"{rank:<3} {c.ticker:<14} {c.name[:20]:<20} {s.trend:<9} "
              f"{s.score:>+6.2f}  {rsi:>5}  {vol:>6}  {buyable}")
    skipped = [c for c in candidates if c.signal is None]
    if skipped:
        print(f"\n{len(skipped)} skipped: " + ", ".join(f"{c.ticker} ({c.skip_reason})" for c in skipped))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="algo_buy",
        description="Screen a stock universe with local trend/momentum scoring, buy the top pick.",
    )
    parser.add_argument("--amount", type=Decimal, default=Decimal(5),
                        help="how much to spend, in GBP (default: 5)")
    parser.add_argument("--list", action="store_true",
                        help="print the ranking and exit; buy nothing")
    parser.add_argument("--force", action="store_true",
                        help="buy the top-ranked candidate even if it fails the entry filter")
    parser.add_argument("--min-score", type=Decimal, default=None,
                        help="override ai.local_strategy.min_entry_score")
    parser.add_argument("--max-vol", type=Decimal, default=None,
                        help="override ai.local_strategy.max_entry_vol_pct")
    parser.add_argument("--universe", default=None,
                        help="path to a file of Trading212 tickers, one per line")
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

    min_score = args.min_score if args.min_score is not None else config.ai.local_strategy.min_entry_score
    max_vol = args.max_vol if args.max_vol is not None else config.ai.local_strategy.max_entry_vol_pct

    universe = load_universe(args.universe)
    print(f"screening {len(universe)} instrument(s) — bullish, score >= {min_score}, "
          f"RSI < 78, volatility < {max_vol}%")
    candidates = [Candidate(t, y, n) for t, y, n in universe]
    screen(candidates, config)
    print_ranking(candidates, min_score, max_vol)

    if args.list:
        return 0

    scored = sorted(
        (c for c in candidates if c.signal is not None),
        key=lambda c: c.signal.score,
        reverse=True,
    )
    buyable = [c for c in scored if passes_filter(c, min_score, max_vol)]

    if buyable:
        chosen = buyable[0]
    elif args.force and scored:
        chosen = scored[0]
        print(f"\n!! --force: buying {chosen.ticker} despite failing the entry filter "
              f"(trend={chosen.signal.trend}, score={chosen.signal.score:+.2f})")
    else:
        print("\nNo candidate meets the entry rule right now. Nothing bought.", file=sys.stderr)
        print("Use --force to buy the top-ranked one anyway, or --list to just see the table.",
              file=sys.stderr)
        return 1

    print(f"\nchosen: {chosen.ticker} ({chosen.name}) — "
          f"trend {chosen.signal.trend}, score {chosen.signal.score:+.2f}")
    print(f"mode:        {config.mode}" + ("   *** REAL MONEY ***" if config.is_live else ""))
    print(f"base url:    {config.t212_base_url}")

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
            cash = client.account_cash()
            available = free_cash(cash)
            print(f"free cash:   {money(available)}")
            if available < args.amount:
                print(f"!! only {money(available)} available — the broker will refuse "
                      f"a {money(args.amount)} order.")

            price = to_gbp(chosen.quote.price, chosen.quote.currency)
            print(f"price:       {money(price)} GBP/share")

            quantity = size_order(args.amount, price, config.execution.quantity_decimals)
            print(f"quantity:    {money(args.amount)} / {money(price)} = {quantity} shares")
            if quantity <= ZERO:
                print(f"\n!! {money(args.amount)} does not buy a tradeable quantity of "
                      f"{chosen.ticker} at {money(price)}.", file=sys.stderr)
                return 1

            print(f"\nplacing a market order: BUY {quantity} {chosen.ticker}")
            if args.dry_run:
                print("--dry-run: nothing sent.")
                return 0

            response = place_with_precision_retry(client, chosen.ticker, quantity, price)
            print("the broker replied:")
            print(json.dumps(response, indent=2, default=str))

    except T212AuthError as exc:
        print(f"\n!! authentication failed: {exc}", file=sys.stderr)
        return 1
    except T212APIError as exc:
        print(f"\n!! the broker refused it: HTTP {exc.status_code}\n   {exc.body[:800]}",
              file=sys.stderr)
        return 1
    except (MarketDataError, FxError) as exc:
        print(f"\n!! could not price {chosen.ticker}: {exc}", file=sys.stderr)
        return 1
    except T212Error as exc:
        print(f"\n!! Trading212 call failed: {exc}", file=sys.stderr)
        return 1

    print(
        f"\nDone. Check the Trading212 app for {chosen.ticker}.\n"
        f"This script only entered the trade. For the medium-term (1-14 day) exit — "
        f"stop-loss, trend exit, take-profit — add {chosen.ticker} to config.yaml's "
        f"watch-list so the scheduled bot manages it, or watch it yourself."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
