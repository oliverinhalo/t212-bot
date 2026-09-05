"""Thin, rate-limit-aware wrapper over the Trading212 public API.

Scope is deliberately small: Invest / Stocks-ISA equity endpoints only. There is
no CFD, no leverage, and no short selling here — not as a policy, but because
this client simply has no method that could express one.

Two conventions from the T212 API that the whole codebase inherits:

* **Sell is a negative quantity**, buy is positive, for every order type.
* **Order endpoints are not idempotent in this beta.** A retried request can
  create a second order. So order submission is the one call this client will
  never retry: ``_request(..., retry=False)``. Network failure on an order
  raises, and executor.py records it as ``unknown`` for a human to reconcile.

Rate limits are per-account and tight. Each endpoint group gets its own token
bucket, and ``x-ratelimit-*`` response headers tighten the bucket further when
the server tells us to slow down.
"""

from __future__ import annotations

import base64
import logging
import random
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

import httpx

from .models import Position, ZERO, maybe_dec

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 30.0
MAX_RETRIES = 4


class T212Error(Exception):
    """Base class for all Trading212 client failures."""


class T212AuthError(T212Error):
    """401/403 — bad key, wrong environment, or the wrong auth scheme."""


class T212RateLimitError(T212Error):
    """429 that survived our own backoff."""


class T212APIError(T212Error):
    def __init__(self, status_code: int, body: str, path: str):
        super().__init__(f"{status_code} from {path}: {body[:400]}")
        self.status_code = status_code
        self.body = body
        self.path = path


class T212TransportError(T212Error):
    """Connection failed or timed out. For an order, the outcome is unknown."""


# --------------------------------------------------------------------------- #
# Rate limiting
# --------------------------------------------------------------------------- #


@dataclass
class Limit:
    """One endpoint group's budget."""

    min_interval: float  # seconds between calls
    per_minute: int | None = None


# Documented per-account limits, with a little headroom. Being throttled costs
# a cycle; being rate-limited mid-order costs certainty about what happened.
LIMITS: dict[str, Limit] = {
    "account": Limit(min_interval=5.5),
    "portfolio": Limit(min_interval=5.5),
    "metadata": Limit(min_interval=10.0),
    "history": Limit(min_interval=6.0),
    "orders_market": Limit(min_interval=1.5, per_minute=45),
    "orders_other": Limit(min_interval=2.2),
    "orders_read": Limit(min_interval=2.2),
}


class RateLimiter:
    """Per-group spacing, with a rolling per-minute cap where one applies."""

    def __init__(
        self,
        limits: Mapping[str, Limit] | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ):
        self._limits = dict(limits or LIMITS)
        self._last: dict[str, float] = {}
        self._recent: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._sleep = sleep
        self._clock = clock

    def limit_for(self, group: str) -> Limit:
        return self._limits.get(group, Limit(min_interval=1.0))

    def acquire(self, group: str) -> float:
        """Block until the group is clear to call. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = self._clock()
                limit = self.limit_for(group)
                delay = 0.0

                last = self._last.get(group)
                if last is not None:
                    delay = max(delay, limit.min_interval - (now - last))

                if limit.per_minute:
                    window = [t for t in self._recent.get(group, []) if now - t < 60.0]
                    self._recent[group] = window
                    if len(window) >= limit.per_minute:
                        delay = max(delay, 60.0 - (now - window[0]))

                if delay <= 0:
                    self._last[group] = now
                    if limit.per_minute:
                        self._recent.setdefault(group, []).append(now)
                    return waited

            self._sleep(delay)
            waited += delay

    def penalise(self, group: str, seconds: float) -> None:
        """Push a group's next allowed call further out (server told us to)."""
        with self._lock:
            limit = self.limit_for(group)
            self._last[group] = self._clock() + max(0.0, seconds - limit.min_interval)


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #


class T212Client:
    """Trading212 equity API client.

    ``base_url`` decides demo vs live; nothing else in the class does, so there
    is no way to accidentally hit live while believing you are on demo.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        api_secret: str = "",
        *,
        auth_scheme: str = "basic",
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        rate_limiter: RateLimiter | None = None,
        sleep=time.sleep,
    ):
        if not base_url:
            raise ValueError("base_url is required")
        self.base_url = base_url.rstrip("/")
        self.auth_scheme = auth_scheme
        self._limiter = rate_limiter or RateLimiter(sleep=sleep)
        self._sleep = sleep
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=timeout, follow_redirects=False)
        self._headers = self._build_auth_headers(api_key, api_secret, auth_scheme)

    @staticmethod
    def _build_auth_headers(api_key: str, api_secret: str, scheme: str) -> dict[str, str]:
        """Build the Authorization header. Never logged, never stored elsewhere."""
        headers = {"Accept": "application/json", "User-Agent": "t212-bot/0.1"}
        if scheme == "basic":
            token = base64.b64encode(f"{api_key}:{api_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        elif scheme == "header":
            headers["Authorization"] = api_key
        else:
            raise ValueError(f"unknown auth scheme {scheme!r}")
        return headers

    @property
    def is_live(self) -> bool:
        return "live.trading212.com" in self.base_url

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "T212Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- plumbing
    def _request(
        self,
        method: str,
        path: str,
        *,
        group: str,
        json_body: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        retry: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        attempt = 0

        while True:
            attempt += 1
            self._limiter.acquire(group)
            try:
                response = self._http.request(
                    method, url, headers=self._headers, json=json_body, params=params
                )
            except httpx.HTTPError as exc:
                # For a non-retryable call (an order) the caller must treat this
                # as "outcome unknown", never as "it did not happen".
                if not retry or attempt > MAX_RETRIES:
                    raise T212TransportError(f"{method} {path} failed: {exc}") from exc
                self._backoff(attempt, f"transport error on {path}: {exc}")
                continue

            self._absorb_rate_limit_headers(group, response)

            if response.status_code in (401, 403):
                raise T212AuthError(
                    f"{response.status_code} from {path} — check T212_API_KEY/"
                    "T212_API_SECRET, T212_AUTH_SCHEME, and that the key belongs to "
                    f"{'live' if self.is_live else 'demo'}"
                )

            if response.status_code == 429:
                if not retry or attempt > MAX_RETRIES:
                    raise T212RateLimitError(f"rate limited on {path} after {attempt} attempts")
                self._limiter.penalise(group, self._retry_after(response, attempt))
                log.warning("rate limited on %s, attempt %d", path, attempt)
                continue

            if response.status_code >= 500:
                if not retry or attempt > MAX_RETRIES:
                    raise T212APIError(response.status_code, response.text, path)
                self._backoff(attempt, f"server error {response.status_code} on {path}")
                continue

            if response.status_code >= 400:
                raise T212APIError(response.status_code, response.text, path)

            log.debug("%s %s -> %d", method, path, response.status_code)
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise T212APIError(response.status_code, response.text, path) from exc

    def _backoff(self, attempt: int, why: str) -> None:
        delay = min(2.0 ** attempt + random.uniform(0, 1), 30.0)
        log.warning("%s — retrying in %.1fs", why, delay)
        self._sleep(delay)

    @staticmethod
    def _retry_after(response: httpx.Response, attempt: int) -> float:
        for header in ("retry-after", "x-ratelimit-reset-after", "x-ratelimit-reset"):
            raw = response.headers.get(header)
            if raw:
                try:
                    return min(max(float(raw), 1.0), 120.0)
                except ValueError:
                    continue
        return min(2.0 ** attempt, 30.0)

    def _absorb_rate_limit_headers(self, group: str, response: httpx.Response) -> None:
        """Slow down pre-emptively when the server says we are nearly out."""
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset-after") or response.headers.get(
            "x-ratelimit-reset"
        )
        if remaining is None:
            return
        try:
            if int(float(remaining)) <= 0 and reset:
                self._limiter.penalise(group, min(max(float(reset), 1.0), 120.0))
        except ValueError:
            return

    # ---------------------------------------------------------------- account
    def account_cash(self) -> dict[str, Any]:
        """GET /equity/account/cash — free, total, invested, ppl, result."""
        return self._request("GET", "/equity/account/cash", group="account")

    def account_info(self) -> dict[str, Any]:
        """GET /equity/account/info — currency code and account id."""
        return self._request("GET", "/equity/account/info", group="account")

    def portfolio(self) -> list[dict[str, Any]]:
        """GET /equity/portfolio — all open positions."""
        return self._request("GET", "/equity/portfolio", group="portfolio") or []

    def position(self, ticker: str) -> dict[str, Any] | None:
        try:
            return self._request("GET", f"/equity/portfolio/{ticker}", group="portfolio")
        except T212APIError as exc:
            if exc.status_code == 404:
                return None
            raise

    # --------------------------------------------------------------- metadata
    def instruments(self) -> list[dict[str, Any]]:
        """GET /equity/metadata/instruments — the full tradable universe.

        Large (several MB) and heavily rate-limited; call it from scripts, not
        from a trading cycle.
        """
        return self._request("GET", "/equity/metadata/instruments", group="metadata") or []

    def exchanges(self) -> list[dict[str, Any]]:
        return self._request("GET", "/equity/metadata/exchanges", group="metadata") or []

    # ----------------------------------------------------------------- orders
    def orders(self) -> list[dict[str, Any]]:
        """GET /equity/orders — currently working orders."""
        return self._request("GET", "/equity/orders", group="orders_read") or []

    def get_order(self, order_id: str | int) -> dict[str, Any]:
        return self._request("GET", f"/equity/orders/{order_id}", group="orders_read")

    def cancel_order(self, order_id: str | int) -> Any:
        return self._request("DELETE", f"/equity/orders/{order_id}", group="orders_read")

    def historical_orders(self, cursor: int | None = None, limit: int = 50) -> dict[str, Any]:
        """GET /equity/history/orders — cursor-paginated order history."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        return self._request("GET", "/equity/history/orders", group="history", params=params) or {}

    def place_market_order(self, ticker: str, quantity: Decimal) -> dict[str, Any]:
        """POST /equity/orders/market. Negative quantity sells.

        Never retried — see the module docstring.
        """
        if quantity == ZERO:
            raise ValueError("refusing to place a zero-quantity order")
        return self._request(
            "POST",
            "/equity/orders/market",
            group="orders_market",
            json_body={"ticker": ticker, "quantity": float(quantity)},
            retry=False,
        )

    def place_limit_order(
        self,
        ticker: str,
        quantity: Decimal,
        limit_price: Decimal,
        time_validity: str = "DAY",
    ) -> dict[str, Any]:
        """POST /equity/orders/limit. Negative quantity sells. Never retried."""
        if quantity == ZERO:
            raise ValueError("refusing to place a zero-quantity order")
        if limit_price <= ZERO:
            raise ValueError(f"limit price must be positive, got {limit_price}")
        return self._request(
            "POST",
            "/equity/orders/limit",
            group="orders_other",
            json_body={
                "ticker": ticker,
                "quantity": float(quantity),
                "limitPrice": float(limit_price),
                "timeValidity": time_validity,
            },
            retry=False,
        )


# --------------------------------------------------------------------------- #
# Response mapping
# --------------------------------------------------------------------------- #


def positions_from_portfolio(payload: list[Mapping[str, Any]]) -> tuple[Position, ...]:
    """Map the /equity/portfolio payload onto our Position type."""
    out: list[Position] = []
    for item in payload or []:
        quantity = maybe_dec(item.get("quantity")) or ZERO
        if quantity <= ZERO:
            continue  # the API has no short positions for Invest accounts
        average = maybe_dec(item.get("averagePrice")) or ZERO
        current = maybe_dec(item.get("currentPrice")) or average
        out.append(
            Position(
                ticker=str(item.get("ticker", "")),
                quantity=quantity,
                average_price=average,
                current_price=current,
            )
        )
    return tuple(out)


def free_cash(payload: Mapping[str, Any]) -> Decimal:
    """Spendable cash from /equity/account/cash.

    ``free`` is what can actually be used for a new order; ``total`` includes
    the value of open positions, and spending against it would blow the cap.
    """
    for key in ("free", "freeForStocks", "cash"):
        value = maybe_dec(payload.get(key))
        if value is not None:
            return value
    raise T212Error(f"no cash field in account payload: {sorted(payload)}")


def find_instrument(instruments: list[Mapping[str, Any]], query: str) -> list[dict[str, Any]]:
    """Case-insensitive search over ticker/name/isin, for scripts/list_instruments."""
    needle = query.strip().lower()
    hits = []
    for item in instruments:
        haystack = " ".join(
            str(item.get(field, "")) for field in ("ticker", "name", "shortName", "isin")
        ).lower()
        if needle in haystack:
            hits.append(dict(item))
    return hits


__all__ = [
    "T212Client",
    "RateLimiter",
    "Limit",
    "LIMITS",
    "T212Error",
    "T212AuthError",
    "T212APIError",
    "T212RateLimitError",
    "T212TransportError",
    "positions_from_portfolio",
    "free_cash",
    "find_instrument",
]
