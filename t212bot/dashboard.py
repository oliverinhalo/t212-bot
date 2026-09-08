"""A one-page status view, for checking the bot from a phone on the home network.

Mostly read-only and database-only: it renders the last snapshot the trading
cycle recorded rather than calling Trading212 itself. That keeps the dashboard
off the account's rate-limit budget.

The one write action is POST /api/force-trade, which triggers a single trading
cycle with ``force=True`` (market-hours gate bypassed, every other safety rule
still enforced). The cycle runs in a background thread so the HTTP request
returns quickly; poll /api/force-trade/status for the result.

    python -m t212bot.dashboard          # http://<pi>:8080/

Bind it to the LAN only (``dashboard.host``) — there is no authentication here.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request

from .config import AppConfig, load
from .models import ZERO, dec, money
from .main import build_runtime, safe_cycle, trading_day
from .storage import Storage

log = logging.getLogger(__name__)

PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>t212-bot</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1rem;
           max-width: 46rem; margin-inline: auto; line-height: 1.45; }
    h1 { font-size: 1.25rem; margin: 0 0 .25rem; }
    h2 { font-size: .95rem; text-transform: uppercase; letter-spacing: .05em;
         opacity: .6; margin: 1.5rem 0 .5rem; }
    .banner { padding: .6rem .8rem; border-radius: .5rem; margin: .5rem 0 1rem;
              font-weight: 600; }
    .live { background: #b3261e; color: #fff; }
    .paper { background: #e7f0e7; color: #14431b; }
    .alert { background: #fde7cf; color: #6a3a00; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
            gap: .5rem; }
    .tile { border: 1px solid rgba(128,128,128,.3); border-radius: .5rem; padding: .6rem .7rem; }
    .tile .label { font-size: .72rem; text-transform: uppercase; opacity: .6; }
    .tile .value { font-size: 1.2rem; font-variant-numeric: tabular-nums; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th, td { text-align: left; padding: .35rem .4rem; border-bottom: 1px solid rgba(128,128,128,.2);
             vertical-align: top; }
    th { font-weight: 600; opacity: .7; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .pos { color: #14431b; } .neg { color: #b3261e; }
    .pill { display: inline-block; padding: .05rem .4rem; border-radius: 1rem;
            font-size: .72rem; border: 1px solid rgba(128,128,128,.4); }
    .muted { opacity: .6; font-size: .8rem; }
    .force-section { margin: 1.5rem 0; padding: .8rem; border: 2px dashed rgba(128,128,128,.3);
                     border-radius: .5rem; }
    .force-btn { background: #1a73e8; color: #fff; border: none; border-radius: .4rem;
                 padding: .5rem 1.2rem; cursor: pointer; font-size: .9rem; font-weight: 600; }
    .force-btn:hover { background: #1557b0; }
    .force-btn:disabled { opacity: .5; cursor: not-allowed; }
    .force-result { margin-top: .6rem; padding: .5rem .6rem; border-radius: .4rem;
                    font-size: .85rem; }
    .force-result.ok { background: #e7f0e7; color: #14431b; }
    .force-result.err { background: #fde7cf; color: #6a3a00; }
    .force-result.running { background: #e8eaf6; color: #1a237e; }
  </style>
</head>
<body>
  <h1>t212-bot</h1>
  <div class="muted">{{ generated }} &middot; {{ config.ai.provider }} &middot;
      cap &pound;{{ cap }}
      {% if regime %} &middot; regime {{ regime.replace('_', ' ') }}{% endif %}
      &middot; AI calls {{ api_calls_today }}/{{ api_budget }} today</div>

  <div class="banner {{ 'live' if config.mode == 'live' else 'paper' }}">
    MODE: {{ config.mode|upper }}{% if config.mode == 'live' %} — REAL MONEY{% endif %}
  </div>

  {% if stop_engaged %}
    <div class="banner alert">KILL SWITCH ENGAGED — {{ config.stop_file }} exists.
      No cycle will trade until it is deleted.</div>
  {% endif %}
  {% if breaker %}
    <div class="banner alert">CIRCUIT BREAKER TRIPPED on {{ breaker['trading_day'] }}:
      {{ breaker['reason'] }} — reset with <code>--reset-breaker</code>.</div>
  {% endif %}
  {% if unresolved %}
    <div class="banner alert">{{ unresolved|length }} UNRESOLVED ORDER(S) — trading is blocked.
      Run <code>--list-unresolved</code>.</div>
  {% endif %}

  <div class="grid">
    <div class="tile"><div class="label">Equity</div>
      <div class="value">&pound;{{ equity }}</div></div>
    <div class="tile"><div class="label">Cash</div>
      <div class="value">&pound;{{ cash }}</div></div>
    <div class="tile"><div class="label">Invested</div>
      <div class="value">&pound;{{ invested }}</div></div>
    <div class="tile"><div class="label">P&amp;L today</div>
      <div class="value {{ 'pos' if pnl_positive else 'neg' }}">&pound;{{ pnl }}</div></div>
    <div class="tile"><div class="label">Trades today</div>
      <div class="value">{{ trades_today }}/{{ config.risk.max_trades_per_day }}</div></div>
  </div>

  <div class="force-section">
    <h2 style="margin-top:0">Force trade</h2>
    <p class="muted" style="margin:.2rem 0 .6rem">
      Run one full AI cycle now, even outside market hours.
      All safety rules still apply — only the hours gate is bypassed.</p>
    <button class="force-btn" id="force-btn" onclick="forceTrade()">
      ⚡ Force trade cycle
    </button>
    <div id="force-result"></div>
  </div>

  <h2>Open positions</h2>
  {% if positions %}
  <table>
    <tr><th>Ticker</th><th class="num">Qty</th><th class="num">Avg</th>
        <th class="num">Now</th><th class="num">Value</th><th class="num">P&amp;L</th>
        <th>Trend</th></tr>
    {% for p in positions %}
    <tr>
      <td>{{ p.ticker }}</td>
      <td class="num">{{ p.quantity }}</td>
      <td class="num">{{ p.average_price }}</td>
      <td class="num">{{ p.current_price }}</td>
      <td class="num">{{ p.value }}</td>
      <td class="num {{ 'pos' if not p.unrealised_pnl.startswith('-') else 'neg' }}">
        {{ p.unrealised_pnl }}</td>
      <td class="muted">{{ p.trend or '—' }}</td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p class="muted">No open positions.</p>{% endif %}

  <h2>Last {{ decisions|length }} decisions</h2>
  {% if decisions %}
  <table>
    <tr><th>When</th><th>AI</th><th>Outcome</th><th>Why</th></tr>
    {% for d in decisions %}
    <tr>
      <td class="muted">{{ d.started_at[:16].replace('T', ' ') }}</td>
      <td>
        {% if d.ai_action %}{{ d.ai_action }}{{ ' ' + d.ai_ticker if d.ai_ticker }}
          <span class="muted">({{ d.confidence or '?' }})</span>
        {% else %}<span class="muted">—</span>{% endif %}
      </td>
      <td>
        {% if d.order_state %}<span class="pill">{{ d.order_state }}</span>
        {% elif d.rule %}<span class="pill">{{ d.rule }}</span>
        {% else %}<span class="pill">{{ d.status }}</span>{% endif %}
      </td>
      <td class="muted">
        {{ (d.reasons | join('; ')) or d.halt_reason or d.reasoning or '' }}
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}<p class="muted">No cycles recorded yet.</p>{% endif %}

  <p class="muted">Refreshes every 60s. JSON at <a href="/api/state">/api/state</a>.</p>

  <script>
    var polling = null;
    function forceTrade() {
      var btn = document.getElementById('force-btn');
      var res = document.getElementById('force-result');
      btn.disabled = true;
      btn.textContent = '⏳ Running…';
      res.className = 'force-result running';
      res.textContent = 'Starting cycle…';

      fetch('/api/force-trade', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.error) {
            res.className = 'force-result err';
            res.textContent = '✗ ' + data.error;
            btn.disabled = false;
            btn.textContent = '⚡ Force trade cycle';
            return;
          }
          res.textContent = 'Cycle started (id: ' + data.id.substring(0, 8) + '…). Waiting for result…';
          pollResult(data.id);
        })
        .catch(function(err) {
          res.className = 'force-result err';
          res.textContent = '✗ Request failed: ' + err;
          btn.disabled = false;
          btn.textContent = '⚡ Force trade cycle';
        });
    }

    function pollResult(id) {
      var btn = document.getElementById('force-btn');
      var res = document.getElementById('force-result');
      if (polling) clearInterval(polling);
      polling = setInterval(function() {
        fetch('/api/force-trade/status?id=' + id)
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.status === 'running') {
              res.textContent = '⏳ Cycle in progress…';
              return;
            }
            clearInterval(polling);
            polling = null;
            btn.disabled = false;
            btn.textContent = '⚡ Force trade cycle';
            if (data.status === 'done') {
              var isGood = data.result && (data.result.indexOf('order:') === 0 ||
                           data.result === 'no-trade' || data.result === 'dry-run');
              res.className = 'force-result ' + (isGood ? 'ok' : 'err');
              res.textContent = (isGood ? '✓ ' : '⚠ ') + data.result;
            } else {
              res.className = 'force-result err';
              res.textContent = '✗ ' + (data.error || 'unknown error');
            }
          })
          .catch(function() {
            /* keep polling; a single failed fetch is not fatal */
          });
      }, 2000);
    }
  </script>
</body>
</html>
"""


def _latest_snapshot(storage: Storage) -> dict:
    rows = storage._read(  # noqa: SLF001 - dashboard is an internal read-only view
        "SELECT snapshot FROM cycles WHERE snapshot IS NOT NULL ORDER BY started_at DESC LIMIT 1"
    )
    if not rows:
        return {}
    try:
        return json.loads(rows[0]["snapshot"])
    except (json.JSONDecodeError, TypeError):
        return {}


def build_state(config: AppConfig, storage: Storage) -> dict:
    snapshot = _latest_snapshot(storage)
    day = trading_day(config)
    row = storage.daily_row(day)

    equity = dec(snapshot.get("equity", "0")) if snapshot else ZERO
    start = dec(row["start_equity"]) if row and row["start_equity"] else equity
    pnl = equity - start

    signals = snapshot.get("signals", {}) or {}
    positions = snapshot.get("positions", [])
    for position in positions:
        sig = signals.get(position.get("ticker"))
        position["trend"] = sig.get("trend") if sig else None

    utc_day = datetime.now(timezone.utc).date()

    return {
        "mode": config.mode,
        "generated": snapshot.get("as_of", "no cycles yet"),
        "cash": str(money(dec(snapshot.get("cash", "0")))),
        "invested": str(money(dec(snapshot.get("invested", "0")))),
        "equity": str(money(equity)),
        "pnl": str(money(pnl)),
        "pnl_positive": pnl >= ZERO,
        "trades_today": storage.trades_today(day),
        "positions": positions,
        "regime": snapshot.get("regime", ""),
        "api_calls_today": storage.ai_calls_today(utc_day),
        "api_budget": config.ai.daily_request_budget,
        "decisions": storage.recent_decisions(10),
        "breaker": storage.breaker(),
        "unresolved": storage.unresolved_orders(),
        "stop_engaged": config.stop_file.exists(),
    }


# --------------------------------------------------------------------------- #
# Force-trade: background cycle runner
# --------------------------------------------------------------------------- #

# In-memory store of pending / completed force-trade results. Only the last few
# are kept; there is no persistent queue — the audit trail is in SQLite.
_force_trades: dict[str, dict] = {}
_force_lock = threading.Lock()
_MAX_HISTORY = 20


def _run_force_cycle(run_id: str, config: AppConfig) -> None:
    """Build a fresh runtime and run one cycle with force=True."""
    try:
        runtime = build_runtime(config)
        try:
            result = safe_cycle(runtime, force=True)
        finally:
            runtime.close()
        with _force_lock:
            _force_trades[run_id] = {"status": "done", "result": result}
        log.info("force-trade %s finished: %s", run_id[:8], result)
    except Exception as exc:  # noqa: BLE001 - must not crash the server thread
        with _force_lock:
            _force_trades[run_id] = {"status": "error", "error": str(exc)}
        log.exception("force-trade %s failed", run_id[:8])


def create_app(config: AppConfig | None = None) -> Flask:
    config = config or load()
    storage = Storage(config.storage.db_path)
    app = Flask(__name__)

    @app.get("/")
    def index() -> str:
        state = build_state(config, storage)
        return render_template_string(
            PAGE,
            config=config,
            cap=money(config.capital.max_capital),
            **state,
        )

    @app.get("/api/state")
    def api_state():
        state = build_state(config, storage)
        state["breaker"] = dict(state["breaker"]) if state["breaker"] else None
        state["unresolved"] = [
            {
                "decision_id": o.decision_id,
                "state": o.state,
                "ticker": o.ticker,
                "side": o.side,
                "quantity": str(o.quantity),
                "error": o.error,
            }
            for o in state["unresolved"]
        ]
        return jsonify(state)

    @app.post("/api/force-trade")
    def force_trade():
        # Only one force-trade at a time.
        with _force_lock:
            running = any(t["status"] == "running" for t in _force_trades.values())
            if running:
                return jsonify({"error": "A force-trade cycle is already running."}), 409

            run_id = uuid.uuid4().hex
            _force_trades[run_id] = {"status": "running"}

            # Trim old entries.
            if len(_force_trades) > _MAX_HISTORY:
                old = sorted(
                    (k for k, v in _force_trades.items() if v["status"] != "running"),
                )
                for k in old[: len(_force_trades) - _MAX_HISTORY]:
                    _force_trades.pop(k, None)

        thread = threading.Thread(
            target=_run_force_cycle,
            args=(run_id, config),
            name=f"force-trade-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        log.info("force-trade %s started from dashboard", run_id[:8])
        return jsonify({"id": run_id, "status": "running"})

    @app.get("/api/force-trade/status")
    def force_trade_status():
        run_id = request.args.get("id", "")
        with _force_lock:
            entry = _force_trades.get(run_id)
        if entry is None:
            return jsonify({"error": "Unknown force-trade id."}), 404
        return jsonify({"id": run_id, **entry})

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True, "mode": config.mode})

    return app


def main() -> int:
    config = load()
    app = create_app(config)
    app.run(host=config.dashboard.host, port=config.dashboard.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
