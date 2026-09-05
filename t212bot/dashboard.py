"""A one-page status view, for checking the bot from a phone on the home network.

Deliberately read-only and database-only: it renders the last snapshot the
trading cycle recorded rather than calling Trading212 itself. That keeps the
dashboard off the account's rate-limit budget and means opening it can never
interfere with a cycle in progress.

    python -m t212bot.dashboard          # http://<pi>:8080/

Bind it to the LAN only (``dashboard.host``) — there is no authentication here.
"""

from __future__ import annotations

import json

from flask import Flask, jsonify, render_template_string

from .config import AppConfig, load
from .models import ZERO, dec, money
from .main import trading_day
from .storage import Storage

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
  </style>
</head>
<body>
  <h1>t212-bot</h1>
  <div class="muted">{{ generated }} &middot; {{ config.ai.provider }} &middot;
      cap &pound;{{ cap }}</div>

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

  <h2>Open positions</h2>
  {% if positions %}
  <table>
    <tr><th>Ticker</th><th class="num">Qty</th><th class="num">Avg</th>
        <th class="num">Now</th><th class="num">Value</th><th class="num">P&amp;L</th></tr>
    {% for p in positions %}
    <tr>
      <td>{{ p.ticker }}</td>
      <td class="num">{{ p.quantity }}</td>
      <td class="num">{{ p.average_price }}</td>
      <td class="num">{{ p.current_price }}</td>
      <td class="num">{{ p.value }}</td>
      <td class="num {{ 'pos' if not p.unrealised_pnl.startswith('-') else 'neg' }}">
        {{ p.unrealised_pnl }}</td>
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

    return {
        "mode": config.mode,
        "generated": snapshot.get("as_of", "no cycles yet"),
        "cash": str(money(dec(snapshot.get("cash", "0")))),
        "invested": str(money(dec(snapshot.get("invested", "0")))),
        "equity": str(money(equity)),
        "pnl": str(money(pnl)),
        "pnl_positive": pnl >= ZERO,
        "trades_today": storage.trades_today(day),
        "positions": snapshot.get("positions", []),
        "decisions": storage.recent_decisions(10),
        "breaker": storage.breaker(),
        "unresolved": storage.unresolved_orders(),
        "stop_engaged": config.stop_file.exists(),
    }


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
