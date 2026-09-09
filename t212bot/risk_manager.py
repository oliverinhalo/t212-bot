"""The risk manager. The only module allowed to authorise an order.

Everything here is a pure function: no I/O, no clock reads (the clock is passed
in via ``RiskInputs.now``), no network, no database. That is what makes it
exhaustively testable, and the tests in tests/test_risk_manager.py are the real
specification of this project's safety behaviour.

Three outcomes only, per the project rules: **approve**, **shrink**, or
**reject**. There is no code path that produces an order larger than the one the
AI proposed — ``_assert_not_enlarged`` enforces that as a last line of defence
and raises rather than returning a bad verdict.

Rule codes (also written to the audit log, so keep them stable):

  R01_BREAKER          daily loss circuit breaker is tripped
  R02_UNRESOLVED       a previous order is in an unknown state; refuse to trade
  R03_DUPLICATE        this decision id already produced an order
  R04_HOLD             the AI said hold, or proposed nothing actionable
  R05_ALLOWLIST        ticker is not on the configured allow-list
  R06_QUOTE_MISSING    no quote for the ticker
  R07_QUOTE_STALE      quote fetched longer ago than risk.max_quote_age_seconds
  R08_QUOTE_INVALID    quote price is zero or negative
  R09_PRICE_DEVIATION  AI's implied price is far from the last quote
  R10_FREQUENCY        already hit risk.max_trades_per_day
  R11_CONFIDENCE       below risk.min_confidence
  R12_NO_SIZE          buy with neither notional nor quantity
  R13_NO_CASH          no spendable cash after the buffer
  R14_CAPITAL_CAP      capital.max_capital_gbp already fully deployed
  R15_POSITION_CAP     this ticker is already at its position cap
  R16_BELOW_MIN        approvable size is under capital.min_order_gbp
  R17_QUANTITY_ZERO    size rounds down to zero shares
  R18_NO_POSITION      sell with nothing held (this is also the no-shorting rule)
  R19_QUOTE_DELAYED    feed's own delay exceeds risk.max_quote_delay_seconds
  OK                   approved
"""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_FLOOR, Decimal

from .config import AppConfig
from .models import (
    ZERO,
    AccountState,
    Proposal,
    Quote,
    RiskInputs,
    Verdict,
    money,
    reject,
)

__all__ = [
    "evaluate",
    "limit_price_for",
    "revalidate",
    "circuit_breaker_state",
    "daily_pnl",
    "floor_quantity",
]


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


def daily_pnl(day_start_equity: Decimal, current_equity: Decimal) -> Decimal:
    """Total P&L for the day: realised and unrealised together.

    Both are captured because ``equity`` is cash plus marked-to-market
    positions — selling at a loss moves value from positions to cash without
    changing the total, so a realised loss shows up here just as an unrealised
    one does.
    """
    return current_equity - day_start_equity


def circuit_breaker_state(
    day_start_equity: Decimal,
    current_equity: Decimal,
    config: AppConfig,
) -> tuple[bool, Decimal, Decimal]:
    """Return ``(should_trip, pnl, limit)``.

    ``limit`` is negative. The breaker trips when P&L is at or below it.
    A day_start_equity of zero means we have no baseline yet, so it cannot trip.
    """
    limit = config.risk.daily_loss_limit(config.capital.max_capital)
    if day_start_equity <= ZERO:
        return False, ZERO, limit
    pnl = daily_pnl(day_start_equity, current_equity)
    return pnl <= limit, pnl, limit


# --------------------------------------------------------------------------- #
# Sizing helpers
# --------------------------------------------------------------------------- #


def floor_quantity(quantity: Decimal, config: AppConfig) -> Decimal:
    """Round a share quantity DOWN to the configured precision.

    Always downward, so rounding can only ever shrink an order — never push it
    past a cap.
    """
    if quantity <= ZERO:
        return ZERO
    if not config.execution.fractional:
        return quantity.to_integral_value(rounding=ROUND_FLOOR)
    step = Decimal(1).scaleb(-config.execution.quantity_decimals)
    return quantity.quantize(step, rounding=ROUND_FLOOR)


def limit_price_for(side: str, price: Decimal, config: AppConfig) -> Decimal:
    """A limit price offset in the direction that helps the order fill.

    Above the market for a buy, below it for a sell, by
    ``execution.limit_offset_bps``. Always computed, whatever the configured
    order type: an out-of-hours pre-order needs one even when the bot is
    otherwise placing market orders.
    """
    offset = price * config.execution.limit_offset_bps / Decimal(10_000)
    raw = price + offset if side == "buy" else price - offset
    return money(max(raw, Decimal("0.01")))


def _limit_price(side: str, price: Decimal, config: AppConfig) -> Decimal | None:
    """The verdict's limit price: set only when limit orders are configured."""
    if config.execution.order_type != "limit":
        return None
    return limit_price_for(side, price, config)


def _assert_not_enlarged(proposal: Proposal, verdict: Verdict, price: Decimal) -> Verdict:
    """Last line of defence: a verdict may never exceed what was proposed.

    Raises rather than returning, because reaching this state means the sizing
    logic above has a bug and continuing would place an unauthorised order.
    """
    if not verdict.approved:
        return verdict

    approved_notional = abs(verdict.quantity) * price
    proposed_notional: Decimal | None = None
    if proposal.notional is not None:
        proposed_notional = abs(proposal.notional)
    elif proposal.quantity is not None:
        proposed_notional = abs(proposal.quantity) * price

    # A sell with no size given is read as "close the position", which is
    # risk-reducing, so there is no proposed notional to compare against.
    if proposed_notional is None:
        return verdict

    tolerance = Decimal("0.0001")
    if approved_notional > proposed_notional + tolerance:
        raise AssertionError(
            "risk manager produced an order larger than proposed "
            f"({approved_notional} > {proposed_notional}) — refusing to continue"
        )
    return verdict


# --------------------------------------------------------------------------- #
# Gates that apply to any trade
# --------------------------------------------------------------------------- #


def _check_quote(ticker: str, inputs: RiskInputs, config: AppConfig) -> Verdict | Quote:
    quote = inputs.quotes.get(ticker)
    if quote is None:
        return reject("R06_QUOTE_MISSING", f"no quote available for {ticker}", ticker=ticker)
    if quote.price <= ZERO:
        return reject(
            "R08_QUOTE_INVALID", f"quote price for {ticker} is {quote.price}", ticker=ticker
        )
    # Two different things can be wrong with a quote's timing, and conflating
    # them is what used to block every trade on a delayed feed:
    #
    #   staleness — how long ago *we* fetched it. This is the one that matters:
    #               it says our own market data has stopped updating.
    #   delay     — how far behind the exchange timestamp is. On a free feed
    #               this is a constant ~15 minutes and says nothing about
    #               whether our data is current, so it is gated separately and
    #               is off by default.
    #
    # Either limit is disabled by setting it to 0 or less.
    max_age = config.risk.max_quote_age_seconds
    if max_age > 0:
        staleness = quote.staleness_seconds(inputs.now)
        if staleness > max_age:
            return reject(
                "R07_QUOTE_STALE",
                f"quote for {ticker} was fetched {staleness:.0f}s ago, limit is {max_age}s",
                ticker=ticker,
            )

    max_delay = config.risk.max_quote_delay_seconds
    if max_delay > 0:
        delay = quote.age_seconds(inputs.now)
        if delay > max_delay:
            return reject(
                "R19_QUOTE_DELAYED",
                f"quote for {ticker} is timestamped {delay:.0f}s behind the market, "
                f"limit is {max_delay}s",
                ticker=ticker,
            )
    return quote


def _check_price_sanity(
    proposal: Proposal, quote: Quote, config: AppConfig
) -> Verdict | None:
    """Reject a proposal whose implied price is nowhere near the market."""
    if proposal.price is None or proposal.price <= ZERO:
        return None
    deviation = abs(proposal.price - quote.price) / quote.price * Decimal(100)
    if deviation > config.risk.max_price_deviation_pct:
        return reject(
            "R09_PRICE_DEVIATION",
            f"AI implied price {proposal.price} is {deviation:.2f}% from the quote "
            f"{quote.price}, limit is {config.risk.max_price_deviation_pct}%",
            ticker=proposal.ticker,
        )
    return None


# --------------------------------------------------------------------------- #
# Sizing
# --------------------------------------------------------------------------- #


def _size_buy(
    proposal: Proposal,
    account: AccountState,
    quote: Quote,
    config: AppConfig,
) -> Verdict:
    ticker = quote.ticker
    price = quote.price
    reasons: list[str] = []

    if proposal.notional is not None:
        desired = proposal.notional
    elif proposal.quantity is not None:
        desired = proposal.quantity * price
    else:
        return reject(
            "R12_NO_SIZE", "buy proposal gave neither notional nor quantity", ticker=ticker
        )

    if desired <= ZERO:
        return reject("R12_NO_SIZE", f"buy size must be positive, got {desired}", ticker=ticker)

    # Each cap is computed independently; the smallest one binds. Naming the
    # binding cap in the audit log is what makes a shrink explainable later.
    caps: list[tuple[str, Decimal, str]] = [
        (
            "R16_BELOW_MIN",
            config.capital.per_trade_cap,
            f"per-trade cap {money(config.capital.per_trade_cap)} "
            f"({config.capital.per_trade_cap_pct}% of {money(config.capital.max_capital)})",
        ),
        (
            "R13_NO_CASH",
            account.cash - config.capital.cash_buffer,
            f"spendable cash {money(account.cash - config.capital.cash_buffer)} "
            f"(cash {money(account.cash)} less {money(config.capital.cash_buffer)} buffer)",
        ),
        (
            "R14_CAPITAL_CAP",
            config.capital.max_capital - account.invested,
            f"remaining capital allowance {money(config.capital.max_capital - account.invested)} "
            f"(cap {money(config.capital.max_capital)}, deployed {money(account.invested)})",
        ),
        (
            "R15_POSITION_CAP",
            config.position_cap_for(ticker) - account.value_of(ticker),
            f"remaining room in {ticker} "
            f"{money(config.position_cap_for(ticker) - account.value_of(ticker))} "
            f"(cap {money(config.position_cap_for(ticker))}, "
            f"held {money(account.value_of(ticker))})",
        ),
    ]

    allowed = desired
    binding_rule = "OK"
    binding_reason = ""
    for rule, cap, description in caps:
        if cap < allowed:
            allowed = cap
            binding_rule = rule
            binding_reason = description

    if allowed <= ZERO:
        return reject(
            binding_rule if binding_rule != "OK" else "R13_NO_CASH",
            f"no room to buy {ticker}: {binding_reason or 'nothing spendable'}",
            ticker=ticker,
        )

    if allowed < desired:
        reasons.append(
            f"shrunk from {money(desired)} to {money(allowed)}, limited by {binding_reason}"
        )

    if allowed < config.capital.min_order:
        return reject(
            "R16_BELOW_MIN",
            f"approvable size {money(allowed)} is below the "
            f"{money(config.capital.min_order)} minimum order"
            + (f" (limited by {binding_reason})" if binding_reason else ""),
            ticker=ticker,
        )

    quantity = floor_quantity(allowed / price, config)
    if quantity <= ZERO:
        return reject(
            "R17_QUANTITY_ZERO",
            f"{money(allowed)} buys 0 shares of {ticker} at {price} "
            f"with {config.execution.quantity_decimals} dp precision"
            + ("" if config.execution.fractional else " (whole shares only)"),
            ticker=ticker,
        )

    notional = quantity * price
    if notional < config.capital.min_order:
        return reject(
            "R16_BELOW_MIN",
            f"rounded order {money(notional)} ({quantity} x {price}) is below the "
            f"{money(config.capital.min_order)} minimum order",
            ticker=ticker,
        )

    reasons.append(
        f"buy {quantity} {ticker} at ~{price} = {money(notional)}; "
        f"after fill deployed {money(account.invested + notional)} of "
        f"{money(config.capital.max_capital)}"
    )
    return Verdict(
        approved=True,
        action="buy",
        rule="OK",
        reasons=tuple(reasons),
        ticker=ticker,
        quantity=quantity,
        notional=notional,
        reference_price=price,
        limit_price=_limit_price("buy", price, config),
        shrunk=allowed < desired,
    )


def _size_sell(
    proposal: Proposal,
    account: AccountState,
    quote: Quote,
    config: AppConfig,
) -> Verdict:
    ticker = quote.ticker
    price = quote.price
    reasons: list[str] = []

    held = account.quantity_of(ticker)
    if held <= ZERO:
        # This is also the no-shorting rule: you cannot sell what you do not own.
        return reject(
            "R18_NO_POSITION",
            f"no position in {ticker} to sell (shorting is not permitted)",
            ticker=ticker,
        )

    if proposal.quantity is not None:
        desired = abs(proposal.quantity)
    elif proposal.notional is not None:
        desired = abs(proposal.notional) / price
    else:
        desired = held
        reasons.append("no size given on the sell; read as a full exit")

    if desired <= ZERO:
        return reject("R12_NO_SIZE", f"sell size must be positive, got {desired}", ticker=ticker)

    quantity = min(desired, held)
    full_exit = quantity >= held
    if full_exit:
        # Sell the exact holding, unrounded, so no dust is left behind.
        quantity = held
        if desired > held:
            reasons.append(f"shrunk from {desired} to the {held} actually held")
    else:
        quantity = floor_quantity(quantity, config)

    if quantity <= ZERO:
        return reject(
            "R17_QUANTITY_ZERO",
            f"sell size rounds down to zero shares of {ticker}",
            ticker=ticker,
        )

    notional = quantity * price
    # A full exit is always allowed through, even if the holding is worth less
    # than min_order — otherwise dust positions could never be closed.
    if notional < config.capital.min_order and not full_exit:
        return reject(
            "R16_BELOW_MIN",
            f"partial sell of {money(notional)} is below the "
            f"{money(config.capital.min_order)} minimum order",
            ticker=ticker,
        )

    reasons.append(
        f"sell {quantity} {ticker} at ~{price} = {money(notional)}"
        + (" (full exit)" if full_exit else f"; {held - quantity} left")
    )
    return Verdict(
        approved=True,
        action="sell",
        rule="OK",
        reasons=tuple(reasons),
        ticker=ticker,
        quantity=-quantity,  # T212 convention: sells are negative quantities
        notional=notional,
        reference_price=price,
        limit_price=_limit_price("sell", price, config),
        shrunk=full_exit and desired > held,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def evaluate(proposal: Proposal, inputs: RiskInputs, config: AppConfig) -> Verdict:
    """Decide whether the AI's proposal may become a real order.

    Gates run cheapest-and-most-absolute first, so an audit log entry names the
    single most important reason a trade did not happen.
    """
    # 1. Circuit breaker. Blocks sells too: an unattended bot that has already
    #    lost its daily limit should stop acting, not start improvising. Close
    #    positions by hand in the app if you need to.
    if inputs.breaker_tripped:
        return reject(
            "R01_BREAKER",
            "daily loss circuit breaker is tripped; restart manually with --reset-breaker",
            ticker=proposal.ticker,
        )

    # 2. A previous order whose outcome we never confirmed. Trading again risks
    #    compounding an unknown position, and T212's endpoints are not
    #    idempotent, so we stop until a human reconciles it.
    if inputs.unresolved_orders:
        return reject(
            "R02_UNRESOLVED",
            f"{inputs.unresolved_orders} order(s) in an unknown state; "
            "reconcile with --list-unresolved before trading again",
            ticker=proposal.ticker,
        )

    # 3. Duplicate-order guard. One decision id may produce at most one order,
    #    ever — including across retries and process restarts.
    if inputs.decision_id in inputs.known_decision_ids:
        return reject(
            "R03_DUPLICATE",
            f"decision {inputs.decision_id} has already produced an order",
            ticker=proposal.ticker,
        )

    # 4. Nothing to do.
    if proposal.action == "hold":
        return reject("R04_HOLD", proposal.reasoning or "AI recommended hold")
    if proposal.action not in ("buy", "sell"):
        return reject("R04_HOLD", f"unrecognised action {proposal.action!r}")

    # 5. Allow-list. The AI cannot invent instruments. In open-universe mode
    #    inputs.allowed_tickers is the set main.py managed to price this cycle
    #    (an unresolvable or hallucinated ticker never makes it in); otherwise
    #    it is the configured watch-list.
    ticker = (proposal.ticker or "").strip()
    if not ticker:
        return reject("R05_ALLOWLIST", "proposal named no ticker")
    allowed = (
        inputs.allowed_tickers
        if inputs.allowed_tickers is not None
        else config.allowed_tickers
    )
    if ticker not in allowed:
        return reject(
            "R05_ALLOWLIST",
            f"{ticker} is not tradable this cycle "
            f"({', '.join(sorted(allowed)) or 'nothing priced'})",
            ticker=ticker,
        )

    # 6. Quote must exist, be positive, and be fresh.
    quote_or_reject = _check_quote(ticker, inputs, config)
    if isinstance(quote_or_reject, Verdict):
        return quote_or_reject
    quote = quote_or_reject

    # 7. Confidence.
    if proposal.confidence < config.risk.min_confidence:
        return reject(
            "R11_CONFIDENCE",
            f"confidence {proposal.confidence} is below the "
            f"{config.risk.min_confidence} minimum",
            ticker=ticker,
        )

    # 8. Implied price must be close to the market.
    deviation_reject = _check_price_sanity(proposal, quote, config)
    if deviation_reject is not None:
        return deviation_reject

    # 9. Frequency cap. This is meant to be low-frequency.
    if inputs.trades_today >= config.risk.max_trades_per_day:
        return reject(
            "R10_FREQUENCY",
            f"already placed {inputs.trades_today} trades today, limit is "
            f"{config.risk.max_trades_per_day}",
            ticker=ticker,
        )

    # 10. Size it, subject to every capital cap.
    if proposal.action == "buy":
        verdict = _size_buy(proposal, inputs.account, quote, config)
    else:
        verdict = _size_sell(proposal, inputs.account, quote, config)

    return _assert_not_enlarged(proposal, verdict, quote.price)


def revalidate(verdict: Verdict, account: AccountState, config: AppConfig) -> Verdict:
    """Re-check an approved verdict against fresh account data, just before submit.

    Called by executor.py after the account balance has been re-fetched. Like
    ``evaluate``, it may only approve, shrink or reject — the returned quantity
    is never larger than the one it was given.
    """
    if not verdict.approved or verdict.ticker is None:
        return verdict

    price = verdict.reference_price
    if price is None or price <= ZERO:
        return reject("R08_QUOTE_INVALID", "no reference price at submission time",
                      ticker=verdict.ticker)

    if verdict.action == "buy":
        spendable = min(
            account.cash - config.capital.cash_buffer,
            config.capital.max_capital - account.invested,
            config.position_cap_for(verdict.ticker) - account.value_of(verdict.ticker),
        )
        if spendable <= ZERO:
            return reject(
                "R13_NO_CASH",
                f"balance re-check at submission: nothing spendable "
                f"(cash {money(account.cash)}, deployed {money(account.invested)})",
                ticker=verdict.ticker,
            )
        if verdict.notional <= spendable:
            return verdict

        quantity = floor_quantity(spendable / price, config)
        notional = quantity * price
        if quantity <= ZERO or notional < config.capital.min_order:
            return reject(
                "R16_BELOW_MIN",
                f"balance re-check at submission: only {money(spendable)} available, "
                f"below the {money(config.capital.min_order)} minimum",
                ticker=verdict.ticker,
            )
        return replace(
            verdict,
            quantity=quantity,
            notional=notional,
            shrunk=True,
            reasons=verdict.reasons
            + (
                f"balance re-check at submission shrank the order to {money(notional)} "
                f"({money(spendable)} available)",
            ),
        )

    held = account.quantity_of(verdict.ticker)
    if held <= ZERO:
        return reject(
            "R18_NO_POSITION",
            f"balance re-check at submission: no {verdict.ticker} held any more",
            ticker=verdict.ticker,
        )
    if abs(verdict.quantity) <= held:
        return verdict
    return replace(
        verdict,
        quantity=-held,
        notional=held * price,
        shrunk=True,
        reasons=verdict.reasons
        + (f"balance re-check at submission shrank the sell to the {held} actually held",),
    )
