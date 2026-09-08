"""SQLite audit trail, paper ledger, and the duplicate-order guard.

Design notes that matter for safety:

* **Money is stored as TEXT.** SQLite's REAL is a float, and a float is not a
  thing to keep a capital cap in. Everything round-trips through ``Decimal``.
* **``orders.decision_id`` is the primary key.** One cycle can produce at most
  one order row, and the insert happens *before* the HTTP call. A retry, a
  crash-restart, or a duplicated scheduler tick all collide on that key and are
  refused. This is the guard against T212's non-idempotent order endpoints.
* **Nothing here ever receives a secret.** Prompts and raw responses are logged;
  API keys are not passed into this module at all.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    ZERO,
    AIResult,
    OrderRecord,
    Position,
    Verdict,
    dec,
    maybe_dec,
    utcnow,
)

SCHEMA_VERSION = 1

# Order states. Anything in UNRESOLVED_STATES blocks further trading until a
# human reconciles it, because we do not know whether money moved.
STATE_RESERVED = "reserved"      # row written, request not yet sent
STATE_SUBMITTING = "submitting"  # request in flight
STATE_FILLED = "filled"
STATE_ACCEPTED = "accepted"      # broker accepted, fill not yet confirmed
STATE_REJECTED = "rejected"      # broker refused; no position change
STATE_UNKNOWN = "unknown"        # crashed or timed out mid-flight
STATE_RESOLVED = "resolved"      # a human checked and closed it out
STATE_CANCELLED = "cancelled"    # withdrawn by the pre-submit re-check; never sent

UNRESOLVED_STATES = (STATE_RESERVED, STATE_SUBMITTING, STATE_UNKNOWN)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS cycles (
    decision_id  TEXT PRIMARY KEY,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    trading_day  TEXT NOT NULL,
    mode         TEXT NOT NULL,
    status       TEXT NOT NULL,
    halt_reason  TEXT,
    snapshot     TEXT
);
CREATE INDEX IF NOT EXISTS idx_cycles_day ON cycles (trading_day);

CREATE TABLE IF NOT EXISTS ai_decisions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id   TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    provider      TEXT,
    model         TEXT,
    prompt        TEXT,
    raw_response  TEXT,
    action        TEXT,
    ticker        TEXT,
    notional      TEXT,
    quantity      TEXT,
    confidence    TEXT,
    reasoning     TEXT,
    latency_ms    INTEGER,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_ai_decision ON ai_decisions (decision_id);

CREATE TABLE IF NOT EXISTS risk_verdicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'primary',
    approved        INTEGER NOT NULL,
    action          TEXT,
    rule            TEXT,
    ticker          TEXT,
    quantity        TEXT,
    notional        TEXT,
    reference_price TEXT,
    limit_price     TEXT,
    shrunk          INTEGER NOT NULL DEFAULT 0,
    reasons         TEXT
);
CREATE INDEX IF NOT EXISTS idx_verdict_decision ON risk_verdicts (decision_id);

CREATE TABLE IF NOT EXISTS orders (
    decision_id     TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    submitted_at    TEXT,
    settled_at      TEXT,
    trading_day     TEXT NOT NULL,
    mode            TEXT NOT NULL,
    state           TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        TEXT NOT NULL,
    notional        TEXT NOT NULL,
    reference_price TEXT,
    broker_order_id TEXT,
    fill_price      TEXT,
    fill_quantity   TEXT,
    error           TEXT,
    raw_response    TEXT,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_day ON orders (trading_day);
CREATE INDEX IF NOT EXISTS idx_orders_state ON orders (state);

CREATE TABLE IF NOT EXISTS paper_cash (
    id        INTEGER PRIMARY KEY CHECK (id = 1),
    cash      TEXT NOT NULL,
    seeded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    ticker        TEXT PRIMARY KEY,
    quantity      TEXT NOT NULL,
    average_price TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily (
    trading_day  TEXT PRIMARY KEY,
    start_equity TEXT,
    last_equity  TEXT,
    realised_pnl TEXT NOT NULL DEFAULT '0',
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS breaker (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    tripped_at  TEXT NOT NULL,
    trading_day TEXT NOT NULL,
    reason      TEXT,
    pnl         TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    day        TEXT PRIMARY KEY,
    calls      INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_cycle_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    trading_day TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    action      TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def _txt(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


class Storage:
    """All persistence for the bot. Safe to share across threads."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ----------------------------------------------------------------- setup
    def _migrate(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            row = self._conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,)
                )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _write(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _read(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _read_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self._read(sql, params)
        return rows[0] if rows else None

    # ---------------------------------------------------------------- cycles
    def start_cycle(self, decision_id: str, trading_day: date, mode: str) -> None:
        self._write(
            "INSERT OR IGNORE INTO cycles "
            "(decision_id, started_at, trading_day, mode, status) VALUES (?, ?, ?, ?, 'running')",
            (decision_id, utcnow().isoformat(), trading_day.isoformat(), mode),
        )

    def record_snapshot(self, decision_id: str, snapshot: Mapping[str, Any]) -> None:
        self._write(
            "UPDATE cycles SET snapshot = ? WHERE decision_id = ?",
            (json.dumps(snapshot, sort_keys=True, default=str), decision_id),
        )

    def finish_cycle(self, decision_id: str, status: str, halt_reason: str | None = None) -> None:
        self._write(
            "UPDATE cycles SET finished_at = ?, status = ?, halt_reason = ? WHERE decision_id = ?",
            (utcnow().isoformat(), status, halt_reason, decision_id),
        )

    # ------------------------------------------------------------------- ai
    def record_ai(self, decision_id: str, result: AIResult) -> None:
        p = result.proposal
        self._write(
            "INSERT INTO ai_decisions (decision_id, created_at, provider, model, prompt, "
            "raw_response, action, ticker, notional, quantity, confidence, reasoning, "
            "latency_ms, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                utcnow().isoformat(),
                result.provider,
                result.model,
                result.prompt,
                result.raw_response,
                p.action,
                p.ticker,
                _txt(p.notional),
                _txt(p.quantity),
                _txt(p.confidence),
                p.reasoning,
                result.latency_ms,
                result.error,
            ),
        )

    # --------------------------------------------------------------- verdict
    def record_verdict(self, decision_id: str, verdict: Verdict, stage: str = "primary") -> None:
        self._write(
            "INSERT INTO risk_verdicts (decision_id, created_at, stage, approved, action, rule, "
            "ticker, quantity, notional, reference_price, limit_price, shrunk, reasons) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                decision_id,
                utcnow().isoformat(),
                stage,
                int(verdict.approved),
                verdict.action,
                verdict.rule,
                verdict.ticker,
                _txt(verdict.quantity),
                _txt(verdict.notional),
                _txt(verdict.reference_price),
                _txt(verdict.limit_price),
                int(verdict.shrunk),
                json.dumps(list(verdict.reasons)),
            ),
        )

    # ------------------------------------------------------- ai budget + cache
    def record_ai_calls(self, day: date, n: int) -> None:
        """Add ``n`` OpenRouter HTTP calls to the running total for ``day``."""
        if n <= 0:
            return
        self._write(
            "INSERT INTO ai_usage (day, calls, updated_at) VALUES (?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET calls = ai_usage.calls + excluded.calls, "
            "updated_at = excluded.updated_at",
            (day.isoformat(), int(n), utcnow().isoformat()),
        )

    def ai_calls_today(self, day: date) -> int:
        row = self._read_one("SELECT calls FROM ai_usage WHERE day = ?", (day.isoformat(),))
        return int(row["calls"]) if row else 0

    def record_cycle_fingerprint(self, trading_day: date, fingerprint: str, action: str) -> None:
        """Remember the market/account fingerprint of the last completed cycle."""
        self._write(
            "INSERT INTO ai_cycle_state (id, trading_day, fingerprint, action, updated_at) "
            "VALUES (1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET trading_day = excluded.trading_day, "
            "fingerprint = excluded.fingerprint, action = excluded.action, "
            "updated_at = excluded.updated_at",
            (trading_day.isoformat(), fingerprint, action, utcnow().isoformat()),
        )

    def last_cycle_fingerprint(self) -> tuple[str, str] | None:
        """``(fingerprint, action)`` from the previous cycle, or None."""
        row = self._read_one("SELECT fingerprint, action FROM ai_cycle_state WHERE id = 1")
        return (row["fingerprint"], row["action"]) if row else None

    # ---------------------------------------------------------------- orders
    def reserve_order(
        self, decision_id: str, trading_day: date, mode: str, verdict: Verdict
    ) -> bool:
        """Claim this decision id for an order. Returns False if already claimed.

        This is the duplicate-order guard, and it is deliberately a database
        constraint rather than an in-memory check so that it survives a crash.
        """
        if verdict.ticker is None:
            raise ValueError("cannot reserve an order with no ticker")
        try:
            self._write(
                "INSERT INTO orders (decision_id, created_at, trading_day, mode, state, ticker, "
                "side, quantity, notional, reference_price) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    utcnow().isoformat(),
                    trading_day.isoformat(),
                    mode,
                    STATE_RESERVED,
                    verdict.ticker,
                    verdict.side,
                    str(verdict.quantity),
                    str(verdict.notional),
                    _txt(verdict.reference_price),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def mark_submitting(self, decision_id: str) -> None:
        self._write(
            "UPDATE orders SET state = ?, submitted_at = ? WHERE decision_id = ?",
            (STATE_SUBMITTING, utcnow().isoformat(), decision_id),
        )

    def settle_order(
        self,
        decision_id: str,
        state: str,
        *,
        broker_order_id: str | None = None,
        fill_price: Decimal | None = None,
        fill_quantity: Decimal | None = None,
        error: str | None = None,
        raw_response: Any = None,
    ) -> None:
        self._write(
            "UPDATE orders SET state = ?, settled_at = ?, broker_order_id = ?, fill_price = ?, "
            "fill_quantity = ?, error = ?, raw_response = ? WHERE decision_id = ?",
            (
                state,
                utcnow().isoformat(),
                broker_order_id,
                _txt(fill_price),
                _txt(fill_quantity),
                error,
                None if raw_response is None else json.dumps(raw_response, default=str),
                decision_id,
            ),
        )

    def get_order(self, decision_id: str) -> OrderRecord | None:
        row = self._read_one("SELECT * FROM orders WHERE decision_id = ?", (decision_id,))
        return _order_from_row(row) if row else None

    def known_decision_ids(self) -> frozenset[str]:
        """Every decision id that has ever claimed an order row."""
        return frozenset(r["decision_id"] for r in self._read("SELECT decision_id FROM orders"))

    def orders_today(self, trading_day: date) -> list[OrderRecord]:
        rows = self._read(
            "SELECT * FROM orders WHERE trading_day = ? ORDER BY created_at",
            (trading_day.isoformat(),),
        )
        return [_order_from_row(r) for r in rows]

    def trades_today(self, trading_day: date) -> int:
        """Count every order attempt today.

        Attempts, not fills: a rejected or unknown order still consumed one of
        the day's slots, which is the conservative reading of a frequency cap.
        Only ``cancelled`` rows are excluded, because those were withdrawn by
        the pre-submit re-check and never reached the broker at all.
        """
        row = self._read_one(
            "SELECT COUNT(*) AS n FROM orders WHERE trading_day = ? AND state != ?",
            (trading_day.isoformat(), STATE_CANCELLED),
        )
        return int(row["n"]) if row else 0

    def unresolved_orders(self) -> list[OrderRecord]:
        placeholders = ",".join("?" * len(UNRESOLVED_STATES))
        rows = self._read(
            f"SELECT * FROM orders WHERE state IN ({placeholders}) ORDER BY created_at",
            UNRESOLVED_STATES,
        )
        return [_order_from_row(r) for r in rows]

    def mark_stranded_orders_unknown(self, note: str) -> int:
        """Flag orders left mid-flight by a crash.

        Called at startup. A ``reserved``/``submitting`` row that outlived its
        process might or might not have reached the broker, so it becomes
        ``unknown`` — which blocks trading until a human resolves it. We never
        retry it: retrying a non-idempotent order endpoint is exactly how you
        end up holding twice what you meant to.
        """
        cursor = self._write(
            "UPDATE orders SET state = ?, note = ? WHERE state IN (?, ?)",
            (STATE_UNKNOWN, note, STATE_RESERVED, STATE_SUBMITTING),
        )
        return cursor.rowcount or 0

    def resolve_order(self, decision_id: str, note: str) -> bool:
        cursor = self._write(
            "UPDATE orders SET state = ?, note = ?, settled_at = ? WHERE decision_id = ? "
            "AND state = ?",
            (STATE_RESOLVED, note, utcnow().isoformat(), decision_id, STATE_UNKNOWN),
        )
        return bool(cursor.rowcount)

    # ---------------------------------------------------------- paper ledger
    def seed_paper_account(self, cash: Decimal, *, force: bool = False) -> Decimal:
        """Create the virtual account if absent. Returns the current cash."""
        row = self._read_one("SELECT cash FROM paper_cash WHERE id = 1")
        if row is not None and not force:
            return dec(row["cash"])
        self._write(
            "INSERT INTO paper_cash (id, cash, seeded_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET cash = excluded.cash, seeded_at = excluded.seeded_at",
            (str(cash), utcnow().isoformat()),
        )
        if force:
            self._write("DELETE FROM paper_positions")
        return cash

    def paper_cash(self) -> Decimal:
        row = self._read_one("SELECT cash FROM paper_cash WHERE id = 1")
        return dec(row["cash"]) if row else ZERO

    def paper_positions(self) -> dict[str, tuple[Decimal, Decimal]]:
        """ticker -> (quantity, average_price)"""
        return {
            r["ticker"]: (dec(r["quantity"]), dec(r["average_price"]))
            for r in self._read("SELECT * FROM paper_positions WHERE CAST(quantity AS REAL) > 0")
        }

    def apply_paper_fill(
        self, ticker: str, quantity: Decimal, price: Decimal, trading_day: date
    ) -> Decimal:
        """Apply a simulated fill to the virtual ledger. Returns realised P&L.

        ``quantity`` is signed: positive buys, negative sells, matching the
        Trading212 convention used everywhere else in this codebase.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT quantity, average_price FROM paper_positions WHERE ticker = ?", (ticker,)
            ).fetchone()
            held = dec(row["quantity"]) if row else ZERO
            avg = dec(row["average_price"]) if row else ZERO
            cash = self.paper_cash()
            realised = ZERO

            if quantity > ZERO:
                cost = quantity * price
                if cost > cash:
                    raise ValueError(
                        f"paper buy of {cost} exceeds virtual cash {cash} — "
                        "the risk manager should have prevented this"
                    )
                new_qty = held + quantity
                avg = ((held * avg) + cost) / new_qty if new_qty > ZERO else ZERO
                held = new_qty
                cash -= cost
            else:
                sold = min(-quantity, held)
                if sold <= ZERO:
                    raise ValueError(f"paper sell of {ticker} with nothing held")
                realised = (price - avg) * sold
                held -= sold
                cash += sold * price

            self._conn.execute(
                "INSERT INTO paper_positions (ticker, quantity, average_price, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(ticker) DO UPDATE SET quantity = excluded.quantity, "
                "average_price = excluded.average_price, updated_at = excluded.updated_at",
                (ticker, str(held), str(avg), utcnow().isoformat()),
            )
            self._conn.execute("UPDATE paper_cash SET cash = ? WHERE id = 1", (str(cash),))
            self._conn.commit()

        if realised != ZERO:
            self.add_realised_pnl(trading_day, realised)
        return realised

    # ----------------------------------------------------------------- daily
    def day_start_equity(self, trading_day: date) -> Decimal | None:
        row = self._read_one(
            "SELECT start_equity FROM daily WHERE trading_day = ?", (trading_day.isoformat(),)
        )
        return maybe_dec(row["start_equity"]) if row else None

    def record_equity(self, trading_day: date, equity: Decimal) -> Decimal:
        """Store today's equity, setting the day's baseline on first call.

        The baseline is what the circuit breaker measures against, so it is
        written exactly once per day and never overwritten.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT start_equity FROM daily WHERE trading_day = ?", (trading_day.isoformat(),)
            ).fetchone()
            start = maybe_dec(row["start_equity"]) if row else None
            if start is None:
                start = equity
            self._conn.execute(
                "INSERT INTO daily (trading_day, start_equity, last_equity, updated_at) "
                "VALUES (?,?,?,?) ON CONFLICT(trading_day) DO UPDATE SET "
                "start_equity = COALESCE(daily.start_equity, excluded.start_equity), "
                "last_equity = excluded.last_equity, updated_at = excluded.updated_at",
                (trading_day.isoformat(), str(start), str(equity), utcnow().isoformat()),
            )
            self._conn.commit()
        return start

    def add_realised_pnl(self, trading_day: date, amount: Decimal) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT realised_pnl FROM daily WHERE trading_day = ?", (trading_day.isoformat(),)
            ).fetchone()
            total = (dec(row["realised_pnl"]) if row else ZERO) + amount
            self._conn.execute(
                "INSERT INTO daily (trading_day, realised_pnl, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(trading_day) DO UPDATE SET realised_pnl = excluded.realised_pnl, "
                "updated_at = excluded.updated_at",
                (trading_day.isoformat(), str(total), utcnow().isoformat()),
            )
            self._conn.commit()

    def daily_row(self, trading_day: date) -> sqlite3.Row | None:
        return self._read_one(
            "SELECT * FROM daily WHERE trading_day = ?", (trading_day.isoformat(),)
        )

    # --------------------------------------------------------------- breaker
    def breaker(self) -> sqlite3.Row | None:
        return self._read_one("SELECT * FROM breaker WHERE id = 1")

    def breaker_tripped(self) -> bool:
        return self.breaker() is not None

    def trip_breaker(self, trading_day: date, reason: str, pnl: Decimal) -> None:
        self._write(
            "INSERT INTO breaker (id, tripped_at, trading_day, reason, pnl) VALUES (1,?,?,?,?) "
            "ON CONFLICT(id) DO NOTHING",
            (utcnow().isoformat(), trading_day.isoformat(), reason, str(pnl)),
        )

    def reset_breaker(self) -> bool:
        cursor = self._write("DELETE FROM breaker WHERE id = 1")
        return bool(cursor.rowcount)

    # ------------------------------------------------------------- dashboard
    def recent_decisions(self, limit: int = 10) -> list[dict[str, Any]]:
        """Joined view of the last N cycles, for the dashboard."""
        rows = self._read(
            """
            SELECT c.decision_id, c.started_at, c.status, c.halt_reason, c.mode,
                   a.action AS ai_action, a.ticker AS ai_ticker, a.confidence,
                   a.reasoning, a.provider, a.model, a.error AS ai_error,
                   v.approved, v.rule, v.quantity AS approved_quantity,
                   v.notional AS approved_notional, v.reasons, v.shrunk,
                   o.state AS order_state, o.fill_price, o.fill_quantity, o.error AS order_error
            FROM cycles c
            LEFT JOIN ai_decisions a ON a.decision_id = c.decision_id
            LEFT JOIN risk_verdicts v ON v.decision_id = c.decision_id AND v.stage = 'primary'
            LEFT JOIN orders o ON o.decision_id = c.decision_id
            ORDER BY c.started_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item.get("reasons"):
                try:
                    item["reasons"] = json.loads(item["reasons"])
                except json.JSONDecodeError:  # pragma: no cover - defensive
                    item["reasons"] = [item["reasons"]]
            else:
                item["reasons"] = []
            out.append(item)
        return out


def _order_from_row(row: sqlite3.Row) -> OrderRecord:
    return OrderRecord(
        decision_id=row["decision_id"],
        mode=row["mode"],
        state=row["state"],
        ticker=row["ticker"],
        side=row["side"],
        quantity=dec(row["quantity"]),
        notional=dec(row["notional"]),
        reference_price=maybe_dec(row["reference_price"]),
        broker_order_id=row["broker_order_id"],
        fill_price=maybe_dec(row["fill_price"]),
        fill_quantity=maybe_dec(row["fill_quantity"]),
        error=row["error"],
        submitted_at=datetime.fromisoformat(row["submitted_at"]) if row["submitted_at"] else None,
        settled_at=datetime.fromisoformat(row["settled_at"]) if row["settled_at"] else None,
    )


def positions_from_paper(
    holdings: Mapping[str, tuple[Decimal, Decimal]],
    prices: Mapping[str, Decimal],
) -> tuple[Position, ...]:
    """Mark the virtual ledger to market using the latest quotes."""
    out: list[Position] = []
    for ticker, (quantity, average_price) in sorted(holdings.items()):
        if quantity <= ZERO:
            continue
        out.append(
            Position(
                ticker=ticker,
                quantity=quantity,
                average_price=average_price,
                # With no fresh quote, fall back to cost: it keeps the position
                # in the equity total without inventing a gain or a loss.
                current_price=prices.get(ticker, average_price),
            )
        )
    return tuple(out)


def iter_tickers(items: Iterable[Any]) -> list[str]:
    return [getattr(i, "ticker", str(i)) for i in items]
