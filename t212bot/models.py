"""Shared value types.

Everything that touches money is a ``Decimal``. Floats are only allowed at the
edges (JSON/YAML parsing, the API wire format) and are converted immediately via
``dec()`` so that no rounding error can ever reach a capital check.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable, Literal, Mapping, Sequence

Action = Literal["buy", "sell", "hold"]

ZERO = Decimal("0")


def dec(value: object) -> Decimal:
    """Convert anything number-ish to Decimal without float noise.

    Floats go via ``repr`` so 0.1 becomes Decimal("0.1"), not the binary
    expansion. Raises ValueError on anything that is not a finite number.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise ValueError(f"refusing to treat bool as a number: {value!r}")
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite number: {value!r}")
        result = Decimal(repr(value))
    elif isinstance(value, str):
        try:
            result = Decimal(value.strip())
        except InvalidOperation as exc:
            raise ValueError(f"not a number: {value!r}") from exc
    else:
        raise ValueError(f"not a number: {value!r}")

    if not result.is_finite():
        raise ValueError(f"non-finite number: {value!r}")
    return result


def maybe_dec(value: object) -> Decimal | None:
    """``dec()`` but ``None`` passes through, and blanks become ``None``."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return dec(value)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def money(value: Decimal) -> Decimal:
    """Round to whole pence, half-up, for display and P&L bookkeeping."""
    return value.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")


@dataclass(frozen=True)
class WatchItem:
    """One entry from the config allow-list."""

    ticker: str
    yahoo: str
    name: str
    max_position_pct: Decimal | None = None


@dataclass(frozen=True)
class Quote:
    ticker: str
    price: Decimal
    currency: str
    as_of: datetime
    source: str

    def age_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.as_of).total_seconds()


@dataclass(frozen=True)
class Bar:
    """One daily close."""

    day: date
    close: Decimal


@dataclass(frozen=True)
class PriceHistory:
    ticker: str
    bars: tuple[Bar, ...]

    def pct_change(self, days: int) -> Decimal | None:
        """Percent change over the last ``days`` bars, or None if too short."""
        if len(self.bars) < days + 1:
            return None
        old = self.bars[-(days + 1)].close
        new = self.bars[-1].close
        if old == ZERO:
            return None
        return (new - old) / old * Decimal(100)


@dataclass(frozen=True)
class Position:
    ticker: str
    quantity: Decimal
    average_price: Decimal
    current_price: Decimal

    @property
    def value(self) -> Decimal:
        return self.quantity * self.current_price

    @property
    def unrealised_pnl(self) -> Decimal:
        return (self.current_price - self.average_price) * self.quantity


@dataclass(frozen=True)
class AccountState:
    """Cash plus positions, as of ``as_of``.

    ``source`` is "paper" (virtual ledger) or "broker" (fetched from T212).
    """

    cash: Decimal
    positions: tuple[Position, ...]
    as_of: datetime
    source: str
    currency: str = "GBP"

    @property
    def invested(self) -> Decimal:
        return sum((p.value for p in self.positions), ZERO)

    @property
    def equity(self) -> Decimal:
        return self.cash + self.invested

    @property
    def unrealised_pnl(self) -> Decimal:
        return sum((p.unrealised_pnl for p in self.positions), ZERO)

    def position(self, ticker: str) -> Position | None:
        for p in self.positions:
            if p.ticker == ticker:
                return p
        return None

    def quantity_of(self, ticker: str) -> Decimal:
        p = self.position(ticker)
        return p.quantity if p else ZERO

    def value_of(self, ticker: str) -> Decimal:
        p = self.position(ticker)
        return p.value if p else ZERO


@dataclass(frozen=True)
class Proposal:
    """What the AI asked for. Never trusted; only ever an input to the risk manager."""

    action: Action
    ticker: str | None = None
    notional: Decimal | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    confidence: Decimal = ZERO
    reasoning: str = ""

    @property
    def is_trade(self) -> bool:
        return self.action in ("buy", "sell")


@dataclass(frozen=True)
class AIResult:
    """A proposal plus the audit trail of how it was produced."""

    proposal: Proposal
    provider: str
    model: str
    prompt: str
    raw_response: str
    latency_ms: int
    error: str | None = None


@dataclass(frozen=True)
class RiskInputs:
    """Everything the risk manager is allowed to look at.

    Assembled by main.py; the risk manager itself performs no I/O, which is what
    makes it exhaustively unit-testable.
    """

    decision_id: str
    account: AccountState
    quotes: Mapping[str, Quote]
    trades_today: int
    day_start_equity: Decimal
    breaker_tripped: bool
    known_decision_ids: frozenset[str] = frozenset()
    unresolved_orders: int = 0
    now: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class Verdict:
    """The risk manager's decision. The only thing executor.py will act on.

    ``quantity`` is signed to match the Trading212 convention: positive buys,
    negative sells. ``approved`` is the single gate.
    """

    approved: bool
    action: Action
    rule: str
    reasons: tuple[str, ...] = ()
    ticker: str | None = None
    quantity: Decimal = ZERO
    notional: Decimal = ZERO
    reference_price: Decimal | None = None
    limit_price: Decimal | None = None
    shrunk: bool = False

    @property
    def side(self) -> str:
        if self.quantity > ZERO:
            return "buy"
        if self.quantity < ZERO:
            return "sell"
        return "none"

    def rejected(self) -> bool:
        return not self.approved


def reject(rule: str, *reasons: str, action: Action = "hold", ticker: str | None = None) -> Verdict:
    return Verdict(
        approved=False,
        action=action,
        rule=rule,
        reasons=tuple(reasons),
        ticker=ticker,
    )


@dataclass(frozen=True)
class OrderRecord:
    """A row from the orders table, as executor/storage exchange it."""

    decision_id: str
    mode: str
    state: str
    ticker: str
    side: str
    quantity: Decimal
    notional: Decimal
    reference_price: Decimal | None = None
    broker_order_id: str | None = None
    fill_price: Decimal | None = None
    fill_quantity: Decimal | None = None
    error: str | None = None
    submitted_at: datetime | None = None
    settled_at: datetime | None = None


def summarise_positions(positions: Iterable[Position]) -> Sequence[dict]:
    """JSON-safe view of positions, for prompts and the audit log."""
    return [
        {
            "ticker": p.ticker,
            "quantity": str(p.quantity),
            "average_price": str(money(p.average_price)),
            "current_price": str(money(p.current_price)),
            "value": str(money(p.value)),
            "unrealised_pnl": str(money(p.unrealised_pnl)),
        }
        for p in positions
    ]
