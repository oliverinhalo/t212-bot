"""The specification of this project's safety behaviour.

If a rule in the README is not asserted here, treat it as unimplemented.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from t212bot.models import Proposal, dec
from t212bot.risk_manager import (
    circuit_breaker_state,
    evaluate,
    floor_quantity,
    revalidate,
)

from conftest import (
    OTHER,
    TICKER,
    buy,
    make_account,
    make_config,
    make_inputs,
    make_position,
    make_quote,
    sell,
)


# --------------------------------------------------------------------------- #
# Gates that stop everything
# --------------------------------------------------------------------------- #


def test_tripped_breaker_blocks_buys(config):
    verdict = evaluate(buy(), make_inputs(breaker_tripped=True), config)
    assert not verdict.approved
    assert verdict.rule == "R01_BREAKER"


def test_tripped_breaker_blocks_sells_too(config):
    """An unattended bot at its loss limit stops acting entirely."""
    account = make_account(cash=0, positions=[make_position(quantity=2)])
    inputs = make_inputs(account=account, breaker_tripped=True)
    verdict = evaluate(sell(quantity=2), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R01_BREAKER"


def test_unresolved_order_blocks_trading(config):
    verdict = evaluate(buy(), make_inputs(unresolved_orders=1), config)
    assert not verdict.approved
    assert verdict.rule == "R02_UNRESOLVED"


def test_duplicate_decision_id_is_refused(config):
    inputs = make_inputs(decision_id="dup", known_decision_ids=["dup"])
    verdict = evaluate(buy(), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R03_DUPLICATE"


def test_fresh_decision_id_is_not_a_duplicate(config):
    inputs = make_inputs(decision_id="new", known_decision_ids=["old"])
    assert evaluate(buy(), inputs, config).approved


# --------------------------------------------------------------------------- #
# Action and allow-list
# --------------------------------------------------------------------------- #


def test_hold_produces_no_order(config):
    verdict = evaluate(Proposal(action="hold", reasoning="nothing to do"), make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R04_HOLD"


def test_unrecognised_action_is_treated_as_hold(config):
    proposal = Proposal(action="short", ticker=TICKER, confidence=dec(1))  # type: ignore[arg-type]
    verdict = evaluate(proposal, make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R04_HOLD"


def test_ticker_off_the_allow_list_is_rejected(config):
    """The AI cannot invent an instrument, however confident it is."""
    inputs = make_inputs(quotes={"TSLA_US_EQ": make_quote("TSLA_US_EQ", 200)})
    verdict = evaluate(buy(ticker="TSLA_US_EQ"), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R05_ALLOWLIST"


def test_missing_ticker_is_rejected(config):
    verdict = evaluate(buy(ticker=None), make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R05_ALLOWLIST"


def test_open_universe_gate_uses_the_priced_ticker_set(config):
    """With enforce_allowlist off, main.py passes the set of tickers it priced.

    A ticker in that set (it resolved and got a quote) is allowed even though it
    is not on the watch-list; one that is not in the set is still rejected.
    """
    quote = make_quote("NVDA_US_EQ", 100)
    ok = evaluate(
        buy(ticker="NVDA_US_EQ", notional=10),
        make_inputs(quotes={"NVDA_US_EQ": quote}, allowed_tickers=["NVDA_US_EQ"]),
        config,
    )
    assert ok.approved

    blocked = evaluate(
        buy(ticker="NVDA_US_EQ", notional=10),
        make_inputs(quotes={"NVDA_US_EQ": quote}, allowed_tickers=[TICKER]),
        config,
    )
    assert blocked.rule == "R05_ALLOWLIST"


# --------------------------------------------------------------------------- #
# Quotes
# --------------------------------------------------------------------------- #


def test_missing_quote_blocks_the_trade(config):
    verdict = evaluate(buy(ticker=OTHER), make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R06_QUOTE_MISSING"


def test_stale_quote_blocks_the_trade(config):
    inputs = make_inputs(quotes={TICKER: make_quote(age_seconds=1000)})
    verdict = evaluate(buy(), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R07_QUOTE_STALE"


def test_zero_price_quote_blocks_the_trade(config):
    inputs = make_inputs(quotes={TICKER: make_quote(price=0)})
    verdict = evaluate(buy(), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R08_QUOTE_INVALID"


def test_implied_price_far_from_the_quote_is_rejected(config):
    inputs = make_inputs(quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(price=dec(20)), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R09_PRICE_DEVIATION"


def test_implied_price_close_to_the_quote_is_fine(config):
    inputs = make_inputs(quotes={TICKER: make_quote(price=10)})
    assert evaluate(buy(price=dec("10.1")), inputs, config).approved


def test_no_implied_price_skips_the_deviation_check(config):
    assert evaluate(buy(), make_inputs(), config).approved


# --------------------------------------------------------------------------- #
# Frequency and confidence
# --------------------------------------------------------------------------- #


def test_frequency_cap_blocks_the_sixth_trade(config):
    verdict = evaluate(buy(), make_inputs(trades_today=5), config)
    assert not verdict.approved
    assert verdict.rule == "R10_FREQUENCY"


def test_fifth_trade_of_the_day_is_still_allowed(config):
    assert evaluate(buy(), make_inputs(trades_today=4), config).approved


def test_low_confidence_is_rejected(config):
    verdict = evaluate(buy(confidence="0.4"), make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R11_CONFIDENCE"


def test_confidence_exactly_at_the_threshold_is_allowed(config):
    assert evaluate(buy(confidence="0.6"), make_inputs(), config).approved


# --------------------------------------------------------------------------- #
# Buy sizing
# --------------------------------------------------------------------------- #


def test_buy_within_every_cap_is_approved_unchanged(config):
    inputs = make_inputs(
        account=make_account(cash=50), quotes={TICKER: make_quote(price=10)}
    )
    verdict = evaluate(buy(notional=10), inputs, config)
    assert verdict.approved
    assert verdict.rule == "OK"
    assert verdict.quantity == dec(1)
    assert verdict.notional == dec(10)
    assert not verdict.shrunk
    assert verdict.side == "buy"


def test_buy_is_shrunk_to_the_per_trade_cap(config):
    """25% of £50 is £12.50, whatever the AI asks for."""
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=40), inputs, config)
    assert verdict.approved
    assert verdict.shrunk
    assert verdict.notional == dec("12.5")
    assert verdict.quantity == dec("1.25")


def test_buy_is_shrunk_to_available_cash(config):
    inputs = make_inputs(account=make_account(cash=5), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=12), inputs, config)
    assert verdict.approved
    assert verdict.shrunk
    assert verdict.notional == dec(5)


def test_cash_buffer_is_never_spent():
    config = make_config(cash_buffer=2)
    inputs = make_inputs(account=make_account(cash=5), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=12), inputs, config)
    assert verdict.approved
    assert verdict.notional == dec(3)


def test_zero_cash_rejects_the_buy(config):
    inputs = make_inputs(account=make_account(cash=0), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R13_NO_CASH"


def test_buy_cannot_push_deployed_capital_over_the_cap():
    """Deployed value after the fill must stay under MAX_CAPITAL."""
    config = make_config(max_capital=50, per_trade_cap_pct=100, max_position_pct=100)
    account = make_account(cash=100, positions=[make_position(quantity=4.5, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=30), inputs, config)
    assert verdict.approved
    assert verdict.notional == dec(5)  # 45 already deployed, 5 of headroom left
    assert account.invested + verdict.notional <= config.capital.max_capital


def test_fully_deployed_capital_rejects_further_buys():
    config = make_config(max_capital=50, per_trade_cap_pct=100, max_position_pct=100)
    account = make_account(cash=100, positions=[make_position(quantity=5, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=10), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R14_CAPITAL_CAP"


def test_position_cap_limits_topping_up_a_holding():
    """Per-position cap is £12.50; £10 is already held, so only £2.50 more."""
    config = make_config(per_trade_cap_pct=100)
    account = make_account(cash=50, positions=[make_position(quantity=1, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=20), inputs, config)
    assert verdict.approved
    assert verdict.notional == dec("2.5")
    assert verdict.shrunk


def test_position_cap_at_its_limit_rejects_the_buy():
    config = make_config(per_trade_cap_pct=100)
    account = make_account(cash=50, positions=[make_position(quantity="1.25", current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=20), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R15_POSITION_CAP"


def test_per_ticker_override_beats_the_global_position_cap():
    from t212bot.models import WatchItem

    config = make_config(
        per_trade_cap_pct=100,
        watchlist=[WatchItem(ticker=TICKER, yahoo="VUSA.L", name="x", max_position_pct=dec(10))],
    )
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=50), inputs, config)
    assert verdict.approved
    assert verdict.notional == dec(5)  # 10% of £50


def test_buy_below_the_minimum_order_is_rejected_not_shrunk():
    config = make_config(min_order=5)
    inputs = make_inputs(account=make_account(cash="2.50"), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=10), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R16_BELOW_MIN"


def test_buy_that_rounds_to_zero_shares_is_rejected():
    """Whole-share mode with a share price above the per-trade cap."""
    config = make_config(fractional=False, min_order="0.01")
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=100)})
    verdict = evaluate(buy(notional=10), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R17_QUANTITY_ZERO"


def test_whole_share_mode_rounds_down():
    config = make_config(fractional=False, per_trade_cap_pct=100, max_position_pct=100)
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=7)})
    verdict = evaluate(buy(notional=50), inputs, config)
    assert verdict.approved
    assert verdict.quantity == dec(7)
    assert verdict.notional == dec(49)


def test_buy_with_no_size_is_rejected(config):
    verdict = evaluate(buy(notional=None), make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R12_NO_SIZE"


def test_buy_sized_in_shares_is_converted_to_notional(config):
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    proposal = Proposal(action="buy", ticker=TICKER, quantity=dec("0.5"), confidence=dec(1))
    verdict = evaluate(proposal, inputs, config)
    assert verdict.approved
    assert verdict.quantity == dec("0.5")
    assert verdict.notional == dec(5)


def test_negative_buy_size_is_rejected(config):
    proposal = Proposal(action="buy", ticker=TICKER, notional=dec(-10), confidence=dec(1))
    verdict = evaluate(proposal, make_inputs(), config)
    assert not verdict.approved
    assert verdict.rule == "R12_NO_SIZE"


# --------------------------------------------------------------------------- #
# Sell sizing — including the no-shorting rule
# --------------------------------------------------------------------------- #


def test_selling_something_not_held_is_refused(config):
    """This is the no-shorting rule: you cannot sell what you do not own."""
    inputs = make_inputs(account=make_account(cash=50, positions=[]))
    verdict = evaluate(sell(quantity=1), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R18_NO_POSITION"


def test_sell_is_clamped_to_the_quantity_actually_held(config):
    account = make_account(cash=0, positions=[make_position(quantity=2, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity=10), inputs, config)
    assert verdict.approved
    assert verdict.quantity == dec(-2)
    assert verdict.shrunk


def test_sell_quantity_is_negative(config):
    """Trading212 encodes a sell as a negative quantity."""
    account = make_account(cash=0, positions=[make_position(quantity=2, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity=1), inputs, config)
    assert verdict.quantity == dec(-1)
    assert verdict.side == "sell"
    assert verdict.notional == dec(10)


def test_sell_with_no_size_closes_the_position(config):
    account = make_account(cash=0, positions=[make_position(quantity="1.5", current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity=None), inputs, config)
    assert verdict.approved
    assert verdict.quantity == dec("-1.5")


def test_partial_sell_below_the_minimum_is_rejected():
    config = make_config(min_order=5)
    account = make_account(cash=0, positions=[make_position(quantity=10, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity="0.1"), inputs, config)
    assert not verdict.approved
    assert verdict.rule == "R16_BELOW_MIN"


def test_full_exit_is_allowed_below_the_minimum_order():
    """Otherwise a dust position could never be closed."""
    config = make_config(min_order=5)
    account = make_account(cash=0, positions=[make_position(quantity="0.1", current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity="0.1"), inputs, config)
    assert verdict.approved
    assert verdict.quantity == dec("-0.1")


def test_sell_sized_in_gbp_is_converted_to_shares(config):
    account = make_account(cash=0, positions=[make_position(quantity=5, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    proposal = Proposal(action="sell", ticker=TICKER, notional=dec(20), confidence=dec(1))
    verdict = evaluate(proposal, inputs, config)
    assert verdict.approved
    assert verdict.quantity == dec(-2)


def test_full_exit_sells_the_exact_holding_leaving_no_dust(config):
    """A holding with more precision than quantity_decimals still closes fully."""
    account = make_account(
        cash=0, positions=[make_position(quantity="1.123456789", current_price=10)]
    )
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity=None), inputs, config)
    assert verdict.quantity == dec("-1.123456789")


# --------------------------------------------------------------------------- #
# The core invariant
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("requested", ["0.01", "1", "12.5", "13", "50", "1000"])
def test_approved_order_is_never_larger_than_proposed(config, requested):
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    proposal = buy(notional=requested)
    verdict = evaluate(proposal, inputs, config)
    if verdict.approved:
        assert verdict.notional <= dec(requested) + Decimal("0.0001")


@pytest.mark.parametrize("cash", ["0", "0.5", "1", "13", "50", "1000"])
@pytest.mark.parametrize("price", ["0.01", "1", "9.99", "250"])
def test_approved_buy_never_exceeds_cash_or_caps(cash, price):
    """Sweep the sizing logic: every approval must satisfy every cap."""
    config = make_config(max_capital=50, cash_buffer="0.5")
    account = make_account(cash=cash)
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=price)})
    verdict = evaluate(buy(notional=1000), inputs, config)
    if not verdict.approved:
        return
    assert verdict.notional <= account.cash - config.capital.cash_buffer
    assert verdict.notional <= config.capital.per_trade_cap
    assert account.invested + verdict.notional <= config.capital.max_capital
    assert verdict.notional >= config.capital.min_order


# --------------------------------------------------------------------------- #
# Circuit breaker
# --------------------------------------------------------------------------- #


def test_breaker_trips_at_the_configured_loss(config):
    """-10% of £50 is -£5."""
    tripped, pnl, limit = circuit_breaker_state(dec(50), dec(45), config)
    assert tripped
    assert pnl == dec(-5)
    assert limit == dec(-5)


def test_breaker_holds_just_above_the_limit(config):
    tripped, pnl, _ = circuit_breaker_state(dec(50), dec("45.01"), config)
    assert not tripped
    assert pnl == dec("-4.99")


def test_breaker_ignores_gains(config):
    tripped, pnl, _ = circuit_breaker_state(dec(50), dec(60), config)
    assert not tripped
    assert pnl == dec(10)


def test_breaker_cannot_trip_without_a_baseline(config):
    tripped, pnl, _ = circuit_breaker_state(dec(0), dec(0), config)
    assert not tripped
    assert pnl == dec(0)


# --------------------------------------------------------------------------- #
# Pre-submit revalidation
# --------------------------------------------------------------------------- #


def test_revalidate_passes_an_unchanged_buy(config):
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=10), inputs, config)
    assert revalidate(verdict, make_account(cash=50), config) == verdict


def test_revalidate_shrinks_a_buy_when_cash_has_gone(config):
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=10), inputs, config)
    rechecked = revalidate(verdict, make_account(cash=4), config)
    assert rechecked.approved
    assert rechecked.shrunk
    assert rechecked.notional == dec(4)


def test_revalidate_rejects_a_buy_when_the_cash_is_all_gone(config):
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=10), inputs, config)
    rechecked = revalidate(verdict, make_account(cash=0), config)
    assert not rechecked.approved
    assert rechecked.rule == "R13_NO_CASH"


def test_revalidate_clamps_a_sell_to_a_reduced_holding(config):
    account = make_account(cash=0, positions=[make_position(quantity=3, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity=3), inputs, config)
    rechecked = revalidate(verdict, make_account(cash=0, positions=[make_position(quantity=1)]), config)
    assert rechecked.approved
    assert rechecked.quantity == dec(-1)


def test_revalidate_rejects_a_sell_when_the_position_has_gone(config):
    account = make_account(cash=0, positions=[make_position(quantity=3, current_price=10)])
    inputs = make_inputs(account=account, quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(sell(quantity=3), inputs, config)
    rechecked = revalidate(verdict, make_account(cash=0), config)
    assert not rechecked.approved
    assert rechecked.rule == "R18_NO_POSITION"


def test_revalidate_never_enlarges_an_order(config):
    inputs = make_inputs(account=make_account(cash=50), quotes={TICKER: make_quote(price=10)})
    verdict = evaluate(buy(notional=5), inputs, config)
    rechecked = revalidate(verdict, make_account(cash=1000), config)
    assert rechecked.notional == dec(5)


# --------------------------------------------------------------------------- #
# Rounding
# --------------------------------------------------------------------------- #


def test_floor_quantity_always_rounds_down(config):
    assert floor_quantity(dec("1.9999999"), config) == dec("1.999999")


def test_floor_quantity_in_whole_share_mode():
    config = make_config(fractional=False)
    assert floor_quantity(dec("1.99"), config) == dec(1)


def test_floor_quantity_of_a_negative_is_zero(config):
    assert floor_quantity(dec("-1"), config) == dec(0)
