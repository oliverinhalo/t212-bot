"""Assemble the account state the risk manager reasons about.

In paper mode this is the virtual ledger in SQLite, marked to market with the
latest quotes. In demo/live mode it is whatever Trading212 says, because the
broker is the only authority on what we actually own.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Mapping

from .config import AppConfig
from .models import AccountState, utcnow
from .storage import Storage, positions_from_paper
from .t212_client import T212Client, free_cash, positions_from_portfolio

log = logging.getLogger(__name__)


def load_account_state(
    config: AppConfig,
    storage: Storage,
    client: T212Client | None,
    prices: Mapping[str, Decimal],
) -> AccountState:
    if config.mode == "paper":
        return _paper_state(config, storage, prices)
    if client is None:
        raise ValueError(f"MODE={config.mode} needs a Trading212 client")
    return _broker_state(client)


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


def _broker_state(client: T212Client) -> AccountState:
    cash_payload = client.account_cash()
    positions = positions_from_portfolio(client.portfolio())
    return AccountState(
        cash=free_cash(cash_payload),
        positions=positions,
        as_of=utcnow(),
        source="broker",
    )
