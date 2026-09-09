"""One cycle, and the scheduler that runs it.

A cycle, in order:

    kill switch -> unresolved orders -> circuit breaker -> market hours
      -> market data -> account state -> P&L and breaker re-check
      -> AI -> risk manager -> execute -> audit log

The checks that can halt the cycle run *before* the AI call, so a stopped bot
does not spend API quota deciding things it will not act on.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import logging.handlers
import signal
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .ai_advisor import advise, build_provider as build_ai_provider
from .config import AppConfig, ConfigError, load
from .executor import Executor
from .fx import FxConverter, FxError
from .indicators import compute_all, compute_signals, market_regime
from .instruments import InstrumentCatalogue
from .local_strategy import advise_locally
from .market_data import (
    MarketDataError,
    Snapshot,
    SymbolResolver,
    build_provider as build_market_provider,
    currencies_match,
)
from .models import ZERO, AIResult, Proposal, RiskInputs, money, summarise_positions, utcnow
from .portfolio import load_account_state
from .risk_manager import circuit_breaker_state, evaluate
from .storage import Storage
from .t212_client import T212Client, T212Error

log = logging.getLogger("t212bot")

_WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


def setup_logging(config: AppConfig) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.logging.level, logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    if config.logging.file:
        config.logging.file.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            config.logging.file, maxBytes=5_000_000, backupCount=5
        )
        rotating.setFormatter(fmt)
        root.addHandler(rotating)

    # These are chatty and can echo request bodies at DEBUG.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


@dataclass
class Runtime:
    """Long-lived collaborators, built once and reused across cycles."""

    config: AppConfig
    storage: Storage
    market: Any
    ai: Any
    client: T212Client | None
    executor: Executor
    catalogue: InstrumentCatalogue | None = None
    fx: FxConverter | None = None
    resolver: SymbolResolver | None = None

    def close(self) -> None:
        for target in (
            self.market, self.ai, self.client, self.fx, self.resolver, self.storage
        ):
            closer = getattr(target, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - shutdown must not raise
                    log.debug("error closing %r", target, exc_info=True)


def build_runtime(config: AppConfig) -> Runtime:
    storage = Storage(config.storage.db_path)

    client: T212Client | None = None
    # Even paper mode gets a client when credentials exist: read-only calls are
    # useful for sanity-checking the account. It is never handed to the paper
    # execution path, which cannot call an order endpoint at all.
    if config.secrets.t212_api_key:
        client = T212Client(
            base_url=config.t212_base_url,
            api_key=config.secrets.t212_api_key,
            api_secret=config.secrets.t212_api_secret,
            auth_scheme=config.t212_auth_scheme,
        )

    # Open-universe mode: the catalogue is the ticker safety net and the FX
    # layer values foreign instruments in GBP. config.load() has already
    # guaranteed the catalogue file exists when enforce_allowlist is off.
    catalogue: InstrumentCatalogue | None = None
    fx: FxConverter | None = None
    resolver: SymbolResolver | None = None
    if not config.risk.enforce_allowlist:
        catalogue = InstrumentCatalogue.load(config.instruments_path)
        if catalogue is None or len(catalogue) == 0:
            raise ConfigError(
                f"instrument catalogue {config.instruments_path} is missing or empty — "
                "run: python -m scripts.list_instruments --refresh"
            )
        fx = FxConverter(
            timeout=config.market_data.timeout_seconds,
            cache_seconds=config.market_data.fx_cache_seconds,
        )
        resolver = SymbolResolver(
            overrides=config.symbol_overrides,
            cache_path=config.instruments_path.parent / "symbol_map.json",
            timeout=config.market_data.timeout_seconds,
            search=config.market_data.symbol_search,
        )

    return Runtime(
        config=config,
        storage=storage,
        market=build_market_provider(config),
        ai=build_ai_provider(config),
        client=client,
        executor=Executor(config, storage, client),
        catalogue=catalogue,
        fx=fx,
        resolver=resolver,
    )


# --------------------------------------------------------------------------- #
# Clock helpers
# --------------------------------------------------------------------------- #


def local_now(config: AppConfig) -> datetime:
    return datetime.now(ZoneInfo(config.schedule.timezone))


def trading_day(config: AppConfig, now: datetime | None = None) -> date:
    """The calendar day used for all daily counters, in the configured tz."""
    return (now or local_now(config)).date()


def _parse_hhmm(value: str) -> dtime:
    hours, _, minutes = value.partition(":")
    return dtime(int(hours), int(minutes or 0))


def within_trading_window(config: AppConfig, now: datetime | None = None) -> tuple[bool, str]:
    now = now or local_now(config)
    weekday = _WEEKDAY_NAMES[now.weekday()]
    if weekday not in config.schedule.trading_days:
        return False, f"{weekday} is not a configured trading day"
    opens = _parse_hhmm(config.schedule.market_open)
    closes = _parse_hhmm(config.schedule.market_close)
    if not (opens <= now.time() <= closes):
        return False, (
            f"{now.strftime('%H:%M')} is outside "
            f"{config.schedule.market_open}-{config.schedule.market_close} "
            f"{config.schedule.timezone}"
        )
    return True, ""


# --------------------------------------------------------------------------- #
# Advice: LLM, or local fallback, or a cached hold
# --------------------------------------------------------------------------- #


def _cycle_fingerprint(
    account: Any,
    snapshot: Snapshot,
    trades_today: int,
    regime: str,
    min_move_pct: Decimal,
) -> str:
    """A stable digest of everything that would change the AI's answer.

    Prices are bucketed by ``min_move_pct`` so a sub-threshold tick does not
    invalidate the cache; positions are included verbatim.
    """
    rel = max(min_move_pct, Decimal("0.01")) / Decimal(100)
    parts = [f"regime={regime}", f"trades={trades_today}"]
    for ticker in sorted(snapshot.quotes):
        price = snapshot.quotes[ticker].price
        bucket = "0" if price <= ZERO else str(int(price / (price * rel)))
        parts.append(f"{ticker}={bucket}")
    for position in sorted(account.positions, key=lambda p: p.ticker):
        parts.append(f"pos:{position.ticker}={position.quantity}")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def _synth_hold(provider: str, model: str, reason: str) -> AIResult:
    return AIResult(
        proposal=Proposal(action="hold", reasoning=reason),
        provider=provider,
        model=model,
        prompt="",
        raw_response="",
        latency_ms=0,
        error=None,
        http_calls=0,
    )


def decide_advice(
    runtime: Runtime,
    account: Any,
    snapshot: Snapshot,
    signals: dict,
    regime: str,
    *,
    trades_today: int,
    day_pnl: Decimal,
    trading_day: date,
    insist: bool = False,
) -> AIResult:
    """The decision ladder: cache skip -> budget guard -> LLM -> local fallback.

    ``insist`` is set when an operator is deliberately retrying (``--until-trade``).
    Retries happen precisely because nothing has changed yet, so the
    "nothing moved, don't spend a call" skip would turn every attempt after the
    first into a no-op. Under ``insist`` the model is asked every time.
    """
    config = runtime.config
    storage = runtime.storage
    provider_name = getattr(runtime.ai, "name", "unknown")
    fingerprint = _cycle_fingerprint(
        account, snapshot, trades_today, regime, config.ai.min_price_move_pct
    )
    utc_day = utcnow().date()

    def finish(result: AIResult) -> AIResult:
        storage.record_cycle_fingerprint(trading_day, fingerprint, result.proposal.action)
        return result

    # 1. Nothing material changed since a cycle that held — do not spend a call.
    if (
        config.ai.skip_when_unchanged
        and not insist
        and storage.last_cycle_fingerprint() == (fingerprint, "hold")
    ):
        log.info("no material change since the last cycle; skipping the AI call")
        return finish(
            _synth_hold("cache", "skip", "No material change since the last cycle; held.")
        )

    # 2. Free-tier daily budget. Hand over to the local strategy before the cap.
    #    OpenRouter only: it is the one provider with a hard free-tier cap.
    #    OmniRoute is self-hosted and unmetered, Anthropic is pay-as-you-go, and
    #    a budget of 0 or less means "no limit" for everyone.
    if provider_name == "openrouter" and config.ai.daily_request_budget > 0:
        used = storage.ai_calls_today(utc_day)
        if used >= config.ai.daily_request_budget:
            log.warning(
                "OpenRouter budget spent (%d/%d today) — using the local strategy",
                used,
                config.ai.daily_request_budget,
            )
            if config.ai.local_fallback:
                return finish(
                    advise_locally(
                        config, account, snapshot, signals, regime,
                        trades_today=trades_today, day_pnl=day_pnl,
                    )
                )
            return finish(
                _synth_hold(
                    "budget", "none",
                    f"OpenRouter daily budget spent ({used}/{config.ai.daily_request_budget}).",
                )
            )

    # 3. Normal path: ask the model.
    result = advise(
        runtime.ai, config, account, snapshot,
        signals=signals, regime=regime,
        open_universe=not config.risk.enforce_allowlist,
        trades_today=trades_today, day_pnl=day_pnl,
    )
    if provider_name == "openrouter" and result.http_calls:
        storage.record_ai_calls(utc_day, result.http_calls)

    # 4. The model failed. A local proposal beats a blind hold.
    if result.error and config.ai.local_fallback and provider_name in ("openrouter", "omniroute"):
        log.warning("AI provider failed (%s) — using the local strategy", result.error)
        local = advise_locally(
            config, account, snapshot, signals, regime,
            trades_today=trades_today, day_pnl=day_pnl,
        )
        return finish(
            replace(local, error=f"LLM unavailable ({result.error}); local strategy used")
        )

    return finish(result)


# --------------------------------------------------------------------------- #
# Open-universe: resolve and price an instrument the AI named off the list
# --------------------------------------------------------------------------- #


def resolve_off_list_instrument(
    runtime: Runtime,
    proposal: Proposal,
    snapshot: Snapshot,
    signals: dict,
) -> tuple[Proposal, dict | None]:
    """Price an off-watch-list instrument the AI proposed.

    Returns ``(proposal, audit)``. ``proposal.ticker`` is rewritten to the
    canonical Trading212 ticker on success and the quote/history are inserted
    into ``snapshot`` (its dicts are mutable). On *any* failure the proposal is
    returned unchanged with no quote added, so the risk manager rejects it
    (R05_ALLOWLIST via the priced-ticker set, or R06_QUOTE_MISSING).
    """
    config = runtime.config
    if (
        config.risk.enforce_allowlist
        or not proposal.is_trade
        or not proposal.ticker
        or proposal.ticker in snapshot.quotes
        or runtime.catalogue is None
    ):
        return proposal, None

    def fail(resolved: str | None, reason: str, **extra: object) -> tuple[Proposal, dict]:
        log.warning("off-list %r rejected: %s", proposal.ticker, reason)
        return proposal, {"query": proposal.ticker, "resolved": resolved, "reason": reason, **extra}

    instrument = runtime.catalogue.resolve(proposal.ticker)
    if instrument is None:
        return fail(None, "not found in the Trading212 catalogue")
    if not instrument.is_equity_like:
        return fail(instrument.ticker, f"instrument type is {instrument.type or 'unknown'}")

    symbol = runtime.resolver.resolve(instrument) if runtime.resolver else None
    if not symbol:
        return fail(instrument.ticker, "no Yahoo symbol could be resolved", isin=instrument.isin)

    try:
        quote, history = runtime.market.fetch_symbol(
            instrument.ticker, symbol, config.max_history_days
        )
    except MarketDataError as exc:
        return fail(instrument.ticker, f"quote fetch failed: {exc}", yahoo=symbol)

    if not currencies_match(quote.currency, instrument.currency):
        # Try derived exchange ticker if ISIN search returned an alternate currency listing
        derived = (
            getattr(runtime.resolver, "_derive", lambda _: None)(instrument)
            if runtime.resolver
            else None
        )
        if derived and derived != symbol:
            try:
                derived_quote, derived_history = runtime.market.fetch_symbol(
                    instrument.ticker, derived, config.max_history_days
                )
                if currencies_match(derived_quote.currency, instrument.currency):
                    quote, history, symbol = derived_quote, derived_history, derived
                    if hasattr(runtime.resolver, "_remember") and instrument.isin:
                        runtime.resolver._remember(instrument.isin, symbol)
            except MarketDataError:
                pass

        if not currencies_match(quote.currency, instrument.currency):
            return fail(
                instrument.ticker,
                f"currency mismatch (Yahoo {quote.currency} vs T212 {instrument.currency}) "
                "— probably the wrong listing",
                yahoo=symbol,
            )

    native_price, native_currency = quote.price, quote.currency
    fx_rate = Decimal(1)
    gbp_price = quote.price
    if instrument.currency not in ("", "GBP", "GBX", "GBP.") and runtime.fx is not None:
        try:
            fx_rate = runtime.fx.rate(instrument.currency)
        except FxError as exc:
            return fail(instrument.ticker, f"FX conversion failed: {exc}", yahoo=symbol)
        gbp_price = quote.price / fx_rate

    quote = replace(
        quote,
        price=gbp_price,
        currency="GBP",
        native_currency=native_currency,
        native_price=native_price,
    )
    if fx_rate != Decimal(1):
        history = replace(
            history,
            bars=tuple(replace(bar, close=bar.close / fx_rate) for bar in history.bars),
        )

    snapshot.quotes[instrument.ticker] = quote
    snapshot.histories[instrument.ticker] = history
    sig = compute_signals(history, quote, config.ai.indicators)
    if sig is not None:
        signals[instrument.ticker] = sig

    audit = {
        "query": proposal.ticker,
        "resolved": instrument.ticker,
        "yahoo": symbol,
        "native": f"{native_price} {native_currency}",
        "gbp_price": str(money(gbp_price)),
        "fx_rate": str(fx_rate),
    }
    return replace(proposal, ticker=instrument.ticker), audit


# --------------------------------------------------------------------------- #
# The cycle
# --------------------------------------------------------------------------- #


def run_cycle(
    runtime: Runtime,
    *,
    dry_run: bool = False,
    force: bool = False,
    insist: bool = False,
) -> str:
    """Run one full decision cycle. Returns a short status string.

    ``insist`` only reaches the AI ladder, where it disables the
    "nothing changed, skip the call" shortcut. It grants no extra permission:
    every risk rule applies exactly as it does on a scheduled cycle.
    """
    config = runtime.config
    storage = runtime.storage
    decision_id = uuid.uuid4().hex
    now = local_now(config)
    day = trading_day(config, now)

    storage.start_cycle(decision_id, day, config.mode)
    log.info("cycle %s starting (mode=%s, day=%s)", decision_id[:8], config.mode, day)

    def halt(reason: str, status: str = "halted") -> str:
        log.warning("cycle %s %s: %s", decision_id[:8], status, reason)
        storage.finish_cycle(decision_id, status, reason)
        return status

    # 1. Kill switch, before anything else happens.
    if config.stop_file.exists():
        return halt(f"kill switch present: {config.stop_file} exists — delete it to resume")

    # 2. An unreconciled order means we do not know our true position.
    unresolved = storage.unresolved_orders()
    if unresolved:
        ids = ", ".join(o.decision_id[:8] for o in unresolved)
        return halt(
            f"{len(unresolved)} unresolved order(s) [{ids}] — check the Trading212 app, "
            "then run --resolve-order <decision_id>"
        )

    # 3. Circuit breaker, as recorded on a previous cycle.
    breaker = storage.breaker()
    if breaker is not None:
        return halt(
            f"circuit breaker tripped on {breaker['trading_day']} ({breaker['reason']}) — "
            "restart manually with --reset-breaker"
        )

    # 4. Market hours.
    open_now, why_closed = within_trading_window(config, now)
    if not open_now and not force:
        return halt(f"market closed: {why_closed}", status="skipped")

    # 5. Chase up anything the broker accepted but had not filled.
    try:
        reconciled = runtime.executor.reconcile_open_orders()
        if reconciled:
            log.info("reconciled %d open order(s)", reconciled)
    except T212Error as exc:
        log.warning("order reconciliation failed: %s", exc)

    # 6. Market data. Tickers that fail are simply untradeable this cycle.
    snapshot: Snapshot = runtime.market.fetch(config.watchlist, config.max_history_days)
    if not snapshot.quotes:
        return halt(f"no market data for any watch-list ticker: {snapshot.errors}")
    for ticker, error in snapshot.errors.items():
        log.warning("no quote for %s: %s", ticker, error)

    # 6b. Local technical analysis over the daily closes. No API cost.
    signals = compute_all(
        config.watchlist, snapshot.quotes, snapshot.histories, config.ai.indicators
    )
    regime = market_regime(config.watchlist, signals)
    if signals:
        log.info(
            "regime %s; %s",
            regime,
            ", ".join(f"{t} {s.trend}({s.score:+.2f})" for t, s in signals.items()),
        )

    # 7. Account state.
    try:
        account = load_account_state(
            config, storage, runtime.client, snapshot.prices(),
            catalogue=runtime.catalogue, fx=runtime.fx,
        )
    except (T212Error, ValueError) as exc:
        return halt(f"could not load account state: {exc}", status="error")

    # 8. P&L and the circuit breaker.
    day_start_equity = storage.record_equity(day, account.equity)
    tripped, pnl, limit = circuit_breaker_state(day_start_equity, account.equity, config)
    trades_today = storage.trades_today(day)

    def snapshot_record() -> dict:
        return {
            "as_of": now.isoformat(),
            "mode": config.mode,
            "cash": str(account.cash),
            "invested": str(account.invested),
            "equity": str(account.equity),
            "day_start_equity": str(day_start_equity),
            "day_pnl": str(pnl),
            "trades_today": trades_today,
            "positions": summarise_positions(account.positions),
            "quotes": {
                ticker: {
                    "price": str(quote.price),
                    "currency": quote.currency,
                    "as_of": quote.as_of.isoformat(),
                    "source": quote.source,
                    **(
                        {"native": f"{quote.native_price} {quote.native_currency}"}
                        if quote.native_price is not None
                        else {}
                    ),
                }
                for ticker, quote in snapshot.quotes.items()
            },
            "quote_errors": snapshot.errors,
            "regime": regime,
            "signals": {ticker: sig.as_dict() for ticker, sig in signals.items()},
        }

    storage.record_snapshot(decision_id, snapshot_record())

    log.info(
        "equity %s (cash %s, invested %s), P&L today %s of %s limit, trades %d/%d",
        money(account.equity),
        money(account.cash),
        money(account.invested),
        money(pnl),
        money(limit),
        trades_today,
        config.risk.max_trades_per_day,
    )

    if tripped:
        reason = f"daily P&L {money(pnl)} breached the {money(limit)} limit"
        storage.trip_breaker(day, reason, pnl)
        return halt(f"circuit breaker tripped: {reason}")

    # 9. Ask the AI — or the local strategy, or a cached hold if nothing moved.
    result = decide_advice(
        runtime,
        account,
        snapshot,
        signals,
        regime,
        trades_today=trades_today,
        day_pnl=pnl,
        trading_day=day,
        insist=insist,
    )
    storage.record_ai(decision_id, result)
    log.info(
        "AI (%s/%s) proposed %s %s conf=%s: %s",
        result.provider,
        result.model,
        result.proposal.action,
        result.proposal.ticker or "-",
        result.proposal.confidence,
        result.proposal.reasoning[:200],
    )

    # 9b. Open-universe: price an instrument the AI named that we do not already
    #     hold data for. On any failure it is simply left unpriced and the risk
    #     manager rejects it (R05/R06).
    proposal, resolution = resolve_off_list_instrument(
        runtime, result.proposal, snapshot, signals
    )
    if resolution is not None:
        record = snapshot_record()
        record["resolved"] = resolution
        storage.record_snapshot(decision_id, record)
        log.info("off-list instrument: %s", resolution)

    # 10. Risk manager. The only thing that can authorise an order.
    if config.risk.enforce_allowlist:
        allowed_tickers: frozenset[str] | None = None
    else:
        allowed_tickers = frozenset(snapshot.quotes)
    inputs = RiskInputs(
        decision_id=decision_id,
        account=account,
        quotes=snapshot.quotes,
        trades_today=trades_today,
        day_start_equity=day_start_equity,
        breaker_tripped=False,
        known_decision_ids=storage.known_decision_ids(),
        unresolved_orders=0,
        now=utcnow(),
        allowed_tickers=allowed_tickers,
    )
    verdict = evaluate(proposal, inputs, config)
    storage.record_verdict(decision_id, verdict)

    if not verdict.approved:
        log.info("no trade [%s]: %s", verdict.rule, "; ".join(verdict.reasons) or "-")
        storage.finish_cycle(decision_id, "completed", verdict.rule)
        return "no-trade"

    log.warning(
        "risk manager APPROVED %s %s %s (~%s)%s: %s",
        verdict.side,
        abs(verdict.quantity),
        verdict.ticker,
        money(verdict.notional),
        " [shrunk]" if verdict.shrunk else "",
        "; ".join(verdict.reasons),
    )

    if dry_run:
        storage.finish_cycle(decision_id, "completed", "dry run: order not placed")
        log.warning("DRY RUN — no order was placed")
        return "dry-run"

    # 11. Execute.
    record = runtime.executor.execute(
        decision_id,
        verdict,
        day,
        lambda: load_account_state(config, storage, runtime.client, snapshot.prices()),
    )
    status = record.state if record else "not-placed"
    storage.finish_cycle(decision_id, "completed", f"order {status}")
    return f"order:{status}"


def safe_cycle(runtime: Runtime, **kwargs: Any) -> str:
    """Wrapper for the scheduler: a failed cycle must not kill the service."""
    try:
        return run_cycle(runtime, **kwargs)
    except Exception:  # noqa: BLE001 - log and live to trade another cycle
        log.exception("cycle failed with an unhandled error")
        return "error"


# Cycle outcomes that end a --until-trade run.
#
# An order that reached the market is the goal. "unknown" and "halted" end it
# too, for the opposite reason: a stranded order or a tripped breaker/kill
# switch needs a human, and every further attempt would stop at the same gate.
# Everything else — no-trade, a rejected or withdrawn order, an error, a closed
# market that may open before the deadline — is worth another attempt.
_TRADED_STATUSES = frozenset({"order:filled", "order:accepted"})
_FATAL_STATUSES = frozenset({"halted", "order:unknown"})


def run_until_trade(
    runtime: Runtime,
    *,
    dry_run: bool = False,
    force: bool = False,
    deadline_minutes: float = 60.0,
    interval_seconds: float = 60.0,
) -> tuple[bool, str]:
    """Run cycles until one trades, the deadline passes, or a human is needed.

    Returns ``(traded, last_status)``. This changes *nothing* about what is
    allowed to trade: it re-runs the ordinary cycle, and every rejection is a
    real rejection by the risk manager. A run that ends without an order means
    the bot never had a proposal it was willing to act on — that is an answer,
    not a failure of the loop.

    In ``dry_run`` an approved-but-unplaced verdict counts as the result: the
    question being asked is "would it trade", and it has been answered.
    """
    deadline = time.monotonic() + deadline_minutes * 60
    attempt = 0
    status = "not-started"

    while True:
        attempt += 1
        remaining = deadline - time.monotonic()
        log.warning(
            "attempt %d (%s, %.0f min left of the %g-minute window)",
            attempt,
            "forced" if force else "normal",
            max(remaining, 0) / 60,
            deadline_minutes,
        )
        status = safe_cycle(runtime, dry_run=dry_run, force=force, insist=True)
        log.info("attempt %d finished: %s", attempt, status)

        if status in _TRADED_STATUSES or (dry_run and status == "dry-run"):
            log.warning("attempt %d got there: %s (after %d attempt(s))", attempt, status, attempt)
            return True, status
        if status in _FATAL_STATUSES:
            log.error(
                "stopping after attempt %d: %s needs a human, retrying cannot clear it",
                attempt,
                status,
            )
            return False, status

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            log.warning(
                "gave up after %d attempt(s) over %g minutes; last result: %s",
                attempt,
                deadline_minutes,
                status,
            )
            return False, status

        wait = min(interval_seconds, remaining)
        log.info("no trade yet; next attempt in %.0fs", wait)
        time.sleep(wait)


# --------------------------------------------------------------------------- #
# Subcommands
# --------------------------------------------------------------------------- #


def cmd_status(runtime: Runtime) -> int:
    config = runtime.config
    storage = runtime.storage
    day = trading_day(config)

    print(f"t212-bot {__version__}")
    print(f"  mode:          {config.mode}"
          + ("  *** REAL MONEY ***" if config.is_live else ""))
    print(f"  base url:      {config.t212_base_url}")
    print(f"  AI provider:   {config.ai.provider}")
    print(f"  capital cap:   {money(config.capital.max_capital)} GBP")
    print(f"  watch-list:    {', '.join(i.ticker for i in config.watchlist)}")

    stopped = config.stop_file.exists()
    print(f"  kill switch:   {'ENGAGED (' + str(config.stop_file) + ')' if stopped else 'clear'}")

    breaker = storage.breaker()
    if breaker:
        print(f"  breaker:       TRIPPED on {breaker['trading_day']} — {breaker['reason']}")
    else:
        print("  breaker:       clear")

    unresolved = storage.unresolved_orders()
    if unresolved:
        print(f"  unresolved:    {len(unresolved)} order(s) — TRADING BLOCKED")
        for order in unresolved:
            print(f"     {order.decision_id} {order.state} {order.side} "
                  f"{order.quantity} {order.ticker}: {order.error or ''}")
    else:
        print("  unresolved:    none")

    row = storage.daily_row(day)
    if row:
        start = row["start_equity"]
        last = row["last_equity"]
        if start and last:
            print(f"  today:         equity {money(Decimal(last))} "
                  f"(start {money(Decimal(start))}, P&L {money(Decimal(last) - Decimal(start))})")
        print(f"  realised P&L:  {money(Decimal(row['realised_pnl']))}")
    print(f"  trades today:  {storage.trades_today(day)} of {config.risk.max_trades_per_day}")
    return 0


def cmd_list_unresolved(runtime: Runtime) -> int:
    unresolved = runtime.storage.unresolved_orders()
    if not unresolved:
        print("No unresolved orders. Trading is not blocked by reconciliation.")
        return 0
    print(f"{len(unresolved)} unresolved order(s). Trading is BLOCKED until each is resolved.")
    print("Check the Trading212 app or order history, then:")
    print("  python -m t212bot.main --resolve-order <decision_id> --note '<what you found>'")
    for order in unresolved:
        print()
        print(f"  decision_id: {order.decision_id}")
        print(f"  state:       {order.state}")
        print(f"  intent:      {order.side} {abs(order.quantity)} {order.ticker} "
              f"(~{money(order.notional)})")
        print(f"  submitted:   {order.submitted_at}")
        print(f"  broker id:   {order.broker_order_id or '-'}")
        print(f"  error:       {order.error or '-'}")
    return 1


def cmd_resolve_order(runtime: Runtime, decision_id: str, note: str) -> int:
    if runtime.storage.resolve_order(decision_id, note):
        print(f"Resolved {decision_id}: {note}")
        return 0
    print(f"No unresolved order with decision id {decision_id}.")
    return 1


def cmd_reset_breaker(runtime: Runtime) -> int:
    if runtime.storage.reset_breaker():
        print("Circuit breaker reset. Trading will resume on the next cycle.")
        return 0
    print("Circuit breaker was not tripped; nothing to reset.")
    return 0


def cmd_seed_paper(runtime: Runtime) -> int:
    config = runtime.config
    if config.mode != "paper":
        print(f"Refusing to reset the paper ledger in MODE={config.mode}.")
        return 1
    cash = runtime.storage.seed_paper_account(config.capital.max_capital, force=True)
    print(f"Paper ledger reset: {money(cash)} GBP cash, no positions.")
    return 0


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="t212bot",
        description="A small, paranoid Trading212 bot. Paper mode by default.",
    )
    parser.add_argument("--config", default=None, help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run a full cycle but never place an order (implies --once)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run even outside market hours (still honours every safety rule)",
    )
    parser.add_argument(
        "--until-trade",
        action="store_true",
        help=(
            "keep running cycles until one places an order or the window runs out "
            "(default 60 minutes). Combine with --force to ignore market hours. "
            "Exits 0 if it traded, 1 if it never did."
        ),
    )
    parser.add_argument(
        "--until-trade-minutes",
        type=float,
        default=60.0,
        metavar="MINUTES",
        help="how long --until-trade keeps trying (default: 60)",
    )
    parser.add_argument(
        "--retry-seconds",
        type=float,
        default=60.0,
        metavar="SECONDS",
        help="wait between --until-trade attempts (default: 60)",
    )
    parser.add_argument("--status", action="store_true", help="print current state and exit")
    parser.add_argument(
        "--list-unresolved", action="store_true", help="show orders needing manual reconciliation"
    )
    parser.add_argument("--resolve-order", metavar="DECISION_ID", help="mark an order reconciled")
    parser.add_argument("--note", default="resolved manually", help="note for --resolve-order")
    parser.add_argument(
        "--reset-breaker", action="store_true", help="clear the daily loss circuit breaker"
    )
    parser.add_argument(
        "--seed-paper", action="store_true", help="reset the paper ledger to max_capital"
    )
    parser.add_argument("--version", action="version", version=f"t212-bot {__version__}")
    return parser


def run_scheduler(runtime: Runtime, force: bool) -> int:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    config = runtime.config
    scheduler = BlockingScheduler(timezone=config.schedule.timezone)
    scheduler.add_job(
        safe_cycle,
        CronTrigger.from_crontab(config.schedule.cron, timezone=config.schedule.timezone),
        kwargs={"runtime": runtime, "force": force},
        id="cycle",
        max_instances=1,       # never let two cycles overlap
        coalesce=True,         # a backlog after a pause collapses to one run
        misfire_grace_time=300,
    )

    def shutdown(signum: int, _frame: Any) -> None:
        log.warning("signal %s received, shutting down", signum)
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    log.warning(
        "scheduler started: '%s' %s (mode=%s)%s",
        config.schedule.cron,
        config.schedule.timezone,
        config.mode,
        "  *** REAL MONEY ***" if config.is_live else "",
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):  # pragma: no cover
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load(args.config)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(config)
    runtime = build_runtime(config)

    try:
        if args.status:
            return cmd_status(runtime)
        if args.list_unresolved:
            return cmd_list_unresolved(runtime)
        if args.resolve_order:
            return cmd_resolve_order(runtime, args.resolve_order, args.note)
        if args.reset_breaker:
            return cmd_reset_breaker(runtime)
        if args.seed_paper:
            return cmd_seed_paper(runtime)

        if config.is_live:
            log.critical(
                "RUNNING IN LIVE MODE — orders placed will use real money (cap %s GBP)",
                money(config.capital.max_capital),
            )

        # A process that died mid-order left rows behind. They are marked
        # unknown, never retried, and they block trading until reconciled.
        stranded = runtime.storage.mark_stranded_orders_unknown(
            "process restarted while the order was in flight"
        )
        if stranded:
            log.critical(
                "%d order(s) were in flight when a previous process stopped. "
                "Trading is blocked; run --list-unresolved.",
                stranded,
            )

        if args.until_trade:
            if args.until_trade_minutes <= 0:
                print("--until-trade-minutes must be positive", file=sys.stderr)
                return 2
            if args.retry_seconds < 0:
                print("--retry-seconds must not be negative", file=sys.stderr)
                return 2
            traded, status = run_until_trade(
                runtime,
                dry_run=args.dry_run,
                force=args.force,
                deadline_minutes=args.until_trade_minutes,
                interval_seconds=args.retry_seconds,
            )
            log.info("until-trade finished: %s", status)
            return 0 if traded else 1

        if args.dry_run or args.once:
            status = safe_cycle(runtime, dry_run=args.dry_run, force=args.force)
            log.info("cycle finished: %s", status)
            return 0
        return run_scheduler(runtime, args.force)
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
