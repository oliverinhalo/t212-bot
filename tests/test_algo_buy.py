"""The algorithmic multi-stock screen-and-buy script: no AI, reused local maths."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from scripts.algo_buy import Candidate, load_universe, passes_filter, print_ranking, screen
from t212bot.config import IndicatorConfig
from t212bot.market_data import MarketDataError
from t212bot.models import Bar, PriceHistory, Quote, dec, utcnow

CFG = IndicatorConfig()


def _history(closes, ticker="X") -> PriceHistory:
    start = date(2026, 1, 1)
    return PriceHistory(
        ticker=ticker,
        bars=tuple(Bar(day=start + timedelta(days=i), close=dec(c)) for i, c in enumerate(closes)),
    )


def _quote(price, ticker="X") -> Quote:
    return Quote(ticker=ticker, price=dec(price), currency="USD", as_of=utcnow(), source="t")


def _fake_config() -> SimpleNamespace:
    return SimpleNamespace(max_history_days=90, ai=SimpleNamespace(indicators=CFG))


# --------------------------------------------------------------------------- #
# load_universe
# --------------------------------------------------------------------------- #


def test_the_default_universe_has_many_well_known_us_tickers():
    universe = load_universe(None)
    assert len(universe) > 20
    tickers = {t for t, _, _ in universe}
    assert "TSLA_US_EQ" in tickers
    assert "NVDA_US_EQ" in tickers
    assert all(t.endswith("_US_EQ") for t, _, _ in universe)


def test_a_custom_universe_file_is_one_ticker_per_line(tmp_path):
    path = tmp_path / "universe.txt"
    path.write_text("AAPL_US_EQ\n# a comment\n\nMSFT_US_EQ\n")

    universe = load_universe(str(path))

    assert universe == [
        ("AAPL_US_EQ", "AAPL", "AAPL_US_EQ"),
        ("MSFT_US_EQ", "MSFT", "MSFT_US_EQ"),
    ]


def test_an_empty_universe_file_refuses(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("\n# only comments\n")

    with pytest.raises(SystemExit, match="no tickers"):
        load_universe(str(path))


# --------------------------------------------------------------------------- #
# passes_filter
# --------------------------------------------------------------------------- #


def _candidate_with_signal(*, trend, score, rsi=None, vol=None) -> Candidate:
    c = Candidate("X_US_EQ", "X", "X Corp")
    history = _history([100 + i for i in range(60)])
    quote = _quote(history.bars[-1].close)
    c.quote = quote
    c.signal = compute_signals_stub(trend, score, rsi, vol, history, quote)
    return c


def compute_signals_stub(trend, score, rsi, vol, history, quote):
    from t212bot.indicators import TechnicalSignals

    return TechnicalSignals(
        ticker="X", last=quote.price, bars=len(history.bars),
        sma_short=None, sma_mid=None, sma_long=None,
        rsi=dec(rsi) if rsi is not None else None,
        vol_annual_pct=dec(vol) if vol is not None else None,
        max_drawdown_pct=None, roc_5=None, roc_10=None, roc_20=None,
        dist_from_high_pct=None, dist_from_low_pct=None, z_score=None,
        trend=trend, score=dec(score), notes=(),
    )


def test_a_candidate_with_no_signal_never_passes():
    c = Candidate("X_US_EQ", "X", "X Corp")
    assert c.signal is None
    assert passes_filter(c, dec("0.35"), dec(35)) is False


def test_bullish_above_threshold_passes():
    c = _candidate_with_signal(trend="bullish", score="0.50", rsi=60, vol=20)
    assert passes_filter(c, dec("0.35"), dec(35)) is True


def test_neutral_trend_never_passes_even_with_a_high_score():
    c = _candidate_with_signal(trend="neutral", score="0.50", rsi=60, vol=20)
    assert passes_filter(c, dec("0.35"), dec(35)) is False


def test_score_below_the_minimum_does_not_pass():
    c = _candidate_with_signal(trend="bullish", score="0.20", rsi=60, vol=20)
    assert passes_filter(c, dec("0.35"), dec(35)) is False


def test_overbought_rsi_does_not_pass():
    c = _candidate_with_signal(trend="bullish", score="0.50", rsi=80, vol=20)
    assert passes_filter(c, dec("0.35"), dec(35)) is False


def test_volatility_at_or_above_the_max_does_not_pass():
    c = _candidate_with_signal(trend="bullish", score="0.50", rsi=60, vol=35)
    assert passes_filter(c, dec("0.35"), dec(35)) is False


# --------------------------------------------------------------------------- #
# screen
# --------------------------------------------------------------------------- #


class _FakeMarket:
    """Answers fetch_symbol from a canned table, keyed by ticker."""

    def __init__(self, table: dict):
        self._table = table
        self.closed = False

    def fetch_symbol(self, ticker, yahoo, history_days):
        result = self._table[ticker]
        if isinstance(result, Exception):
            raise result
        return result

    def close(self):
        self.closed = True


def test_screen_scores_every_candidate_it_can_price(monkeypatch):
    closes = [100 + i for i in range(60)]
    history = _history(closes, ticker="AAPL_US_EQ")
    quote = _quote(closes[-1], ticker="AAPL_US_EQ")
    market = _FakeMarket({"AAPL_US_EQ": (quote, history)})
    monkeypatch.setattr("scripts.algo_buy.YahooMarketData", lambda: market)

    candidates = [Candidate("AAPL_US_EQ", "AAPL", "Apple")]
    screen(candidates, _fake_config(), quiet=True)

    assert candidates[0].quote is quote
    assert candidates[0].signal is not None
    assert candidates[0].skip_reason is None
    assert market.closed is True


def test_screen_records_a_skip_reason_when_data_is_unavailable_and_keeps_going(monkeypatch):
    closes = [100 + i for i in range(60)]
    good_history = _history(closes, ticker="MSFT_US_EQ")
    good_quote = _quote(closes[-1], ticker="MSFT_US_EQ")
    market = _FakeMarket({
        "BOGUS_US_EQ": MarketDataError("no such ticker"),
        "MSFT_US_EQ": (good_quote, good_history),
    })
    monkeypatch.setattr("scripts.algo_buy.YahooMarketData", lambda: market)

    candidates = [Candidate("BOGUS_US_EQ", "BOGUS", "Bogus Inc"), Candidate("MSFT_US_EQ", "MSFT", "Microsoft")]
    screen(candidates, _fake_config(), quiet=True)

    assert candidates[0].signal is None
    assert "no such ticker" in candidates[0].skip_reason
    assert candidates[1].signal is not None


def test_screen_skips_a_candidate_with_too_little_history(monkeypatch):
    short_history = _history([100, 101, 102], ticker="NEW_US_EQ")
    quote = _quote(102, ticker="NEW_US_EQ")
    market = _FakeMarket({"NEW_US_EQ": (quote, short_history)})
    monkeypatch.setattr("scripts.algo_buy.YahooMarketData", lambda: market)

    candidates = [Candidate("NEW_US_EQ", "NEW", "New Co")]
    screen(candidates, _fake_config(), quiet=True)

    assert candidates[0].signal is None
    assert candidates[0].skip_reason == "not enough daily closes yet"


def test_print_ranking_does_not_blow_up_on_an_empty_list(capsys):
    print_ranking([], dec("0.35"), dec(35))
    out = capsys.readouterr().out
    assert "Ticker" in out
