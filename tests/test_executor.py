"""Execution: the duplicate guard, the pre-submit re-check, and paper fills."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from t212bot.executor import ExecutionError, Executor
from t212bot.models import Verdict, dec
from t212bot.storage import (
    STATE_ACCEPTED,
    STATE_CANCELLED,
    STATE_FILLED,
    STATE_REJECTED,
    STATE_UNKNOWN,
)
from t212bot.t212_client import T212APIError, T212TransportError

from conftest import TICKER, make_account, make_config, make_position

DAY = date(2026, 3, 2)


def buy_verdict(quantity="1", notional="10", price="10") -> Verdict:
    return Verdict(
        approved=True,
        action="buy",
        rule="OK",
        ticker=TICKER,
        quantity=dec(quantity),
        notional=dec(notional),
        reference_price=dec(price),
    )


def sell_verdict(quantity="-1", notional="10", price="10") -> Verdict:
    return Verdict(
        approved=True,
        action="sell",
        rule="OK",
        ticker=TICKER,
        quantity=dec(quantity),
        notional=dec(notional),
        reference_price=dec(price),
    )


class FakeClient:
    """Records order calls. Any unexpected call is a test failure."""

    def __init__(self, response=None, raises=None):
        self.calls: list[tuple] = []
        self._response = response if response is not None else {"id": 4242, "status": "FILLED"}
        self._raises = raises

    def place_market_order(self, ticker, quantity):
        self.calls.append(("market", ticker, quantity))
        if self._raises:
            raise self._raises
        return self._response

    def place_limit_order(self, ticker, quantity, limit_price, time_validity="DAY"):
        self.calls.append(("limit", ticker, quantity, limit_price))
        if self._raises:
            raise self._raises
        return self._response

    def get_order(self, order_id):
        return {"status": "FILLED", "fillPrice": 10, "filledQuantity": 1}


class ExplodingClient(FakeClient):
    def place_market_order(self, ticker, quantity):  # pragma: no cover - must never run
        raise AssertionError("paper mode must never call an order endpoint")

    place_limit_order = place_market_order


# --------------------------------------------------------------------------- #
# Paper mode
# --------------------------------------------------------------------------- #


def test_paper_fill_updates_the_virtual_ledger(storage):
    config = make_config(mode="paper")
    storage.seed_paper_account(dec(50))
    executor = Executor(config, storage)

    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))

    assert record.state == STATE_FILLED
    assert storage.paper_cash() == dec(40)
    assert storage.paper_positions()[TICKER][0] == dec(1)


def test_paper_mode_never_touches_an_order_endpoint(storage):
    config = make_config(mode="paper")
    storage.seed_paper_account(dec(50))
    executor = Executor(config, storage, ExplodingClient())
    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))
    assert record.state == STATE_FILLED


def test_paper_slippage_works_against_the_trade(storage):
    config = make_config(mode="paper", paper_slippage_bps=100)  # 1%
    storage.seed_paper_account(dec(50))
    executor = Executor(config, storage)
    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))
    assert record.fill_price == dec("10.1")


def test_paper_sell_slippage_works_against_the_trade(storage):
    config = make_config(mode="paper", paper_slippage_bps=100)
    storage.seed_paper_account(dec(50))
    storage.apply_paper_fill(TICKER, dec(1), dec(10), DAY)
    executor = Executor(config, storage)
    account = make_account(cash=40, positions=[make_position(quantity=1)])
    record = executor.execute("d1", sell_verdict(), DAY, lambda: account)
    assert record.fill_price == dec("9.9")


def test_slippage_that_makes_the_order_unaffordable_shrinks_it(storage):
    """The virtual account is never overdrawn, even by a penny of slippage."""
    config = make_config(mode="paper", paper_slippage_bps=100, quantity_decimals=2)
    storage.seed_paper_account(dec(10))
    executor = Executor(config, storage)
    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=10))
    assert record.state == STATE_FILLED
    assert storage.paper_cash() >= Decimal(0)


# --------------------------------------------------------------------------- #
# The duplicate-order guard
# --------------------------------------------------------------------------- #


def test_the_same_decision_id_never_places_two_orders(storage):
    config = make_config(mode="demo")
    client = FakeClient()
    executor = Executor(config, storage, client)

    executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))
    executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))

    assert len(client.calls) == 1


def test_a_replayed_decision_after_a_restart_is_refused(storage):
    """The guard is a database row, so a fresh Executor still sees it."""
    config = make_config(mode="demo")
    client = FakeClient()
    Executor(config, storage, client).execute(
        "d1", buy_verdict(), DAY, lambda: make_account(cash=50)
    )
    Executor(config, storage, FakeClient()).execute(
        "d1", buy_verdict(), DAY, lambda: make_account(cash=50)
    )
    assert len(client.calls) == 1


def test_a_rejected_verdict_is_never_submitted(storage):
    config = make_config(mode="demo")
    client = FakeClient()
    executor = Executor(config, storage, client)
    verdict = Verdict(approved=False, action="hold", rule="R04_HOLD")
    assert executor.execute("d1", verdict, DAY, lambda: make_account()) is None
    assert client.calls == []


# --------------------------------------------------------------------------- #
# Pre-submit re-check
# --------------------------------------------------------------------------- #


def test_the_balance_is_rechecked_immediately_before_submission(storage):
    config = make_config(mode="demo")
    client = FakeClient()
    executor = Executor(config, storage, client)

    # Cash vanished between the risk check and now.
    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=0))

    assert client.calls == []
    assert record.state == STATE_CANCELLED
    assert "R13_NO_CASH" in record.error


def test_the_recheck_shrinks_rather_than_cancelling_when_it_can(storage):
    config = make_config(mode="demo")
    client = FakeClient()
    executor = Executor(config, storage, client)

    executor.execute("d1", buy_verdict(notional="10"), DAY, lambda: make_account(cash=6))

    assert len(client.calls) == 1
    assert client.calls[0][2] == Decimal("0.6")  # 6 GBP at 10 GBP/share


def test_a_failed_balance_check_cancels_rather_than_guessing(storage):
    config = make_config(mode="demo")
    client = FakeClient()
    executor = Executor(config, storage, client)

    def boom():
        raise RuntimeError("broker unreachable")

    record = executor.execute("d1", buy_verdict(), DAY, boom)
    assert client.calls == []
    assert record.state == STATE_CANCELLED


# --------------------------------------------------------------------------- #
# Broker responses
# --------------------------------------------------------------------------- #


def test_a_sell_is_submitted_as_a_negative_quantity(storage):
    config = make_config(mode="demo")
    client = FakeClient()
    executor = Executor(config, storage, client)
    account = make_account(cash=0, positions=[make_position(quantity=1)])

    executor.execute("d1", sell_verdict(), DAY, lambda: account)

    assert client.calls[0][2] == Decimal("-1")


def test_a_transport_failure_leaves_the_order_unknown_and_blocks_trading(storage):
    """A timeout is not a failure — we do not know whether it reached the broker."""
    config = make_config(mode="demo")
    client = FakeClient(raises=T212TransportError("timed out"))
    executor = Executor(config, storage, client)

    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))

    assert record.state == STATE_UNKNOWN
    assert len(storage.unresolved_orders()) == 1


def test_an_unknown_order_is_never_retried(storage):
    config = make_config(mode="demo")
    client = FakeClient(raises=T212TransportError("timed out"))
    executor = Executor(config, storage, client)
    executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))
    executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))
    assert len(client.calls) == 1


def test_a_broker_4xx_is_a_definite_rejection(storage):
    config = make_config(mode="demo")
    client = FakeClient(raises=T212APIError(400, "InsufficientResources", "/equity/orders/market"))
    executor = Executor(config, storage, client)

    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))

    assert record.state == STATE_REJECTED
    assert storage.unresolved_orders() == []  # a refusal does not block trading


def test_an_accepted_but_unfilled_order_is_recorded_as_accepted(storage):
    config = make_config(mode="demo")
    client = FakeClient(response={"id": 99, "status": "SUBMITTED"})
    executor = Executor(config, storage, client)

    record = executor.execute("d1", buy_verdict(), DAY, lambda: make_account(cash=50))

    assert record.state == STATE_ACCEPTED
    assert record.broker_order_id == "99"


def test_reconciliation_promotes_accepted_orders_to_filled(storage):
    config = make_config(mode="demo")
    client = FakeClient(response={"id": 99, "status": "SUBMITTED"})
    executor = Executor(config, storage, client)
    storage.reserve_order("d1", date.today(), "demo", buy_verdict())
    storage.settle_order("d1", STATE_ACCEPTED, broker_order_id="99")

    assert executor.reconcile_open_orders() == 1
    assert storage.get_order("d1").state == STATE_FILLED


def test_limit_orders_are_routed_to_the_limit_endpoint(storage):
    config = make_config(mode="demo", order_type="limit")
    client = FakeClient()
    executor = Executor(config, storage, client)
    verdict = replace(buy_verdict(), limit_price=dec("10.03"))

    executor.execute("d1", verdict, DAY, lambda: make_account(cash=50))

    assert client.calls[0][0] == "limit"
    assert client.calls[0][3] == dec("10.03")


# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_live_mode_requires_a_client(storage):
    with pytest.raises(ExecutionError, match="needs a client"):
        Executor(make_config(mode="live"), storage, None)


def test_paper_mode_needs_no_client(storage):
    assert Executor(make_config(mode="paper"), storage, None) is not None
