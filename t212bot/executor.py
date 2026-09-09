"""Order placement. The only module that talks to an order endpoint.

The sequence is fixed and the order of the steps is the point:

1. **Claim the decision id** in SQLite *before* any network call. If the row
   already exists — a retry, a duplicated scheduler tick, a restart mid-cycle —
   we stop. T212's order endpoints are not idempotent, so the guard has to be
   ahead of the request, not around it.
2. **Re-fetch the balance and re-run the risk manager.** Between the AI call and
   here, a fill or a price move may have changed what is affordable. The
   re-check can only shrink or reject.
3. **Submit, without retries.** A timeout on an order means "unknown", not
   "failed" — the row is marked ``unknown`` and every later cycle refuses to
   trade (rule R02) until a human reconciles it.

In paper mode step 3 simulates a fill against the last quote and touches the
virtual ledger. No order endpoint is called, in any circumstance.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Callable

from .config import AppConfig
from .models import AccountState, OrderRecord, Verdict, ZERO, maybe_dec, money
from .risk_manager import limit_price_for, revalidate
from .storage import (
    STATE_ACCEPTED,
    STATE_CANCELLED,
    STATE_FILLED,
    STATE_REJECTED,
    STATE_UNKNOWN,
    Storage,
)
from .t212_client import (
    T212APIError,
    T212AuthError,
    T212Client,
    T212Error,
    T212TransportError,
)

log = logging.getLogger(__name__)


class ExecutionError(Exception):
    """Execution could not proceed. Not the same as an order being rejected."""


class Executor:
    def __init__(
        self,
        config: AppConfig,
        storage: Storage,
        client: T212Client | None = None,
    ):
        self.config = config
        self.storage = storage
        self.client = client
        if config.places_real_orders and client is None:
            raise ExecutionError(f"MODE={config.mode} places real orders and needs a client")

    # ------------------------------------------------------------------ main
    def execute(
        self,
        decision_id: str,
        verdict: Verdict,
        trading_day: date,
        refresh_account: Callable[[], AccountState],
        preorder: bool = False,
    ) -> OrderRecord | None:
        """Place the approved order, or return None if it never reached the market.

        ``preorder`` says the market is closed and the order should rest until
        it opens: a limit order with the configured time validity, rather than
        a market order the broker would refuse outright. It changes how the
        order is placed, never whether it is allowed — the risk manager has
        already decided that, and the pre-submit re-check below still runs.
        """
        if not verdict.approved or verdict.ticker is None:
            return None

        # 1. Duplicate-order guard, ahead of any network call.
        if not self.storage.reserve_order(decision_id, trading_day, self.config.mode, verdict):
            existing = self.storage.get_order(decision_id)
            log.error(
                "duplicate submission blocked for decision %s (existing state=%s) — not resending",
                decision_id,
                existing.state if existing else "?",
            )
            return existing

        # 2. Fresh balance, then re-run the risk manager against it.
        try:
            account = refresh_account()
        except Exception as exc:  # noqa: BLE001 - cannot verify funds, so do not trade
            self.storage.settle_order(
                decision_id, STATE_CANCELLED, error=f"balance re-check failed: {exc}"
            )
            log.error("balance re-check failed for %s: %s", decision_id, exc)
            return self.storage.get_order(decision_id)

        checked = revalidate(verdict, account, self.config)
        self.storage.record_verdict(decision_id, checked, stage="pre_submit")
        if not checked.approved:
            self.storage.settle_order(
                decision_id,
                STATE_CANCELLED,
                error=f"{checked.rule}: {'; '.join(checked.reasons)}",
            )
            log.warning(
                "order withdrawn at the pre-submit check: %s %s",
                checked.rule,
                "; ".join(checked.reasons),
            )
            return self.storage.get_order(decision_id)

        if checked.shrunk and checked.quantity != verdict.quantity:
            log.info(
                "pre-submit re-check shrank the order to %s %s",
                checked.quantity,
                checked.ticker,
            )

        # 3. Submit.
        self.storage.mark_submitting(decision_id)
        if self.config.mode == "paper":
            return self._simulate(decision_id, checked, trading_day, preorder=preorder)
        return self._submit(decision_id, checked, preorder=preorder)

    # ----------------------------------------------------------------- paper
    def _simulate(
        self,
        decision_id: str,
        verdict: Verdict,
        trading_day: date,
        preorder: bool = False,
    ) -> OrderRecord:
        price = verdict.reference_price
        if price is None or price <= ZERO:  # pragma: no cover - blocked upstream
            raise ExecutionError("cannot simulate a fill without a reference price")

        # Slippage always works against us, so paper results never flatter the
        # strategy relative to what a real fill would have done.
        slip = self.config.execution.paper_slippage_bps / Decimal(10_000)
        fill_price = price * (Decimal(1) + slip) if verdict.quantity > ZERO else price * (
            Decimal(1) - slip
        )

        quantity = verdict.quantity
        if quantity > ZERO:
            # The slipped price may make the approved quantity unaffordable by a
            # penny; shrink rather than overdraw the virtual account.
            spendable = self.storage.paper_cash() - self.config.capital.cash_buffer
            if quantity * fill_price > spendable:
                from .risk_manager import floor_quantity

                quantity = floor_quantity(spendable / fill_price, self.config)
                if quantity <= ZERO:
                    self.storage.settle_order(
                        decision_id,
                        STATE_CANCELLED,
                        error="simulated slippage left nothing affordable",
                    )
                    return self.storage.get_order(decision_id)

        realised = self.storage.apply_paper_fill(
            verdict.ticker, quantity, fill_price, trading_day
        )
        self.storage.settle_order(
            decision_id,
            STATE_FILLED,
            broker_order_id=f"paper-{decision_id[:8]}",
            fill_price=fill_price,
            fill_quantity=quantity,
            raw_response={
                "simulated": True,
                "realised_pnl": str(realised),
                # The paper ledger has no concept of a resting order, so this
                # fills straight away. Recording the flag keeps the audit log
                # honest about what a live run would have done instead.
                **({"preorder": True} if preorder else {}),
            },
        )
        log.info(
            "PAPER fill: %s %s at %s (%s), realised %s",
            "BUY" if quantity > ZERO else "SELL",
            verdict.ticker,
            money(fill_price),
            money(abs(quantity) * fill_price),
            money(realised),
        )
        return self.storage.get_order(decision_id)

    # ------------------------------------------------------------ demo / live
    def _submit(
        self, decision_id: str, verdict: Verdict, preorder: bool = False
    ) -> OrderRecord:
        assert self.client is not None  # guaranteed by __init__
        ticker = verdict.ticker
        quantity = verdict.quantity  # already signed: negative sells
        limit_price = self._preorder_limit_price(verdict) if preorder else None

        log.warning(
            "%s %s: %s %s %s (~%s) decision=%s%s",
            self.config.mode.upper(),
            "PRE-ORDER" if limit_price else "ORDER",
            "BUY" if quantity > ZERO else "SELL",
            abs(quantity),
            ticker,
            money(verdict.notional),
            decision_id,
            f" limit {money(limit_price)} {self.config.execution.preorder_time_validity}"
            if limit_price
            else "",
        )

        try:
            if limit_price is not None:
                # Market closed: rest a limit order until it opens. The limit is
                # the point of it — a market order into an opening gap has no
                # ceiling, this one does.
                response = self.client.place_limit_order(
                    ticker,
                    quantity,
                    limit_price,
                    time_validity=self.config.execution.preorder_time_validity,
                )
            elif self.config.execution.order_type == "limit" and verdict.limit_price:
                response = self.client.place_limit_order(ticker, quantity, verdict.limit_price)
            else:
                response = self.client.place_market_order(ticker, quantity)
        except T212TransportError as exc:
            # The request may or may not have reached the broker. Never resend.
            self.storage.settle_order(
                decision_id, STATE_UNKNOWN, error=f"transport failure, outcome unknown: {exc}"
            )
            log.critical(
                "ORDER OUTCOME UNKNOWN for %s (%s). Trading is now blocked. "
                "Check the Trading212 app, then run --resolve-order %s",
                decision_id,
                exc,
                decision_id,
            )
            return self.storage.get_order(decision_id)
        except T212APIError as exc:
            # A 4xx is a definite refusal: the broker did not accept the order.
            self.storage.settle_order(
                decision_id, STATE_REJECTED, error=str(exc), raw_response={"body": exc.body[:2000]}
            )
            log.error("broker rejected order %s: %s", decision_id, exc)
            return self.storage.get_order(decision_id)
        except T212AuthError as exc:
            # 401/403 is refused before an order can exist, so this is a
            # rejection, not an unknown outcome. Treating it as unknown would
            # block all trading over what is usually a credentials or
            # wrong-environment mistake.
            self.storage.settle_order(decision_id, STATE_REJECTED, error=str(exc))
            log.error(
                "broker refused order %s without creating it: %s", decision_id, exc
            )
            return self.storage.get_order(decision_id)
        except T212Error as exc:
            self.storage.settle_order(decision_id, STATE_UNKNOWN, error=str(exc))
            log.critical("ORDER OUTCOME UNKNOWN for %s: %s", decision_id, exc)
            return self.storage.get_order(decision_id)

        payload = response if isinstance(response, dict) else {"response": response}
        order_id = payload.get("id") or payload.get("orderId")
        status = str(payload.get("status", "")).upper()
        filled_quantity = maybe_dec(payload.get("filledQuantity"))
        fill_price = maybe_dec(payload.get("fillPrice")) or maybe_dec(payload.get("filledValue"))

        state = STATE_FILLED if status in ("FILLED", "COMPLETED") else STATE_ACCEPTED
        self.storage.settle_order(
            decision_id,
            state,
            broker_order_id=str(order_id) if order_id is not None else None,
            fill_price=fill_price,
            fill_quantity=filled_quantity,
            raw_response=payload,
        )
        log.warning("order %s accepted by broker as %s (status=%s)", decision_id, order_id, status)
        return self.storage.get_order(decision_id)

    def _preorder_limit_price(self, verdict: Verdict) -> Decimal | None:
        """The price to rest a closed-market order at, or None if unknowable."""
        if verdict.limit_price is not None:
            return verdict.limit_price
        price = verdict.reference_price
        if price is None or price <= ZERO:
            return None
        side = "buy" if verdict.quantity > ZERO else "sell"
        return limit_price_for(side, price, self.config)

    # ----------------------------------------------------------- reconciling
    def reconcile_open_orders(self) -> int:
        """Chase up orders the broker accepted but had not filled yet.

        Read-only: it asks about existing orders and never creates one.
        """
        if self.client is None or self.config.mode == "paper":
            return 0

        updated = 0
        for record in self.storage.orders_today(date.today()):
            if record.state != STATE_ACCEPTED or not record.broker_order_id:
                continue
            try:
                payload = self.client.get_order(record.broker_order_id)
            except T212APIError as exc:
                if exc.status_code == 404:
                    # Working orders drop out of /equity/orders once they fill.
                    self.storage.settle_order(
                        record.decision_id,
                        STATE_FILLED,
                        broker_order_id=record.broker_order_id,
                        error="not in working orders; assumed filled — verify in history",
                    )
                    updated += 1
                continue
            except T212Error as exc:
                log.warning("could not reconcile order %s: %s", record.decision_id, exc)
                continue

            status = str((payload or {}).get("status", "")).upper()
            if status in ("FILLED", "COMPLETED"):
                self.storage.settle_order(
                    record.decision_id,
                    STATE_FILLED,
                    broker_order_id=record.broker_order_id,
                    fill_price=maybe_dec((payload or {}).get("fillPrice")),
                    fill_quantity=maybe_dec((payload or {}).get("filledQuantity")),
                    raw_response=payload,
                )
                updated += 1
            elif status in ("REJECTED", "CANCELLED", "CANCELED"):
                self.storage.settle_order(
                    record.decision_id, STATE_REJECTED, raw_response=payload
                )
                updated += 1
        return updated
