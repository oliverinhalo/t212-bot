"""The Trading212 instrument catalogue and name resolution."""

from __future__ import annotations

import json

from t212bot.instruments import Instrument, InstrumentCatalogue

ROWS = [
    {"ticker": "AAPL_US_EQ", "type": "STOCK", "isin": "US0378331005",
     "currencyCode": "USD", "name": "Apple", "shortName": "AAPL"},
    {"ticker": "MSFT_US_EQ", "type": "STOCK", "isin": "US5949181045",
     "currencyCode": "USD", "name": "Microsoft", "shortName": "MSFT"},
    {"ticker": "VUSAl_EQ", "type": "ETF", "isin": "IE00B3XXRP09",
     "currencyCode": "GBP", "name": "Vanguard S&P 500 (Dist)", "shortName": "VUSA"},
    {"ticker": "3AAPl_EQ", "type": "ETF", "isin": "XS1111111111",
     "currencyCode": "GBX", "name": "Leverage Shares 3x Apple", "shortName": "3AAP"},
    # two instruments sharing a short name, to test ambiguity
    {"ticker": "SAPd_EQ", "type": "STOCK", "isin": "DE0007164600",
     "currencyCode": "EUR", "name": "SAP", "shortName": "SAP"},
    {"ticker": "SAP_US_EQ", "type": "STOCK", "isin": "US8030542042",
     "currencyCode": "USD", "name": "SAP ADR", "shortName": "SAP"},
]


def _cat() -> InstrumentCatalogue:
    return InstrumentCatalogue(
        i for i in (
            Instrument(
                ticker=r["ticker"], name=r["name"], short_name=r["shortName"],
                isin=r["isin"], currency=r["currencyCode"], type=r["type"],
            )
            for r in ROWS
        )
    )


def test_load_returns_none_when_the_file_is_missing(tmp_path):
    assert InstrumentCatalogue.load(tmp_path / "nope.json") is None


def test_load_parses_the_real_payload_shape(tmp_path):
    path = tmp_path / "instruments.json"
    path.write_text(json.dumps(ROWS))
    cat = InstrumentCatalogue.load(path)
    assert cat is not None and len(cat) == len(ROWS)
    apple = cat.get("AAPL_US_EQ")
    assert apple.currency == "USD" and apple.short_name == "AAPL"


def test_resolve_exact_ticker():
    assert _cat().resolve("MSFT_US_EQ").ticker == "MSFT_US_EQ"


def test_resolve_unique_short_name():
    assert _cat().resolve("aapl").ticker == "AAPL_US_EQ"


def test_resolve_by_isin():
    assert _cat().resolve("IE00B3XXRP09").ticker == "VUSAl_EQ"


def test_resolve_unique_name_substring():
    assert _cat().resolve("Microsoft").ticker == "MSFT_US_EQ"


def test_ambiguous_short_name_does_not_resolve():
    assert _cat().resolve("SAP") is None


def test_unknown_query_resolves_to_none():
    assert _cat().resolve("Wingdings Corp") is None
    assert _cat().resolve("") is None
