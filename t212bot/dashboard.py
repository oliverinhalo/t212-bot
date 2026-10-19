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
import os
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template_string, request

from .config import AppConfig, ConfigError, crypto_overlay_paths, load, load_crypto_config
from .crypto_engine import CryptoScheduler
from .indicators import compute_all
from .models import ZERO, Verdict, dec, money
from .main import build_runtime, safe_cycle, trading_day
from .portfolio import load_account_state
from .risk_manager import floor_quantity
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
    .stop-btn { background: #b3261e; }
    .stop-btn:hover { background: #8f1d17; }
    .settings-grid label { display: block; margin-bottom: .6rem; font-size: .85rem; }
    .settings-grid input, .settings-grid select, .settings-grid textarea {
      width: 100%; margin-top: .2rem; padding: .35rem .45rem; border-radius: .3rem;
      border: 1px solid rgba(128,128,128,.4); background: transparent; color: inherit;
      font: inherit; box-sizing: border-box;
    }
    summary { font-weight: 600; }
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

    <h2 style="margin-top:1.2rem">Buy now</h2>
    <p class="muted" style="margin:.2rem 0 .6rem">
      Buys &pound;{{ quick_buy_amount }} of whichever watch-list instrument the
      local signals rank highest, immediately.
      <strong>No AI, no caps, no confidence gate, no market-hours gate.</strong>
      Only the kill switch still stops it.</p>
    <button class="force-btn" id="quick-buy-btn" onclick="quickBuy()">
      💷 Buy &pound;{{ quick_buy_amount }} of the top pick
    </button>
    <div id="quick-buy-result"></div>
  </div>

  <div class="force-section" id="crypto-section">
    <h2 style="margin-top:0">Crypto</h2>
    <p class="muted" style="margin:.2rem 0 .6rem">
      A second, independent market — no AI, screened with the same trend/RSI/volatility
      maths used above, trend-following or mean-reversion, medium-term holds with their own
      stop-loss and take-profit. The same kill switch halts this too.</p>

    <div id="crypto-status-box" class="muted">Loading…</div>
    <div style="margin:.6rem 0">
      <button class="force-btn" id="crypto-start-btn" onclick="cryptoStart()">▶ Start</button>
      <button class="force-btn stop-btn" id="crypto-stop-btn" onclick="cryptoStop()">⏸ Stop</button>
    </div>
    <div id="crypto-action-result"></div>

    <h2>Crypto positions</h2>
    <div id="crypto-positions" class="muted">—</div>

    <h2>Recent crypto trades</h2>
    <div id="crypto-trades" class="muted">—</div>

    <details style="margin-top:1.2rem">
      <summary style="cursor:pointer">Settings</summary>
      <div class="settings-grid" style="margin-top:.6rem">
        <label>Exchange (a CCXT id, e.g. binance, kraken, coinbase)
          <input id="cx-exchange" type="text" placeholder="binance"></label>
        <label>Mode
          <select id="cx-mode"><option value="paper">paper</option><option value="live">live</option></select></label>
        <label>Strategy
          <select id="cx-strategy"><option value="trend">trend-following</option>
            <option value="mean_reversion">mean-reversion</option></select></label>
        <label>Pairs — one per line, same quote currency, e.g. BTC/USDT
          <textarea id="cx-pairs" rows="3" placeholder="BTC/USDT&#10;ETH/USDT"></textarea></label>
        <label>Poll interval (seconds) <input id="cx-poll" type="number" min="30"></label>
        <label>Capital cap <input id="cx-capital" type="number" step="any"></label>
        <label>Per-trade cap % <input id="cx-pertrade" type="number" step="any"></label>
        <label>Minimum order <input id="cx-minorder" type="number" step="any"></label>
        <label>Max concurrent positions <input id="cx-maxpos" type="number" min="1"></label>
        <label>Max trades per day <input id="cx-maxtrades" type="number" min="0"></label>
        <label>Stop-loss % <input id="cx-stoploss" type="number" step="any"></label>
        <label>Take-profit % <input id="cx-takeprofit" type="number" step="any"></label>
        <label>Min entry score — trend strategy <input id="cx-minscore" type="number" step="any"></label>
        <label>Max volatility % — trend strategy <input id="cx-maxvol" type="number" step="any"></label>
        <label>Entry z-score — mean-reversion <input id="cx-entryz" type="number" step="any"></label>
        <label>Exit z-score — mean-reversion <input id="cx-exitz" type="number" step="any"></label>
        <label>Quantity decimals <input id="cx-qdecimals" type="number" min="0" max="12"></label>
        <label id="cx-key-label">API key <input id="cx-apikey" type="password"
          placeholder="leave blank to keep the current key"></label>
        <label>API secret <input id="cx-apisecret" type="password"
          placeholder="leave blank to keep the current secret"></label>
      </div>
      <button class="force-btn" id="crypto-save-btn" onclick="cryptoSaveSettings()">💾 Save settings</button>
      <div id="crypto-save-result"></div>
      <p class="muted" style="margin-top:.6rem">
        Switching mode to <code>live</code> here is not enough on its own: the machine this
        runs on also needs <code>I_UNDERSTAND_THIS_IS_REAL_MONEY=yes</code> set in its own
        environment. That one confirmation is deliberately not in this form.</p>
    </details>
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

    var quickPolling = null;
    function quickBuy() {
      var btn = document.getElementById('quick-buy-btn');
      var res = document.getElementById('quick-buy-result');
      var label = btn.textContent;
      btn.disabled = true;
      btn.textContent = '⏳ Buying…';
      res.className = 'force-result running';
      res.textContent = 'Placing order…';

      fetch('/api/quick-buy', { method: 'POST' })
        .then(function(r) { return r.json(); })
        .then(function(data) {
          if (data.error) {
            res.className = 'force-result err';
            res.textContent = '✗ ' + data.error;
            btn.disabled = false;
            btn.textContent = label;
            return;
          }
          pollQuickBuy(data.id, label);
        })
        .catch(function(err) {
          res.className = 'force-result err';
          res.textContent = '✗ Request failed: ' + err;
          btn.disabled = false;
          btn.textContent = label;
        });
    }

    function pollQuickBuy(id, label) {
      var btn = document.getElementById('quick-buy-btn');
      var res = document.getElementById('quick-buy-result');
      if (quickPolling) clearInterval(quickPolling);
      quickPolling = setInterval(function() {
        fetch('/api/quick-buy/status?id=' + id)
          .then(function(r) { return r.json(); })
          .then(function(data) {
            if (data.status === 'running') {
              res.textContent = '⏳ Placing order…';
              return;
            }
            clearInterval(quickPolling);
            quickPolling = null;
            btn.disabled = false;
            btn.textContent = label;
            if (data.status === 'done') {
              var filled = data.order_state === 'filled' || data.order_state === 'accepted';
              res.className = 'force-result ' + (filled ? 'ok' : 'err');
              res.textContent = (filled ? '✓ ' : '⚠ ') + data.result +
                                (data.error ? ' — ' + data.error : '');
            } else {
              res.className = 'force-result err';
              res.textContent = '✗ ' + (data.error || 'unknown error');
            }
          })
          .catch(function() { /* keep polling */ });
      }, 2000);
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

    function cryptoLoadSettings() {
      fetch('/api/crypto/settings').then(function(r) { return r.json(); }).then(function(s) {
        document.getElementById('cx-exchange').value = s.exchange;
        document.getElementById('cx-mode').value = s.mode;
        document.getElementById('cx-strategy').value = s.strategy;
        document.getElementById('cx-pairs').value = s.pairs.join('\n');
        document.getElementById('cx-poll').value = s.poll_interval_seconds;
        document.getElementById('cx-capital').value = s.max_capital;
        document.getElementById('cx-pertrade').value = s.per_trade_cap_pct;
        document.getElementById('cx-minorder').value = s.min_order;
        document.getElementById('cx-maxpos').value = s.max_positions;
        document.getElementById('cx-maxtrades').value = s.max_trades_per_day;
        document.getElementById('cx-stoploss').value = s.stop_loss_pct;
        document.getElementById('cx-takeprofit').value = s.take_profit_pct;
        document.getElementById('cx-minscore').value = s.min_entry_score;
        document.getElementById('cx-maxvol').value = s.max_entry_vol_pct;
        document.getElementById('cx-entryz').value = s.entry_z;
        document.getElementById('cx-exitz').value = s.exit_z;
        document.getElementById('cx-qdecimals').value = s.quantity_decimals;
        document.getElementById('cx-key-label').firstChild.textContent =
          s.has_api_key ? 'API key (one is already saved) ' : 'API key ';
      }).catch(function() {});
    }

    function cryptoSaveSettings() {
      var btn = document.getElementById('crypto-save-btn');
      var res = document.getElementById('crypto-save-result');
      var pairs = document.getElementById('cx-pairs').value
        .split('\n').map(function(s) { return s.trim(); }).filter(Boolean);
      var body = {
        exchange: document.getElementById('cx-exchange').value,
        mode: document.getElementById('cx-mode').value,
        strategy: document.getElementById('cx-strategy').value,
        pairs: pairs,
        poll_interval_seconds: document.getElementById('cx-poll').value,
        max_capital: document.getElementById('cx-capital').value,
        per_trade_cap_pct: document.getElementById('cx-pertrade').value,
        min_order: document.getElementById('cx-minorder').value,
        max_positions: document.getElementById('cx-maxpos').value,
        max_trades_per_day: document.getElementById('cx-maxtrades').value,
        stop_loss_pct: document.getElementById('cx-stoploss').value,
        take_profit_pct: document.getElementById('cx-takeprofit').value,
        min_entry_score: document.getElementById('cx-minscore').value,
        max_entry_vol_pct: document.getElementById('cx-maxvol').value,
        entry_z: document.getElementById('cx-entryz').value,
        exit_z: document.getElementById('cx-exitz').value,
        quantity_decimals: document.getElementById('cx-qdecimals').value,
        api_key: document.getElementById('cx-apikey').value,
        api_secret: document.getElementById('cx-apisecret').value
      };
      btn.disabled = true;
      res.className = 'force-result running';
      res.textContent = 'Saving…';
      fetch('/api/crypto/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body)
      }).then(function(r) { return r.json().then(function(data) { return { ok: r.ok, data: data }; }); })
        .then(function(result) {
          btn.disabled = false;
          document.getElementById('cx-apikey').value = '';
          document.getElementById('cx-apisecret').value = '';
          if (!result.ok) {
            res.className = 'force-result err';
            res.textContent = '✗ ' + (result.data.error || 'save failed');
            return;
          }
          res.className = 'force-result ok';
          res.textContent = '✓ saved' + (result.data.running ? ' — engine running' : ' — engine stopped');
          cryptoLoadSettings();
          cryptoRefreshStatus();
        })
        .catch(function(err) {
          btn.disabled = false;
          res.className = 'force-result err';
          res.textContent = '✗ request failed: ' + err;
        });
    }

    function cryptoStart() { cryptoToggle('/api/crypto/start'); }
    function cryptoStop() { cryptoToggle('/api/crypto/stop'); }

    function cryptoToggle(url) {
      var res = document.getElementById('crypto-action-result');
      res.className = 'force-result running';
      res.textContent = 'Working…';
      fetch(url, { method: 'POST' })
        .then(function(r) { return r.json().then(function(data) { return { ok: r.ok, data: data }; }); })
        .then(function(result) {
          if (!result.ok) {
            res.className = 'force-result err';
            res.textContent = '✗ ' + (result.data.error || 'request failed');
            return;
          }
          res.className = 'force-result ok';
          res.textContent = result.data.running ? '✓ running' : '✓ stopped';
          cryptoRefreshStatus();
        })
        .catch(function(err) {
          res.className = 'force-result err';
          res.textContent = '✗ request failed: ' + err;
        });
    }

    function cryptoRefreshStatus() {
      fetch('/api/crypto/status').then(function(r) { return r.json(); }).then(function(s) {
        var box = document.getElementById('crypto-status-box');
        box.textContent = (s.running ? 'Running' : 'Stopped') + ' — mode ' + s.mode +
          (s.cash !== null ? ', paper cash ' + s.cash : '') +
          (s.last_result && s.last_result.halted ? ' — HALTED: ' + s.last_result.halted : '') +
          (s.last_result && s.last_result.started_at ? ' — last cycle ' + s.last_result.started_at : '');

        var positions = document.getElementById('crypto-positions');
        if (s.positions.length === 0) {
          positions.textContent = 'No open crypto positions.';
        } else {
          positions.innerHTML = '<table><tr><th>Symbol</th><th class="num">Qty</th>' +
            '<th class="num">Avg</th><th>Opened</th></tr>' + s.positions.map(function(p) {
              return '<tr><td>' + p.symbol + '</td><td class="num">' + p.quantity +
                '</td><td class="num">' + p.average_price + '</td><td class="muted">' +
                p.opened_at.slice(0, 16).replace('T', ' ') + '</td></tr>';
            }).join('') + '</table>';
        }

        var trades = document.getElementById('crypto-trades');
        if (s.recent_trades.length === 0) {
          trades.textContent = 'No crypto trades yet.';
        } else {
          trades.innerHTML = '<table><tr><th>When</th><th>Side</th><th>Symbol</th>' +
            '<th class="num">Qty</th><th class="num">Price</th><th>Reason</th></tr>' +
            s.recent_trades.map(function(t) {
              return '<tr><td class="muted">' + t.created_at.slice(0, 16).replace('T', ' ') +
                '</td><td>' + t.side + '</td><td>' + t.symbol + '</td><td class="num">' +
                t.quantity + '</td><td class="num">' + t.price + '</td><td class="muted">' +
                (t.error || t.reason || '') + '</td></tr>';
            }).join('') + '</table>';
        }
      }).catch(function() {});
    }

    cryptoLoadSettings();
    cryptoRefreshStatus();
    setInterval(cryptoRefreshStatus, 5000);
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


# --------------------------------------------------------------------------- #
# Quick-buy: one click, a fixed amount, the top-ranked instrument
# --------------------------------------------------------------------------- #

_quick_buys: dict[str, dict] = {}


def _top_ranked(config: AppConfig, snapshot) -> tuple[str, object]:
    """The watch-list instrument the local signals rank highest, and its quote.

    "Top recommended" without asking the model: the same trend score the local
    strategy ranks entries by, over the instruments we have a live price for.
    With no usable history at all, the first priced watch-list entry wins —
    an arbitrary but predictable choice, rather than no answer.
    """
    priced = [item for item in config.watchlist if item.ticker in snapshot.quotes]
    if not priced:
        raise ExecutionRefused(f"no live price for any watch-list ticker: {snapshot.errors}")

    signals = compute_all(
        config.watchlist, snapshot.quotes, snapshot.histories, config.ai.indicators
    )
    ranked = sorted(
        (s for t, s in signals.items() if t in snapshot.quotes),
        key=lambda s: s.score,
        reverse=True,
    )
    ticker = ranked[0].ticker if ranked else priced[0].ticker
    return ticker, snapshot.quotes[ticker]


class ExecutionRefused(Exception):
    """The quick buy could not be turned into an order."""


def _run_quick_buy(run_id: str, config: AppConfig) -> None:
    """Buy ``dashboard.quick_buy_gbp`` of the top-ranked instrument, now.

    This is the manual override: no AI call, no confidence gate, no capital or
    position cap, no market-hours gate, no daily-trade budget. It still honours
    the kill switch — a STOP file is an emergency brake, and a button that
    ignored it would not be an override, it would be a bug — and it still goes
    through the executor, so the order gets a duplicate guard and an audit row
    like any other.
    """
    amount = config.dashboard.quick_buy_gbp
    try:
        if config.stop_file.exists():
            raise ExecutionRefused(
                f"kill switch is engaged ({config.stop_file}) — delete it to trade"
            )

        runtime = build_runtime(config)
        try:
            snapshot = runtime.market.fetch(config.watchlist, config.max_history_days)
            ticker, quote = _top_ranked(config, snapshot)

            quantity = floor_quantity(amount / quote.price, config)
            if quantity <= ZERO:
                raise ExecutionRefused(
                    f"{money(amount)} does not buy a tradeable quantity of {ticker} "
                    f"at {money(quote.price)}"
                    + ("" if config.execution.fractional else " (whole shares only)")
                )

            notional = quantity * quote.price
            decision_id = uuid.uuid4().hex
            day = trading_day(config)
            verdict = Verdict(
                approved=True,
                action="buy",
                rule="MANUAL_QUICK_BUY",
                reasons=(
                    f"manual quick buy from the dashboard: {money(amount)} of {ticker} "
                    f"at ~{quote.price} = {quantity} shares ({money(notional)}); "
                    "risk checks bypassed by request",
                ),
                ticker=ticker,
                quantity=quantity,
                notional=notional,
                reference_price=quote.price,
            )

            runtime.storage.start_cycle(decision_id, day, config.mode)
            runtime.storage.record_verdict(decision_id, verdict)
            log.warning(
                "QUICK BUY: %s of %s at ~%s (%s shares) — checks bypassed",
                money(amount),
                ticker,
                quote.price,
                quantity,
            )

            record = runtime.executor.execute(
                decision_id,
                verdict,
                day,
                lambda: load_account_state(
                    config, runtime.storage, runtime.client, snapshot.prices()
                ),
                skip_recheck=True,
            )
            state = record.state if record else "not-placed"
            runtime.storage.finish_cycle(decision_id, "completed", f"quick buy {state}")
        finally:
            runtime.close()

        with _force_lock:
            _quick_buys[run_id] = {
                "status": "done",
                "result": f"{state}: {money(notional)} of {ticker}",
                "ticker": ticker,
                "order_state": state,
                "error": record.error if record else None,
            }
        log.info("quick-buy %s finished: %s %s", run_id[:8], state, ticker)
    except ExecutionRefused as exc:
        with _force_lock:
            _quick_buys[run_id] = {"status": "error", "error": str(exc)}
        log.warning("quick-buy %s refused: %s", run_id[:8], exc)
    except Exception as exc:  # noqa: BLE001 - must not crash the server thread
        with _force_lock:
            _quick_buys[run_id] = {"status": "error", "error": str(exc)}
        log.exception("quick-buy %s failed", run_id[:8])


# --------------------------------------------------------------------------- #
# Crypto: settings saved by the GUI, and the background engine it drives
# --------------------------------------------------------------------------- #


def _apply_crypto_settings(
    config: AppConfig,
    fields: dict,
    api_key: str | None,
    api_secret: str | None,
) -> AppConfig:
    """Merge ``fields`` into the saved crypto overlay and return a new config.

    Writes next to the database (``crypto_overlay_paths``), never into .env or
    config.yaml — see that function's docstring for why. Only the crypto slice
    of ``config`` changes; everything else (the stock side, AI, dashboard
    settings) is untouched. Raises ``ConfigError`` on anything invalid, same
    as a normal config.yaml edit would.
    """
    secrets_path, settings_path = crypto_overlay_paths(config.storage.db_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict = {}
    if settings_path.exists():
        try:
            existing = json.loads(settings_path.read_text()) or {}
        except (json.JSONDecodeError, OSError):
            existing = {}
    existing.update(fields)
    settings_path.write_text(json.dumps(existing, indent=2, default=str))

    secrets = config.secrets
    if api_key or api_secret:
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        new_key = api_key or secrets.crypto_api_key
        new_secret = api_secret or secrets.crypto_api_secret
        secrets_path.write_text(f"CRYPTO_API_KEY={new_key}\nCRYPTO_API_SECRET={new_secret}\n")
        # Take effect immediately in this process, without waiting on a reload
        # to re-read the file we just wrote.
        os.environ["CRYPTO_API_KEY"] = new_key
        os.environ["CRYPTO_API_SECRET"] = new_secret
        secrets = replace(secrets, crypto_api_key=new_key, crypto_api_secret=new_secret)

    new_crypto = load_crypto_config({}, secrets, settings_path)
    return replace(config, crypto=new_crypto, secrets=secrets)


def _crypto_settings_public(config: AppConfig) -> dict:
    c = config.crypto
    return {
        "enabled": c.enabled,
        "exchange": c.exchange,
        "mode": c.mode,
        "pairs": list(c.pairs),
        "strategy": c.strategy,
        "poll_interval_seconds": c.poll_interval_seconds,
        "max_capital": str(c.max_capital),
        "per_trade_cap_pct": str(c.per_trade_cap_pct),
        "min_order": str(c.min_order),
        "max_positions": c.max_positions,
        "max_trades_per_day": c.max_trades_per_day,
        "stop_loss_pct": str(c.stop_loss_pct),
        "take_profit_pct": str(c.take_profit_pct),
        "min_entry_score": str(c.min_entry_score),
        "max_entry_vol_pct": str(c.max_entry_vol_pct),
        "entry_z": str(c.entry_z),
        "exit_z": str(c.exit_z),
        "quantity_decimals": c.quantity_decimals,
        "has_api_key": bool(config.secrets.crypto_api_key),
    }


_CRYPTO_SETTINGS_FIELDS = (
    "enabled", "exchange", "mode", "strategy", "pairs", "poll_interval_seconds",
    "max_capital", "per_trade_cap_pct", "min_order", "max_positions", "max_trades_per_day",
    "stop_loss_pct", "take_profit_pct", "min_entry_score", "max_entry_vol_pct",
    "entry_z", "exit_z", "quantity_decimals",
)


def create_app(config: AppConfig | None = None) -> Flask:
    config = config or load()
    storage = Storage(config.storage.db_path)
    app = Flask(__name__)

    # The stock side's ``config`` above is never reassigned — only the crypto
    # settings form changes anything at runtime, and it changes only the
    # crypto slice. ``_cfg`` is the one mutable reference to the *current*
    # config, read by every crypto route; everything else keeps closing over
    # the original ``config``.
    _cfg = {"value": config}
    crypto_scheduler = CryptoScheduler(config, storage)
    if config.crypto.enabled:
        crypto_scheduler.start()

    @app.get("/")
    def index() -> str:
        state = build_state(config, storage)
        return render_template_string(
            PAGE,
            config=config,
            cap=money(config.capital.max_capital),
            quick_buy_amount=money(config.dashboard.quick_buy_gbp),
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

    @app.post("/api/quick-buy")
    def quick_buy():
        with _force_lock:
            if any(t["status"] == "running" for t in _quick_buys.values()):
                return jsonify({"error": "A quick buy is already running."}), 409
            run_id = uuid.uuid4().hex
            _quick_buys[run_id] = {"status": "running"}
            if len(_quick_buys) > _MAX_HISTORY:
                old = sorted(k for k, v in _quick_buys.items() if v["status"] != "running")
                for k in old[: len(_quick_buys) - _MAX_HISTORY]:
                    _quick_buys.pop(k, None)

        threading.Thread(
            target=_run_quick_buy,
            args=(run_id, config),
            name=f"quick-buy-{run_id[:8]}",
            daemon=True,
        ).start()
        log.warning("quick-buy %s started from dashboard", run_id[:8])
        return jsonify({"id": run_id, "status": "running"})

    @app.get("/api/quick-buy/status")
    def quick_buy_status():
        with _force_lock:
            entry = _quick_buys.get(request.args.get("id", ""))
        if entry is None:
            return jsonify({"error": "Unknown quick-buy id."}), 404
        return jsonify(entry)

    @app.get("/api/crypto/settings")
    def crypto_settings_get():
        return jsonify(_crypto_settings_public(_cfg["value"]))

    @app.post("/api/crypto/settings")
    def crypto_settings_save():
        body = request.get_json(silent=True) or {}
        fields = {k: body[k] for k in _CRYPTO_SETTINGS_FIELDS if k in body}
        try:
            new_config = _apply_crypto_settings(
                _cfg["value"], fields, body.get("api_key"), body.get("api_secret")
            )
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400

        _cfg["value"] = new_config
        crypto_scheduler.stop()
        crypto_scheduler.reconfigure(new_config)
        if new_config.crypto.enabled:
            crypto_scheduler.start()
        log.warning(
            "crypto settings saved from dashboard: exchange=%s mode=%s strategy=%s pairs=%s",
            new_config.crypto.exchange, new_config.crypto.mode, new_config.crypto.strategy,
            ", ".join(new_config.crypto.pairs),
        )
        return jsonify({"ok": True, "running": crypto_scheduler.running, **_crypto_settings_public(new_config)})

    @app.post("/api/crypto/start")
    def crypto_start():
        current = _cfg["value"]
        if not current.crypto.pairs:
            return jsonify({"error": "no crypto pairs configured yet — save settings first"}), 400
        try:
            new_config = _apply_crypto_settings(current, {"enabled": True}, None, None)
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        _cfg["value"] = new_config
        crypto_scheduler.reconfigure(new_config)
        crypto_scheduler.start()
        log.warning("crypto engine started from dashboard")
        return jsonify({"running": True})

    @app.post("/api/crypto/stop")
    def crypto_stop():
        current = _cfg["value"]
        new_config = _apply_crypto_settings(current, {"enabled": False}, None, None)
        _cfg["value"] = new_config
        crypto_scheduler.stop()
        log.warning("crypto engine stopped from dashboard")
        return jsonify({"running": False})

    @app.get("/api/crypto/status")
    def crypto_status():
        current = _cfg["value"]
        positions = [
            {
                "symbol": symbol,
                "quantity": str(quantity),
                "average_price": str(average_price),
                "opened_at": opened_at.isoformat(),
            }
            for symbol, (quantity, average_price, opened_at) in storage.crypto_positions().items()
        ]
        return jsonify(
            {
                "enabled": current.crypto.enabled,
                "running": crypto_scheduler.running,
                "mode": current.crypto.mode,
                "last_result": crypto_scheduler.last_result,
                "cash": str(storage.crypto_cash()) if current.crypto.mode == "paper" else None,
                "positions": positions,
                "recent_trades": storage.recent_crypto_trades(10),
            }
        )

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
