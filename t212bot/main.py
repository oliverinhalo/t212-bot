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
import logging
import logging.handlers
import signal
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from . import __version__
from .ai_advisor import advise, build_provider as build_ai_provider
from .config import AppConfig, ConfigError, load
from .executor import Executor
from .market_data import Snapshot, build_provider as build_market_provider
from .models import RiskInputs, money, summarise_positions, utcnow
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

    def close(self) -> None:
        for target in (self.market, self.ai, self.client, self.storage):
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

    return Runtime(
        config=config,
        storage=storage,
        market=build_market_provider(config),
        ai=build_ai_provider(config),
        client=client,
        executor=Executor(config, storage, client),
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
# The cycle
# --------------------------------------------------------------------------- #


def run_cycle(runtime: Runtime, *, dry_run: bool = False, force: bool = False) -> str:
    """Run one full decision cycle. Returns a short status string."""
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
    snapshot: Snapshot = runtime.market.fetch(config.watchlist, config.ai.history_days)
    if not snapshot.quotes:
        return halt(f"no market data for any watch-list ticker: {snapshot.errors}")
    for ticker, error in snapshot.errors.items():
        log.warning("no quote for %s: %s", ticker, error)

    # 7. Account state.
    try:
        account = load_account_state(config, storage, runtime.client, snapshot.prices())
    except (T212Error, ValueError) as exc:
        return halt(f"could not load account state: {exc}", status="error")

    # 8. P&L and the circuit breaker.
    day_start_equity = storage.record_equity(day, account.equity)
    tripped, pnl, limit = circuit_breaker_state(day_start_equity, account.equity, config)
    trades_today = storage.trades_today(day)

    storage.record_snapshot(
        decision_id,
        {
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
                }
                for ticker, quote in snapshot.quotes.items()
            },
            "quote_errors": snapshot.errors,
        },
    )

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

    # 9. Ask the AI.
    result = advise(
        runtime.ai,
        config,
        account,
        snapshot,
        trades_today=trades_today,
        day_pnl=pnl,
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

    # 10. Risk manager. The only thing that can authorise an order.
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
    )
    verdict = evaluate(result.proposal, inputs, config)
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

        if args.dry_run or args.once:
            status = safe_cycle(runtime, dry_run=args.dry_run, force=args.force)
            log.info("cycle finished: %s", status)
            return 0
        return run_scheduler(runtime, args.force)
    finally:
        runtime.close()


if __name__ == "__main__":
    raise SystemExit(main())
