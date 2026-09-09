# t212-bot

A small, deliberately paranoid trading bot for a Raspberry Pi. On a schedule it
checks a short watch-list, asks an LLM for a buy/hold/sell recommendation, and —
only if a strict local risk manager approves — places an order through the
Trading212 API.

Total capital under management: **£50**. This is a hobby project. Correctness,
safety rails and a clear audit trail matter more than trading sophistication.

**It runs in paper mode by default and cannot place a real order until you set
two separate environment variables.**

---

## Contents

- [How it works](#how-it-works)
- [The safety rules](#the-safety-rules)
- [Quick start](#quick-start)
- [Modes: paper, demo, live](#modes-paper-demo-live)
- [The kill switch](#the-kill-switch)
- [The circuit breaker](#the-circuit-breaker)
- [Unresolved orders](#unresolved-orders)
- [Configuration](#configuration)
- [Making it actually trade](#making-it-actually-trade)
- [The audit log](#the-audit-log)
- [The dashboard](#the-dashboard)
- [Running as a service](#running-as-a-service)
- [Tests](#tests)
- [Going live](#going-live)
- [Assumptions and caveats](#assumptions-and-caveats)

---

## How it works

One cycle, in order. Every gate that can stop the cycle runs *before* the AI
call, so a halted bot spends no API quota.

```
kill switch → unresolved orders → circuit breaker → market hours
  → market data → technical signals → account state → P&L and breaker check
  → AI proposal (model, or local strategy, or cached hold)
  → resolve + price the named instrument (open-universe) → risk manager
  → execute → audit log
```

| Module | Responsibility |
|---|---|
| `t212bot/config.py` | Loads `config.yaml` + `.env`, validates every cap, resolves the mode |
| `t212bot/t212_client.py` | Rate-limit-aware Trading212 wrapper. The only HTTP client for the broker |
| `t212bot/market_data.py` | Quotes and daily closes (Yahoo Finance — T212 has no quote endpoint); Yahoo symbol resolution |
| `t212bot/instruments.py` | The Trading212 instrument catalogue — resolves any name the AI gives to a real ticker |
| `t212bot/fx.py` | Converts a foreign-currency quote to GBP (open-universe mode) |
| `t212bot/indicators.py` | Local technical analysis over the daily closes (SMA/RSI/vol/momentum, trend, regime) |
| `t212bot/ai_advisor.py` | Builds the prompt, calls the provider (model fallback chain), parses JSON defensively |
| `t212bot/local_strategy.py` | Deterministic rule-based advisor: the fallback when the LLM is unavailable or the budget is spent |
| **`t212bot/risk_manager.py`** | **Pure functions. The only module that can authorise an order** |
| `t212bot/executor.py` | Places the order. The only module that calls an order endpoint |
| `t212bot/storage.py` | SQLite audit trail, paper ledger, duplicate-order guard |
| `t212bot/portfolio.py` | Assembles account state from the ledger or the broker |
| `t212bot/main.py` | The cycle, the scheduler and the CLI |
| `t212bot/dashboard.py` | Read-only status page |

The AI's output is a *proposal*, never an instruction. The risk manager can
**approve, shrink, or reject** it — there is no path that produces an order
larger than the one proposed, and `_assert_not_enlarged` raises rather than
returning one if the sizing logic is ever wrong.

---

## The safety rules

All enforced in code, not in the prompt. Each has a rule code that appears in
the audit log, and each is asserted in `tests/test_risk_manager.py`.

| # | Rule | Code | Where |
|---|---|---|---|
| 1 | **Paper mode by default.** `MODE=live` also requires `I_UNDERSTAND_THIS_IS_REAL_MONEY=yes` or the app refuses to start | — | `config.resolve_mode` |
| 2 | **Hard capital cap.** Deployed position value after a buy must stay under `max_capital_gbp` | `R14_CAPITAL_CAP` | `risk_manager._size_buy` |
| 3 | **Per-trade cap.** No single order over `per_trade_cap_pct` of capital (default 25% = £12.50). A separate `max_position_pct` caps the accumulated holding per ticker | `R15_POSITION_CAP` | `risk_manager._size_buy` |
| 4 | **Daily loss circuit breaker.** At −`daily_loss_limit_pct` of capital (default −10% = −£5), trading halts and stays halted until `--reset-breaker` | `R01_BREAKER` | `risk_manager.circuit_breaker_state` |
| 5 | **Trade-frequency cap.** `max_trades_per_day` (default 5), counting attempts, not fills | `R10_FREQUENCY` | `risk_manager.evaluate` |
| 6 | **Ticker allow-list.** With `risk.enforce_allowlist: true` (default) the AI can only act on watch-list tickers. With it `false` (open-universe) the AI may name any instrument, but it must resolve against the Trading212 catalogue — a hallucinated ticker is still rejected before the broker | `R05_ALLOWLIST` | `risk_manager.evaluate` |
| 7 | **No leverage, no shorting, no options.** Structurally: the client has no method that could express one, and a sell is clamped to the quantity actually held | `R18_NO_POSITION` | `t212_client`, `risk_manager._size_sell` |
| 8 | **Every proposal is sanity-checked.** Unknown ticker, missing/stale/zero quote, implied price far from the market, low confidence, or any cap breach → rejected or shrunk | `R05`–`R19` | `risk_manager.evaluate` |
| 9 | **Duplicate-order guard.** One decision id claims at most one order row, written to SQLite *before* the HTTP call. Survives retries and crashes | `R03_DUPLICATE` | `storage.reserve_order` |
| 10 | **Kill switch.** A `STOP` file halts every cycle cleanly | — | `main.run_cycle` |
| 11 | **Full audit log.** Snapshot, prompt, raw response, verdict, reasoning, order and fill — all in SQLite | — | `storage.py` |
| 12 | **Secrets in `.env` only.** Never logged, never stored, `Secrets.__repr__` is redacted | — | `config.Secrets` |
| 13 | **Rate limits respected** with per-endpoint token buckets, `x-ratelimit-*` header awareness, and backoff | — | `t212_client.RateLimiter` |

Two more that fall out of the design:

- **Order endpoints are never retried.** T212's order endpoints are not
  idempotent in this beta — a retried request can double-place. On a timeout the
  order is recorded as `unknown` and *all* trading stops until you reconcile it.
- **The balance is re-checked immediately before submission**, and the risk
  manager runs again against it. That re-check can only shrink or reject.

---

## Quick start

Requires Python 3.11+.

```bash
git clone <your-repo> t212-bot && cd t212-bot
./deploy/install.sh              # venv + deps + seeds config.yaml and .env
```

Then, in order:

```bash
# 1. Secrets. Leave MODE=paper.
nano .env

# 2. Find the exact T212 tickers for your watch-list (they are not plain symbols)
.venv/bin/python -m scripts.list_instruments vusa

# 3. Edit the watch-list and the caps
nano config.yaml

# 4. Confirm the credentials work (read-only, hits demo)
.venv/bin/python -m scripts.check_auth

# 5. A full cycle that places nothing
.venv/bin/python -m t212bot.main --dry-run --force

# 6. A real paper cycle
.venv/bin/python -m t212bot.main --once --force

# 7. Look at what it did
.venv/bin/python -m t212bot.main --status
```

Leave it in paper mode for a few days and read the audit log before going
further.

### CLI

```
--once              run one cycle and exit
--dry-run           run a full cycle but never place an order
--force             run even outside market hours (all other rules still apply)
--preorder          out of hours, rest a limit order that waits for the open
                    instead of a market order the broker refuses. Implies
                    --force; no effect while the market is open
--until-trade       keep running cycles until one places an order, or the
                    window runs out (default 60 min). Exit 0 if it traded,
                    1 if it never did.
--until-trade-minutes N   how long to keep trying (default 60)
--retry-seconds N         wait between attempts (default 60)
--status            current mode, caps, breaker, unresolved orders, today's P&L
--list-unresolved   orders needing manual reconciliation
--resolve-order ID  mark one reconciled (with --note "what you found")
--reset-breaker     clear the daily loss circuit breaker
--seed-paper        reset the paper ledger to max_capital (paper mode only)
```

With no flags it starts the scheduler and runs on the configured cron.

### Keep trying until it trades

```bash
# One attempt, right now, market hours ignored
.venv/bin/python -m t212bot.main --once --force

# Keep attempting for up to an hour, one attempt a minute
.venv/bin/python -m t212bot.main --until-trade --force

# Same, but 30 minutes at 15-second intervals
.venv/bin/python -m t212bot.main --until-trade --force \
    --until-trade-minutes 30 --retry-seconds 15

# "Would it trade?" — approves and stops, places nothing
.venv/bin/python -m t212bot.main --until-trade --force --dry-run
```

`--until-trade` re-runs the ordinary cycle. It grants no extra permission:
every attempt goes through the same risk manager, and a run that ends without
an order means the bot never had a proposal it was willing to act on. It stops
early — without waiting out the window — on anything a retry cannot clear: the
kill switch, a tripped breaker, or an unresolved order. Because retries happen
precisely when nothing has changed, they bypass `ai.skip_when_unchanged` and
ask the model every attempt.

If it runs the full window and never trades, the audit log says why: check
`--status` and the `rule` column (`R04_HOLD` means the model kept saying hold;
`R11_CONFIDENCE`, `R16_BELOW_MIN` and friends mean it proposed something the
caps refused).

### Buying out of hours (pre-orders)

`--force` will run a cycle at 8pm, but the order it then places is a **market**
order, and a broker will not accept one for a closed market — the cycle ends in
a rejection. `--preorder` places a **limit order that rests until the market
opens** instead:

```bash
# Decide now, rest the order until the open
.venv/bin/python -m t212bot.main --once --preorder

# Keep trying to get one queued, for up to an hour
.venv/bin/python -m t212bot.main --until-trade --preorder
```

```
market closed (20:23 is outside 08:05-16:25 Europe/London)
  — an approved order will be pre-ordered as a GOOD_TILL_CANCEL limit order
DEMO PRE-ORDER: BUY 0.056100 VUSAl_EQ (~6.00) limit 107.22 GOOD_TILL_CANCEL
order accepted by broker as 991122 (status=PENDING)
```

`--preorder` implies `--force` and does nothing while the market is open —
there is nothing to wait for, so an ordinary order is placed. To make it the
standing behaviour, set `execution.preorder_when_closed: true`;
`execution.preorder_time_validity` chooses `GOOD_TILL_CANCEL` (rests into the
next session) or `DAY`.

The limit price is `execution.limit_offset_bps` away from the last quote, in
the direction that helps it fill — above the market for a buy, below for a
sell. That is also the safety property: a market order into an opening gap has
no ceiling, a limit order does. If the open gaps past your limit the order
simply does not fill, which is the outcome you want.

Two things worth knowing:

- **The decision is made on a closed-market price.** The order executes at the
  open, hours later, on whatever the market does overnight. The limit caps the
  damage; it does not make the decision fresher.
- **It is a real resting order at the broker.** It is not held locally and not
  re-checked before it fills. Cancel it in the Trading212 app if you change
  your mind — the bot's kill switch stops new cycles, not an order already
  queued.

---

## Modes: paper, demo, live

Set with `MODE` in `.env`.

| Mode | Order endpoints | Money | Positions tracked in | Extra requirement |
|---|---|---|---|---|
| **`paper`** (default) | **never called** | none | SQLite virtual ledger | — |
| `demo` | called against `demo.trading212.com` | none (practice account) | your T212 demo account | `T212_API_KEY` |
| `live` | called against `live.trading212.com` | **real** | your real account | `I_UNDERSTAND_THIS_IS_REAL_MONEY=yes` |

Paper mode reads **real quotes** and simulates fills against them, with
configurable slippage that always works against you. It starts with
`max_capital_gbp` of virtual cash and never calls an order endpoint — not on any
code path, which is asserted by a test that hands the executor a client whose
order methods raise.

The base URL is derived from the mode and nothing else, so there is no way to
hit live while believing you are on demo.

---

## The kill switch

```bash
touch STOP      # every cycle halts cleanly from now on
rm STOP         # resume
```

Checked first in every cycle, before any network call. The service keeps
running, so this is safe to use while you investigate something. `systemctl stop
t212-bot` also works; the scheduler handles `SIGTERM` between cycles.

---

## The circuit breaker

If total P&L for the day (realised **and** unrealised) falls to
−`daily_loss_limit_pct` of `max_capital_gbp`, the bot writes a breaker record to
SQLite and halts. It blocks **sells too** — an unattended bot that has already
hit its loss limit should stop acting, not start improvising. Close positions by
hand in the Trading212 app if you need to.

Because the breaker lives in the database, restarting the process (or a systemd
auto-restart) does **not** clear it. That is the point. Clear it deliberately:

```bash
.venv/bin/python -m t212bot.main --reset-breaker
```

The day's baseline equity is recorded on the first cycle of each day and never
overwritten.

---

## Unresolved orders

If an order request times out or fails at the transport layer, the bot does not
know whether it reached the broker. It records the order as `unknown`, **never
retries it**, and refuses to trade at all until you reconcile it.

```bash
.venv/bin/python -m t212bot.main --list-unresolved
# ... check the Trading212 app / order history ...
.venv/bin/python -m t212bot.main --resolve-order a1b2c3... --note "no order was placed"
```

The same applies to a process that dies mid-order: on the next start, any row
left in `reserved`/`submitting` becomes `unknown`.

---

## Configuration

`config.yaml` holds behaviour and limits; `.env` holds secrets and `MODE`. Both
have a documented `.example` alongside them. The loader is strict — a malformed
or impossible cap is a startup error, not a silent default.

The watch-list is the allow-list (unless open-universe mode is on, below):

```yaml
watchlist:
  - ticker: VUSAl_EQ          # EXACT Trading212 ticker — find it with list_instruments
    yahoo: VUSA.L             # Yahoo Finance symbol, for quotes and history
    name: Vanguard S&P 500 UCITS ETF
    max_position_pct: 20.0    # optional per-ticker override
```

### Open-universe mode

`risk.enforce_allowlist: false` lets the AI name **any** Trading212 instrument,
not just the watch-list. One-time setup:

```bash
python -m scripts.list_instruments --refresh   # caches data/instruments.json
```

Per cycle, when the AI names something off the watch-list:

1. it is resolved against `data/instruments.json` (by ticker, symbol, ISIN, or
   unique name) — **an unknown or ambiguous name is rejected** (`R05`);
2. a Yahoo symbol is found (override → `data/symbol_map.json` cache → Yahoo ISIN
   search → derived from the exchange suffix), with a currency cross-check;
3. the quote is fetched and **converted to GBP** via a live FX rate, so every
   cap still binds correctly;
4. only then does it enter the unchanged risk manager.

The watch-list still matters: it is the set with full technical signals, and the
local fallback strategy only operates on it. Pin a wrong symbol lookup with
`market_data.symbol_overrides`. **Caveat:** symbol resolution for obscure or
dual-listed instruments can pick the wrong listing — check `data/symbol_map.json`
and the `--status` prices. Broker-mode position values for foreign holdings are
converted using the same FX layer.

Key caps, with their defaults for £50 of capital:

| Setting | Default | Effect |
|---|---|---|
| `capital.max_capital_gbp` | 50.00 | Total deployed value ceiling |
| `capital.per_trade_cap_pct` | 25% | £12.50 per order |
| `capital.max_position_pct` | 25% | £12.50 per ticker, accumulated |
| `capital.min_order_gbp` | 1.00 | Smaller orders are rejected, not shrunk to dust |
| `capital.cash_buffer_gbp` | 0.50 | Never spent |
| `risk.daily_loss_limit_pct` | 10% | Breaker at −£5 |
| `risk.max_trades_per_day` | 5 | Attempts, not fills |
| `risk.max_price_deviation_pct` | 2% | AI's implied price vs the quote |
| `risk.max_quote_age_seconds` | 900 | How long ago *we* fetched the quote; 0 disables |
| `risk.max_quote_delay_seconds` | 0 (off) | How far behind the exchange the feed's own timestamp may be |
| `risk.min_confidence` | 0.60 | Below this, treated as hold |
| `execution.quantity_decimals` | 6 | Quantities always round **down** |

### AI provider

`ai.provider: auto` picks Anthropic if `ANTHROPIC_API_KEY` is set, else
OpenRouter if `OPENROUTER_API_KEY` is set, else OmniRoute if
`OMNIROUTE_API_KEY` is set. Pin it with `openrouter`, `omniroute`,
`anthropic`, or `stub`.

- **OpenRouter** (default, free): `ai.openrouter.models` is a fallback chain,
  tried in order until one answers, so a single free model being rate-limited
  or retired does not lose the cycle. Put `openrouter/free` (the auto-router)
  last. Free tier is roughly 20 requests/minute and 50/day.
- **OmniRoute**: a self-hosted/private OpenAI-compatible router — same wire
  format as OpenRouter (model fallback chain, JSON-mode retry), just pointed
  at your own instance. Set `ai.omniroute.base_url` (or override per-environment
  with `OMNIROUTE_BASE_URL` in `.env`, e.g. to switch between a local instance
  and a remote one) and `ai.omniroute.models`/`.model`; the key comes from
  `OMNIROUTE_API_KEY`. Not subject to the OpenRouter free-tier budget below.
- **Anthropic**: `claude-haiku-4-5` by default. Needs `pip install
  "anthropic>=1.0,<2"`.
- **`stub`**: always returns `hold`. Exercises the whole pipeline with no API
  calls and no key.

**Staying inside the free tier.** Every OpenRouter HTTP call is counted per UTC
day in SQLite (`ai_usage`). Three things keep the bot under the cap:

| Mechanism | Config | Effect |
|---|---|---|
| Daily budget | `ai.daily_request_budget` (45) | **OpenRouter only.** Past this, the **local strategy** runs instead of the model for the rest of the day. `0` means no limit. OmniRoute (self-hosted, unmetered) and Anthropic (pay-as-you-go) are never metered by it |
| Cache skip | `ai.skip_when_unchanged` (true) | If nothing moved more than `ai.min_price_move_pct` since a cycle that held, the model is not called at all |
| Local fallback | `ai.local_fallback` (true) | A model failure or refusal falls back to the local strategy, not a blind `hold` |

**Local technical signals** (`ai.indicators`, no API cost) are computed from the
Yahoo daily closes — SMA/RSI/annualised vol/momentum/drawdown, a per-ticker
trend label and score, and a coarse market regime — and fed into the prompt as
a `TECHNICAL SIGNALS` block.

**The local strategy** (`ai.local_strategy`) is a deterministic, rule-based
advisor: stop-loss → trend exit → take-profit → one cautious entry → hold. It
emits the same JSON contract as the model and its proposal goes through the
**same risk manager** — it is never an order authoriser.

A provider failure with local fallback disabled still degrades to `hold`, with
the error recorded. Nothing here ever crashes the cycle or trades on a guess.

---

## Making it actually trade

A bot that holds every cycle is usually not broken — some gate is refusing, and
the audit log names which one. Diagnose before loosening:

```bash
# What did it decide, and which rule stopped it?
.venv/bin/python -m t212bot.main --status
sqlite3 data/t212bot.sqlite3 \
  "SELECT created_at, ai_action, ai_ticker, rule, reason FROM decisions ORDER BY id DESC LIMIT 20;"
```

Then loosen the knob that matches the rule you actually see. Each row costs you
the protection in the last column — that is the trade, not a free win.

| Symptom (`rule`) | Config change | What it costs |
|---|---|---|
| `R04_HOLD` every cycle | `ai.skip_when_unchanged: false`, and check `ai.provider` is really answering (`--once --force` and read the log) | More API calls |
| `R11_CONFIDENCE` | Lower `risk.min_confidence` (e.g. `0.10`) | Acts on weaker convictions |
| `R16_BELOW_MIN` | Lower `capital.min_order_gbp` (e.g. `1.00`) | More dust-sized orders, fees bite harder |
| `R09_PRICE_DEVIATION` | Raise `risk.max_price_deviation_pct` (e.g. `5.0`) | Weaker guard against a hallucinated or stale price |
| `R15_POSITION_CAP` | Raise `capital.max_position_pct` | Less diversification, more single-ticker exposure |
| `R14_CAPITAL_CAP` | Raise `capital.max_capital_gbp` | More money at risk. The one knob that changes your real downside |
| `R10_FREQUENCY` | Raise `risk.max_trades_per_day` | More trading, more fees |
| `R13_NO_CASH` | Lower `capital.cash_buffer_gbp` | A slipped fill can overdraw |
| `R05_ALLOWLIST` / nothing to buy | `risk.enforce_allowlist: false` (run `python -m scripts.list_instruments --refresh` first) | The AI may name anything on Trading212, including things you have never looked at |
| `skipped: market closed` | Widen `schedule.market_open`/`market_close`/`trading_days`, or pass `--force` | Trading on a delayed or closed-market price |
| Cycles too rare | `schedule.cron` (e.g. `*/15 8-16 * * mon-fri`) | More API calls, more chances to trade |

Sizing is bracketed by `capital.min_order_gbp` and `per_trade_cap_pct` × 
`max_capital_gbp`. If the minimum is above the per-trade cap the config is
rejected at load; if they are close, most proposals land outside the band and
get refused, which reads as "it never trades". Widening that band is usually
the single most effective change.

Do this in paper mode first, then demo, and read the audit log before letting
any of it near `MODE=live`.

---

## The audit log

Everything lands in `data/t212bot.sqlite3`:

| Table | Holds |
|---|---|
| `cycles` | Start/finish, status, halt reason, and the full market + account snapshot as JSON |
| `ai_decisions` | Provider, model, **the full prompt**, **the raw response**, parsed fields, latency |
| `risk_verdicts` | Approved/rejected, rule code, reasoning, final quantity — both the primary check and the pre-submit re-check |
| `orders` | One row per decision id, its state, broker id, fill price and quantity |
| `daily` | Start equity, last equity, realised P&L per day |
| `breaker` | Whether the circuit breaker is tripped, and why |
| `ai_usage` | OpenRouter HTTP calls made per UTC day, for the free-tier budget |
| `ai_cycle_state` | Fingerprint + action of the last cycle, for the cache skip |

Money is stored as TEXT and handled as `Decimal` throughout. No API key ever
reaches this database; there is a test that greps the whole thing for
credential-shaped strings.

```bash
sqlite3 data/t212bot.sqlite3 \
  "SELECT started_at, status, halt_reason FROM cycles ORDER BY started_at DESC LIMIT 10;"
```

---

## The dashboard

```bash
.venv/bin/python -m t212bot.dashboard      # http://<pi>:8080/
```

Balance, open positions, today's P&L, the last 10 decisions, and prominent
banners for the kill switch, the breaker and unresolved orders. It reads only
the database — never the Trading212 API — so opening it costs no rate-limit
budget and cannot interfere with a cycle.

There is **no authentication**. Bind it to your LAN (`dashboard.host`) and do
not expose it to the internet.

---

## Running as a service

`./deploy/install.sh` (as root) installs two hardened systemd units:

```bash
sudo systemctl enable --now t212-bot
sudo systemctl enable --now t212-bot-dashboard
journalctl -u t212-bot -f
```

`Restart=on-failure` with `RestartSec=60`. Note that a restart does not clear
the circuit breaker or an unresolved order — both live in SQLite and both
require a deliberate command.

---

## Tests

```bash
.venv/bin/python -m pytest
```

224 tests, ~0.5s, no network. `tests/test_risk_manager.py` is the real
specification: if a rule above is not asserted there, treat it as
unimplemented. It covers zero cash, every cap binding in turn, the breaker at
and either side of its threshold, the duplicate-order guard across a simulated
restart, no-shorting, dust exits, whole-share rounding, and a parameter sweep
asserting that an approved buy can never exceed cash or any cap.

---

## Going live

Do not do this until you have run several days of paper cycles and read the
audit log. Then, in order:

1. **Test execution on demo first.** Set `MODE=demo` with a demo API key. This
   uses the real order endpoints against a practice account, so it exercises
   everything live mode does, with fake money. Run for at least a day.
2. **Generate a separate live API key** in the Trading212 app (Settings → API).
   Never reuse the demo key.
3. **Shrink the first trade.** In `config.yaml`:
   ```yaml
   capital:
     max_capital_gbp: 50.00
     per_trade_cap_pct: 2.0      # £1.00 — the smallest useful order
     min_order_gbp: 1.00
   risk:
     max_trades_per_day: 1
   ```
4. **Set both variables** in `.env`:
   ```bash
   MODE=live
   I_UNDERSTAND_THIS_IS_REAL_MONEY=yes
   T212_API_KEY=<your live key>
   T212_API_SECRET=<your live secret>
   ```
   Missing or misspelling the second variable is a startup failure, by design.
5. **Run one cycle by hand and watch it**, during market hours:
   ```bash
   .venv/bin/python -m t212bot.main --once
   ```
6. **Verify the fill in the Trading212 app**, and check the order row:
   ```bash
   sqlite3 data/t212bot.sqlite3 "SELECT * FROM orders ORDER BY created_at DESC LIMIT 1;"
   ```
7. Only then raise the caps and start the service. Keep `touch STOP` in your
   shell history.

---

## Assumptions and caveats

Worth knowing before you trust this with money.

- **Auth scheme.** This is built to the documented HTTP Basic scheme
  (`base64(API_KEY:API_SECRET)`). Some Trading212 keys are single tokens sent as
  a bare `Authorization` header instead. `T212_AUTH_SCHEME=basic|header` switches
  between them without a code change; `scripts/check_auth.py` tells you which
  one works. **Run it against demo before anything else.**
- **Quotes do not come from Trading212.** The T212 public API has no quote
  endpoint, so prices come from Yahoo Finance via the `yahoo` symbol in each
  watch-list entry. That mapping is yours to get right — a wrong symbol means
  the bot prices the wrong instrument. Cross-check the price in
  `--status` against the app before trusting it.
- **Pence vs pounds.** London-listed ETFs quote in pence (`GBp`). The market
  data layer converts to GBP explicitly; getting this wrong would be a
  factor-of-100 error in every cap.
- **Quote freshness is a trading gate, but feed delay is not.** These are two
  different clocks and conflating them used to block every order.
  `max_quote_age_seconds` measures how long ago *the bot fetched* the quote —
  it catches our own market data stalling (`R07_QUOTE_STALE`).
  `max_quote_delay_seconds` measures how far behind the exchange the feed's own
  timestamp is; Yahoo is delayed ~15 minutes, so a quote pulled a second ago is
  permanently ~900s "old" by that measure. It is off (`0`) by default — set it
  to `900` to refuse to trade on a delayed feed (`R19_QUOTE_DELAYED`). Either
  limit is disabled with `0`. Trading outside market hours is gated separately,
  by `schedule.market_open`/`market_close`.
- **Endpoint paths** follow the documented `/api/v0/equity/...` shapes. Verify
  against demo before live; `check_auth` exercises the account and portfolio
  endpoints, and a `--once` demo cycle exercises the order path.
- **GBP is assumed everywhere.** The API only executes in the account's primary
  currency and does not support multi-currency accounts. `check_auth` warns if
  your account currency is not GBP.
- **Fractional shares** are assumed available (`execution.fractional: true`).
  Set it to `false` if your instruments require whole shares — with £12.50 per
  trade, that will reject most orders, which is the safe failure.
- **Pies endpoints are deprecated** and deliberately not implemented.
- **This is not investment advice, and an LLM is not an analyst.** The risk
  manager exists because the model will, at some point, propose something
  stupid. £50 is the real safety mechanism.
