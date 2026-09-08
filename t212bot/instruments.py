"""The Trading212 instrument catalogue — the safety net for open-universe mode.

When ``risk.enforce_allowlist`` is off, the AI may name any instrument. Every
name it gives is resolved against this catalogue before anything else happens:
an unknown or invented ticker resolves to ``None`` and is rejected (R05). The
catalogue also supplies the currency, which the FX layer needs to value a
foreign instrument in GBP.

The catalogue is the ``data/instruments.json`` file produced by
``python -m scripts.list_instruments --refresh`` (the T212 metadata endpoint is
several MB and heavily rate-limited, so it is fetched by that script, never from
a trading cycle).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Instrument:
    ticker: str        # exact Trading212 ticker, e.g. "AAPL_US_EQ"
    name: str          # e.g. "Apple"
    short_name: str    # e.g. "AAPL"
    isin: str
    currency: str      # "USD" | "EUR" | "GBP" | "GBX" | ...
    type: str          # "STOCK" | "ETF" | ...

    @property
    def is_equity_like(self) -> bool:
        return self.type in ("STOCK", "ETF")


def _instrument_from_row(row: dict) -> Instrument | None:
    ticker = str(row.get("ticker", "")).strip()
    if not ticker:
        return None
    return Instrument(
        ticker=ticker,
        name=str(row.get("name", "") or ticker),
        short_name=str(row.get("shortName", "") or "").strip(),
        isin=str(row.get("isin", "") or "").strip(),
        currency=str(row.get("currencyCode", "") or "").strip().upper(),
        type=str(row.get("type", "") or "").strip().upper(),
    )


class InstrumentCatalogue:
    """Read-only lookup over the cached T212 instrument universe."""

    def __init__(self, instruments: Iterable[Instrument]):
        self._by_ticker: dict[str, Instrument] = {}
        self._by_short: dict[str, list[Instrument]] = {}
        self._by_isin: dict[str, Instrument] = {}
        for inst in instruments:
            self._by_ticker[inst.ticker] = inst
            if inst.short_name:
                self._by_short.setdefault(inst.short_name.upper(), []).append(inst)
            if inst.isin:
                self._by_isin.setdefault(inst.isin.upper(), inst)

    def __len__(self) -> int:
        return len(self._by_ticker)

    @classmethod
    def load(cls, path: str | Path) -> "InstrumentCatalogue | None":
        """Build from ``data/instruments.json``. ``None`` if the file is absent."""
        path = Path(path)
        if not path.exists():
            return None
        try:
            rows = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:  # pragma: no cover - defensive
            log.error("could not read instrument catalogue %s: %s", path, exc)
            return None
        instruments = [
            inst for inst in (_instrument_from_row(r) for r in rows) if inst is not None
        ]
        log.info("loaded %d instruments from %s", len(instruments), path)
        return cls(instruments)

    def get(self, ticker: str) -> Instrument | None:
        """Exact Trading212-ticker lookup."""
        return self._by_ticker.get((ticker or "").strip())

    def resolve(self, query: str) -> Instrument | None:
        """Best-effort resolution of whatever the AI named.

        Precedence, each step only accepting an unambiguous hit:
          1. exact T212 ticker           ("AAPL_US_EQ")
          2. exact short name             ("AAPL")  — must be unique
          3. exact ISIN
          4. unique case-insensitive name substring ("Apple")
        Anything ambiguous or unmatched returns ``None`` and the caller rejects.
        """
        q = (query or "").strip()
        if not q:
            return None

        exact = self._by_ticker.get(q)
        if exact is not None:
            return exact

        shorts = self._by_short.get(q.upper())
        if shorts:
            equities = [i for i in shorts if i.is_equity_like]
            pool = equities or shorts
            if len(pool) == 1:
                return pool[0]

        by_isin = self._by_isin.get(q.upper())
        if by_isin is not None:
            return by_isin

        needle = q.lower()
        name_hits = [
            i for i in self._by_ticker.values()
            if i.is_equity_like and needle in i.name.lower()
        ]
        if len(name_hits) == 1:
            return name_hits[0]

        return None


__all__ = ["Instrument", "InstrumentCatalogue"]
