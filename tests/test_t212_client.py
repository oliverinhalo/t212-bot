"""Auth, rate limiting, retry policy, and response mapping."""

from __future__ import annotations

import base64
from decimal import Decimal

import httpx
import pytest

from t212bot.t212_client import (
    Limit,
    RateLimiter,
    T212APIError,
    T212AuthError,
    T212Client,
    T212RateLimitError,
    T212TransportError,
    find_instrument,
    free_cash,
    positions_from_portfolio,
)

BASE = "https://demo.trading212.com/api/v0"


class FakeClock:
    """A clock that only moves when something sleeps, so waits are free."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def make_limiter(limits=None) -> tuple[RateLimiter, FakeClock]:
    clock = FakeClock()
    return RateLimiter(limits=limits or {}, sleep=clock.sleep, clock=clock), clock


def make_client(handler, **kwargs) -> T212Client:
    limiter, clock = make_limiter()
    transport = httpx.MockTransport(handler)
    return T212Client(
        base_url=BASE,
        api_key="key",
        api_secret="secret",
        client=httpx.Client(transport=transport),
        rate_limiter=limiter,
        sleep=clock.sleep,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #


def test_basic_auth_header_is_base64_of_key_and_secret():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={})

    make_client(handler).account_cash()

    expected = base64.b64encode(b"key:secret").decode()
    assert captured["auth"] == f"Basic {expected}"


def test_header_auth_scheme_sends_the_raw_key():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers["Authorization"]
        return httpx.Response(200, json={})

    make_client(handler, auth_scheme="header").account_cash()
    assert captured["auth"] == "key"


def test_unknown_auth_scheme_is_rejected():
    with pytest.raises(ValueError, match="auth scheme"):
        T212Client(base_url=BASE, api_key="k", auth_scheme="magic")


def test_401_gives_an_actionable_error():
    client = make_client(lambda r: httpx.Response(401, text="unauthorised"))
    with pytest.raises(T212AuthError, match="T212_AUTH_SCHEME"):
        client.account_cash()


def test_live_detection_is_based_on_the_base_url():
    assert not make_client(lambda r: httpx.Response(200, json={})).is_live
    live = T212Client(base_url="https://live.trading212.com/api/v0", api_key="k")
    assert live.is_live
    live.close()


# --------------------------------------------------------------------------- #
# Order conventions
# --------------------------------------------------------------------------- #


def test_a_sell_is_sent_as_a_negative_quantity():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"id": 1})

    make_client(handler).place_market_order("VUSAl_EQ", Decimal("-1.5"))
    assert captured == {"ticker": "VUSAl_EQ", "quantity": -1.5}


def test_a_zero_quantity_order_is_refused_before_any_request():
    def handler(request):  # pragma: no cover - must never run
        raise AssertionError("no request should be made")

    with pytest.raises(ValueError, match="zero-quantity"):
        make_client(handler).place_market_order("VUSAl_EQ", Decimal(0))


def test_a_non_positive_limit_price_is_refused():
    def handler(request):  # pragma: no cover
        raise AssertionError("no request should be made")

    with pytest.raises(ValueError, match="limit price"):
        make_client(handler).place_limit_order("X", Decimal(1), Decimal(0))


# --------------------------------------------------------------------------- #
# Retry policy — the important part
# --------------------------------------------------------------------------- #


def test_orders_are_never_retried_on_a_transport_failure():
    """Retrying a non-idempotent order endpoint can double-place it."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectTimeout("boom")

    with pytest.raises(T212TransportError):
        make_client(handler).place_market_order("X", Decimal(1))
    assert len(attempts) == 1


def test_orders_are_never_retried_on_a_429():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(429, text="slow down")

    with pytest.raises(T212RateLimitError):
        make_client(handler).place_market_order("X", Decimal(1))
    assert len(attempts) == 1


def test_orders_are_never_retried_on_a_server_error():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="unavailable")

    with pytest.raises(T212APIError):
        make_client(handler).place_market_order("X", Decimal(1))
    assert len(attempts) == 1


def test_read_only_calls_do_retry_a_server_error():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(500, text="oops")
        return httpx.Response(200, json={"free": 12.0})

    assert make_client(handler).account_cash() == {"free": 12.0}
    assert len(attempts) == 3


def test_read_only_calls_retry_a_rate_limit():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"retry-after": "1"})
        return httpx.Response(200, json={"free": 1.0})

    make_client(handler).account_cash()
    assert len(attempts) == 2


def test_a_4xx_is_not_retried():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(400, text="bad ticker")

    with pytest.raises(T212APIError):
        make_client(handler).account_cash()
    assert len(attempts) == 1


# --------------------------------------------------------------------------- #
# Rate limiter
# --------------------------------------------------------------------------- #


def test_rate_limiter_spaces_calls_in_the_same_group():
    limiter, clock = make_limiter({"account": Limit(min_interval=5.0)})
    limiter.acquire("account")
    limiter.acquire("account")
    assert sum(clock.slept) >= 5.0


def test_rate_limiter_does_not_block_across_groups():
    limiter, clock = make_limiter(
        {"account": Limit(min_interval=5.0), "portfolio": Limit(min_interval=5.0)}
    )
    limiter.acquire("account")
    limiter.acquire("portfolio")
    assert clock.slept == []


def test_rate_limiter_enforces_a_per_minute_cap():
    limiter, clock = make_limiter({"orders_market": Limit(min_interval=0.0, per_minute=2)})
    for _ in range(3):
        limiter.acquire("orders_market")
    assert sum(clock.slept) >= 60.0


def test_a_429_pushes_the_next_call_out():
    limiter, clock = make_limiter({"account": Limit(min_interval=0.0)})
    limiter.penalise("account", 30.0)
    limiter.acquire("account")
    assert sum(clock.slept) >= 30.0


# --------------------------------------------------------------------------- #
# Response mapping
# --------------------------------------------------------------------------- #


def test_portfolio_maps_onto_positions():
    positions = positions_from_portfolio(
        [{"ticker": "VUSAl_EQ", "quantity": 1.5, "averagePrice": 10.0, "currentPrice": 12.0}]
    )
    assert positions[0].quantity == Decimal("1.5")
    assert positions[0].value == Decimal("18.0")
    assert isinstance(positions[0].current_price, Decimal)


def test_closed_positions_are_skipped():
    assert positions_from_portfolio([{"ticker": "X", "quantity": 0}]) == ()


def test_free_cash_prefers_the_spendable_field():
    assert free_cash({"free": 12.34, "total": 99.0, "invested": 50.0}) == Decimal("12.34")


def test_missing_cash_field_is_an_error():
    from t212bot.t212_client import T212Error

    with pytest.raises(T212Error):
        free_cash({"total": 1})


def test_instrument_search_matches_name_and_ticker():
    universe = [
        {"ticker": "VUSAl_EQ", "name": "Vanguard S&P 500", "isin": "IE00B3XXRP09"},
        {"ticker": "ISFl_EQ", "name": "iShares Core FTSE 100", "isin": "IE0005042456"},
    ]
    assert len(find_instrument(universe, "vusa")) == 1
    assert len(find_instrument(universe, "ftse")) == 1
    assert find_instrument(universe, "tesla") == []
