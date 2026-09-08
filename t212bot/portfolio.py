"""Assemble the account state the risk manager reasons about.

In paper mode this is the virtual ledger in SQLite, marked to market with the
latest quotes. In demo/live mode it is whatever Trading212 says, because the
broker is the only authority on what we actually own.

Every figure the risk manager sees is GBP. Trading212 reports a position's
``averagePrice`` / ``currentPrice`` in the instrument's own currency, so a
foreign holding is converted here using the instrument catalogue (for the
currency) and the FX layer.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from decimal import Decimal
from typing import Mapping

from .config import AppConfig
from .fx import FxConverter, FxError
from .instruments import InstrumentCatalogue
from .models import ZERO, AccountState, Position, utcnow
from .storage import Storage, positions_from_paper
from .t212_client import T212Client, free_cash, positions_from_portfolio

log = logging.getLogger(__name__)


def load_account_state(
    config: AppConfig,
    storage: Storage,
    client: T212Client | None,
    prices: Mapping[str, Decimal],
    *,
    catalogue: InstrumentCatalogue | None = None,
    fx: FxConverter | None = None,
) -> AccountState:
    if config.mode == "paper":
        return _paper_state(config, storage, prices)
    if client is None:
        raise ValueError(f"MODE={config.mode} needs a Trading212 client")
    return _broker_state(client, catalogue, fx)


def _paper_state(
    config: AppConfig, storage: Storage, prices: Mapping[str, Decimal]
) -> AccountState:
    # Seeded once, at max_capital, so the virtual account starts where the real
    # one is capped. Re-seed deliberately with --seed-paper.
    storage.seed_paper_account(config.capital.max_capital)
    positions = positions_from_paper(storage.paper_positions(), prices)
    return AccountState(
        cash=storage.paper_cash(),
        positions=positions,
        as_of=utcnow(),
        source="paper",
    )


def _to_gbp_position(
    position: Position,
    catalogue: InstrumentCatalogue | None,
    fx: FxConverter | None,
) -> Position:
    """Convert a broker position's prices to GBP when the instrument is foreign."""
    instrument = catalogue.get(position.ticker) if catalogue else None
    currency = instrument.currency if instrument else "GBP"
    if currency in ("", "GBP") or fx is None:
        return position
    try:
        return replace(
            position,
            average_price=fx.to_gbp(position.average_price, currency),
            current_price=fx.to_gbp(position.current_price, currency),
        )
    except FxError as exc:
        log.warning(
            "could not convert %s (%s) to GBP: %s — leaving prices as-is",
            position.ticker, currency, exc,
        )
        return position


def _broker_state(
    client: T212Client,
    catalogue: InstrumentCatalogue | None,
    fx: FxConverter | None,
) -> AccountState:
    cash_payload = client.account_cash()
    positions = tuple(
        _to_gbp_position(p, catalogue, fx)
        for p in positions_from_portfolio(client.portfolio())
        if p.quantity > ZERO
    )
    return AccountState(
        cash=free_cash(cash_payload),
        positions=positions,
        as_of=utcnow(),
        source="broker",
    )
