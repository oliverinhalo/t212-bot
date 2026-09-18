"""The standalone NVIDIA buy script: listing choice and symbol derivation."""

from __future__ import annotations

from scripts.buy_nvidia import NVIDIA_ISIN, _pick
from t212bot.instruments import Instrument, InstrumentCatalogue
from t212bot.market_data import yahoo_symbol_for


def _instrument(ticker: str, currency: str) -> Instrument:
    return Instrument(
        ticker=ticker, name="Nvidia", short_name="NVDA",
        isin=NVIDIA_ISIN, currency=currency, type="STOCK",
    )


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
