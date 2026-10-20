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
    .nav-link { display: inline-block; margin: .5rem 0 0; font-weight: 600; }
  </style>
</head>
<body>
  <h1>t212-bot</h1>
  <a class="nav-link" href="/crypto">&#8599; Crypto trading (separate market, separate page)</a>
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
  </script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# A whole separate page for crypto — a different market with its own account,
# strategy, and risk settings, so it does not share the stock dashboard's
# layout. Every field pairs a plain-language label with a one-line
# description and an expandable "what does this do?" with the full effect of
# changing it — the goal is that nothing here needs the README open to use.
# --------------------------------------------------------------------------- #

CRYPTO_PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>t212-bot — Crypto</title>
  <style>
    :root { color-scheme: light dark; }
    body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1rem;
           max-width: 46rem; margin-inline: auto; line-height: 1.45; }
    h1 { font-size: 1.25rem; margin: 0 0 .25rem; }
    h2 { font-size: 1.05rem; margin: 1.8rem 0 .3rem; padding-bottom: .3rem;
         border-bottom: 1px solid rgba(128,128,128,.3); }
    h2:first-of-type { margin-top: 1.2rem; }
    .muted { opacity: .6; font-size: .8rem; }
    .nav-link { display: inline-block; margin: .5rem 0 1rem; font-weight: 600; }
    .banner { padding: .6rem .8rem; border-radius: .5rem; margin: .5rem 0 1rem; font-weight: 600; }
    .live { background: #b3261e; color: #fff; }
    .paper { background: #e7f0e7; color: #14431b; }
    .alert { background: #fde7cf; color: #6a3a00; }
    .section-intro { opacity: .75; font-size: .88rem; margin: .2rem 0 .8rem; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th, td { text-align: left; padding: .35rem .4rem; border-bottom: 1px solid rgba(128,128,128,.2);
             vertical-align: top; }
    th { font-weight: 600; opacity: .7; }
    td.num { text-align: right; font-variant-numeric: tabular-nums; }
    .force-btn { background: #1a73e8; color: #fff; border: none; border-radius: .4rem;
                 padding: .5rem 1.2rem; cursor: pointer; font-size: .9rem; font-weight: 600; }
    .force-btn:hover { background: #1557b0; }
    .force-btn:disabled { opacity: .5; cursor: not-allowed; }
    .stop-btn { background: #b3261e; }
    .stop-btn:hover { background: #8f1d17; }
    .force-result { margin-top: .6rem; padding: .5rem .6rem; border-radius: .4rem; font-size: .85rem; }
    .force-result.ok { background: #e7f0e7; color: #14431b; }
    .force-result.err { background: #fde7cf; color: #6a3a00; }
    .force-result.running { background: #e8eaf6; color: #1a237e; }
    .field { margin-bottom: 1.1rem; }
    .field label { display: block; font-weight: 600; font-size: .9rem; margin-bottom: .15rem; }
    .field .field-desc { font-size: .82rem; opacity: .75; margin-bottom: .35rem; }
    .field input, .field select, .field textarea {
      width: 100%; padding: .4rem .5rem; border-radius: .3rem;
      border: 1px solid rgba(128,128,128,.4); background: transparent; color: inherit;
      font: inherit; box-sizing: border-box;
    }
    .field details.help { margin-top: .35rem; font-size: .82rem; }
    .field details.help summary { cursor: pointer; opacity: .7; }
    .field details.help p { opacity: .85; margin: .4rem 0 0; }
    .mode-switch { display: flex; gap: .6rem; margin: .4rem 0; }
    .mode-switch label { display: flex; align-items: center; gap: .4rem; font-weight: 600;
      font-size: .9rem; border: 1px solid rgba(128,128,128,.4); border-radius: .4rem;
      padding: .5rem .8rem; cursor: pointer; flex: 1; }
    .mode-switch input { width: auto; margin: 0; }
  </style>
</head>
<body>
  <h1>t212-bot — Crypto</h1>
  <a class="nav-link" href="/">&#8592; Dashboard (Trading212)</a>
  <p class="muted">
    A second, independent market from the Trading212 side of this bot. No AI — it screens
    the pairs below with plain technical-analysis maths and trades the strategy you pick,
    for a medium-term hold with its own stop-loss and take-profit. Its money, positions, and
    settings are entirely separate from the Trading212 side; the only thing they share is the
    kill switch (<code>touch STOP</code> halts both).</p>
  <p class="muted">
    Every setting below can be changed here <em>or</em> in <code>config.yaml</code>'s
    <code>crypto:</code> section — whichever was saved most recently wins, field by field. So
    editing the file and restarting works, and so does saving here; they do not conflict, they
    just both write to the same place in the end.</p>

  <div id="crypto-status-box" class="banner paper">Loading…</div>

  <div style="margin:.6rem 0">
    <button class="force-btn" id="crypto-start-btn" onclick="cryptoStart()">&#9654; Start</button>
    <button class="force-btn stop-btn" id="crypto-stop-btn" onclick="cryptoStop()">&#9208; Stop</button>
  </div>
  <div id="crypto-action-result"></div>

  <h2>Open positions</h2>
  <div id="crypto-positions" class="muted">—</div>

  <h2>Recent trades</h2>
  <div id="crypto-trades" class="muted">—</div>

  <h2>Account</h2>
  <p class="section-intro">
    Which exchange this connects to, and whether it is practicing or trading for real.</p>

  <div class="field">
    <label>Paper or live</label>
    <div class="mode-switch">
      <label><input type="radio" name="cx-mode" id="cx-mode-paper" value="paper"> Paper (simulated)</label>
      <label><input type="radio" name="cx-mode" id="cx-mode-live" value="live"> Live (real money)</label>
    </div>
    <details class="help">
      <summary>What does this do?</summary>
      <p><strong>Paper</strong> uses real, live prices from the exchange but never places a
        real order — buys and sells are simulated against a virtual balance kept in this
        bot's own database. Nothing about your real exchange account is touched. Safe to
        leave running indefinitely to see how a strategy would have done.</p>
      <p><strong>Live</strong> places real orders with real money on the exchange account
        the API key below belongs to. Switching to live here is <strong>not enough on its
        own</strong>: the server this runs on must also have
        <code>I_UNDERSTAND_THIS_IS_REAL_MONEY=yes</code> set in its own environment (not in
        this form, on purpose — it is a second, deliberate step). Without it, saving
        <code>mode: live</code> is refused with an error.</p>
      <p>Paper and live keep completely separate cash and position records, so switching back
        and forth does not mix real and simulated history together.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-exchange">Exchange</label>
    <div class="field-desc">Which exchange this bot connects to, by its
      <a href="https://github.com/ccxt/ccxt/wiki/Exchange-Markets" target="_blank" rel="noopener">CCXT</a>
      id — e.g. binance, kraken, coinbase.</div>
    <input id="cx-exchange" type="text" placeholder="binance">
    <details class="help">
      <summary>What does this do?</summary>
      <p>This bot never talks to an exchange directly — it goes through the CCXT library,
        which speaks a common language to dozens of exchanges. This field picks which one.
        Changing it means the API key and secret below must belong to <em>that</em> exchange
        account, and the pairs below must be ones that exchange actually lists (e.g. Kraken
        and Binance don't always use identical pair names). Nothing else about how the bot
        decides to trade changes — only where the prices and orders come from.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-pairs">Pairs to watch</label>
    <div class="field-desc">One per line, e.g. BTC/USDT — all must share the same second
      half (the "quote currency").</div>
    <textarea id="cx-pairs" rows="4" placeholder="BTC/USDT&#10;ETH/USDT"></textarea>
    <details class="help">
      <summary>What does this do?</summary>
      <p>Each line is one market this bot is allowed to look at and trade — e.g.
        <code>BTC/USDT</code> means "Bitcoin, priced in Tether". Every pair must use the
        <strong>same</strong> currency after the slash, because the capital cap below is a
        single number measured in that currency — mixing <code>BTC/USDT</code> and
        <code>ETH/EUR</code> would make "100 of capital" ambiguous (100 what?). Adding more
        pairs gives the strategy more chances to find a trade, but each one still competes for
        the same capital cap, per-trade cap, and max-concurrent-positions limits below — more
        pairs does not mean more money, just more candidates.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-apikey">Exchange API key</label>
    <div class="field-desc">From the exchange account itself — leave blank to keep whatever
      is already saved.</div>
    <input id="cx-apikey" type="password" placeholder="leave blank to keep the current key">
    <details class="help">
      <summary>What does this do?</summary>
      <p>The credentials this bot uses to read your balance and, in live mode, place real
        orders on the exchange named above. Generate one from the exchange's own website
        (not here) — give it trading permission but <strong>not</strong> withdrawal
        permission, so that even a leaked key can only trade, never move funds out. Blank on
        save means "don't change it"; you only need to re-enter it when you actually have a
        new key.</p>
    </details>
  </div>
  <div class="field">
    <label for="cx-apisecret">Exchange API secret</label>
    <div class="field-desc">Same idea as the key above, and the same blank-keeps-current rule.</div>
    <input id="cx-apisecret" type="password" placeholder="leave blank to keep the current secret">
  </div>

  <h2>Strategy</h2>
  <p class="section-intro">
    What decides whether to buy, and what decides to sell once holding something — the daily
    stop-loss/take-profit checks below apply no matter which strategy is picked.</p>

  <div class="field">
    <label for="cx-strategy">Strategy</label>
    <div class="field-desc">How a buy is decided. Does not affect how a sell is decided — see
      stop-loss/take-profit below for that.</div>
    <select id="cx-strategy">
      <option value="trend">Trend-following</option>
      <option value="mean_reversion">Mean-reversion</option>
    </select>
    <details class="help">
      <summary>What does this do?</summary>
      <p><strong>Trend-following</strong> buys a pair once it is clearly trending up — several
        technical measurements (moving averages, momentum, RSI) all have to agree — and sells
        once that trend breaks down. It tends to do well in a market that is genuinely rising,
        and tends to buy a little late and sell a little late by nature (it waits for
        confirmation first). It can lose in a choppy, sideways market that keeps faking out a
        trend and reversing.</p>
      <p><strong>Mean-reversion</strong> buys a pair once its price has dropped unusually far
        below its own recent average — a statistical "oversold" dip — betting it snaps back,
        and sells once it has recovered toward that average. It tends to do well in a market
        that is range-bound (goes up and down but doesn't trend hard either way), and is
        riskier in a genuine crash, where it will keep buying dips that keep getting deeper.</p>
      <p>Changing this only changes what triggers a <em>new</em> buy from here on — a position
        already open keeps running under the stop-loss/take-profit rules below regardless of
        which strategy is selected when it closes.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-minscore">Min entry score <span class="muted">(trend-following only)</span></label>
    <div class="field-desc">0 to 1. How convincing the uptrend must be before buying.</div>
    <input id="cx-minscore" type="number" step="any" min="0" max="1">
    <details class="help">
      <summary>What does this do?</summary>
      <p>The trend-following strategy combines several signals (price vs. its moving
        averages, momentum, RSI) into one score from -1 (strongly bearish) to +1 (strongly
        bullish). A pair is only bought once its score is at or above this number.
        <strong>Raising it</strong> means fewer, more convincing entries — it waits for a
        stronger, more agreed-upon trend before risking money, at the cost of missing some
        real moves that started more hesitantly. <strong>Lowering it</strong> trades more
        often on weaker evidence, which usually means more false starts. Ignored entirely
        when the strategy above is mean-reversion.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-maxvol">Max volatility % <span class="muted">(trend-following only)</span></label>
    <div class="field-desc">An otherwise-good trend entry is skipped if the pair has been
      swinging more than this, annualised.</div>
    <input id="cx-maxvol" type="number" step="any" min="0">
    <details class="help">
      <summary>What does this do?</summary>
      <p>Even a pair with a convincing uptrend is skipped if its recent price swings
        (volatility, annualised) are above this percentage — a wild, choppy trend is more
        likely to whipsaw straight through a stop-loss before the "real" move happens.
        <strong>Raising it</strong> allows trading more volatile pairs (often smaller/newer
        coins, which trend hard but swing hard too). <strong>Lowering it</strong> restricts
        entries to calmer, larger-cap-style pairs. Ignored entirely when the strategy above
        is mean-reversion.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-entryz">Entry z-score <span class="muted">(mean-reversion only)</span></label>
    <div class="field-desc">Usually negative, e.g. -1.5. How far below its own average the
      price must fall before buying.</div>
    <input id="cx-entryz" type="number" step="any">
    <details class="help">
      <summary>What does this do?</summary>
      <p>A z-score measures how many standard deviations the current price is from its own
        recent average — 0 is "right on average", -2 is "unusually low". This is the
        threshold the price must drop <em>at or below</em> before mean-reversion considers it
        oversold and buys. <strong>A more negative number</strong> (e.g. -2 instead of -1.5)
        waits for a rarer, deeper dip — fewer trades, each one a more extreme bet that it
        snaps back. <strong>A less negative number</strong> (e.g. -1) buys smaller, far more
        common dips. Ignored entirely when the strategy above is trend-following.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-exitz">Exit z-score <span class="muted">(mean-reversion only)</span></label>
    <div class="field-desc">Usually 0. How far the price must recover before selling.</div>
    <input id="cx-exitz" type="number" step="any">
    <details class="help">
      <summary>What does this do?</summary>
      <p>Once a mean-reversion position is open, it is sold once the z-score recovers to at
        or above this number. <strong>0</strong> (the default) sells as soon as the price is
        back to its own recent average — a modest, reliable target. <strong>A positive
        number</strong> (e.g. 0.5) waits for the price to overshoot <em>above</em> average
        first, aiming for a bigger profit but giving back some of the gain if it stalls or
        reverses before getting there. Ignored entirely when the strategy above is
        trend-following.</p>
    </details>
  </div>

  <h2>Risk and capital</h2>
  <p class="section-intro">
    How much this is allowed to risk, and the rules that close a position regardless of what
    the strategy says.</p>

  <div class="field">
    <label for="cx-capital">Capital cap</label>
    <div class="field-desc">The most this whole crypto engine may have invested at once,
      across every open position combined, in the pairs' quote currency (e.g. USDT).</div>
    <input id="cx-capital" type="number" step="any" min="0">
    <details class="help">
      <summary>What does this do?</summary>
      <p>A hard ceiling, checked before every single buy: the total cost of everything
        currently held plus a new buy may never exceed this number. <strong>Raising it</strong>
        allows bigger and/or more simultaneous positions. <strong>Lowering it</strong> is the
        single biggest lever on how much this can lose in a worst case — in paper mode this is
        also the virtual balance it starts with.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-pertrade">Per-trade cap %</label>
    <div class="field-desc">Of the capital cap above, the most that can go into any one new
      buy.</div>
    <input id="cx-pertrade" type="number" step="any" min="0" max="100">
    <details class="help">
      <summary>What does this do?</summary>
      <p>E.g. 25% of a 100 USDT capital cap means at most 25 USDT goes into any single new
        position. <strong>Raising it</strong> means fewer, bigger, more concentrated
        positions (higher risk per trade, but capital gets used faster). <strong>Lowering
        it</strong> spreads the same capital cap across more, smaller positions — better
        diversification, but each individual trade matters less.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-minorder">Minimum order</label>
    <div class="field-desc">In the quote currency. Orders smaller than this are skipped
      rather than placed.</div>
    <input id="cx-minorder" type="number" step="any" min="0">
    <details class="help">
      <summary>What does this do?</summary>
      <p>An order this small or smaller is not worth placing — exchange fees would eat a
        disproportionate chunk of it, or the exchange might reject it outright as "dust".
        This must be smaller than the per-trade cap above (checked on save) — otherwise no
        order could ever clear both rules at once and nothing would ever be bought.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-maxpos">Max concurrent positions</label>
    <div class="field-desc">The most pairs that may be held open at the same time.</div>
    <input id="cx-maxpos" type="number" min="1">
    <details class="help">
      <summary>What does this do?</summary>
      <p>Once this many positions are open, no new buy happens — even a perfect signal is
        skipped — until something is sold and frees up a slot. <strong>Raising it</strong>
        spreads the capital cap across more pairs at once (each one gets a smaller slice, in
        practice, since the per-trade cap stays the same). <strong>Lowering it</strong>
        (e.g. to 1) means the whole engine ever only holds one thing at a time.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-maxtrades">Max trades per day</label>
    <div class="field-desc">Caps how many buys and sells can happen in one calendar day.</div>
    <input id="cx-maxtrades" type="number" min="0">
    <details class="help">
      <summary>What does this do?</summary>
      <p>A circuit-breaker on activity, not on loss: once this many trades have happened
        today, no more buys are placed (a stop-loss/take-profit sell on an already-open
        position is not blocked by this — closing a position is never refused). Guards
        against an unexpectedly choppy market turning this into constant in-and-out trading
        that mostly just pays the exchange in fees.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-stoploss">Stop-loss %</label>
    <div class="field-desc">Sell a position automatically once it has fallen this far below
      where it was bought.</div>
    <input id="cx-stoploss" type="number" step="any" min="0">
    <details class="help">
      <summary>What does this do?</summary>
      <p>The one rule that always fires, whatever the strategy says: once an open position is
        down this percentage from its entry price, it is sold immediately. <strong>A smaller
        number</strong> (e.g. 5%) cuts losses tighter but is more likely to be shaken out by
        ordinary price noise on a position that would have recovered. <strong>A bigger
        number</strong> (e.g. 15%) gives a position more room to breathe, at the cost of a
        bigger loss allowed before it cuts. This is checked once per poll interval (below),
        not continuously — on a fast-moving pair the actual loss by the time it sells can be
        a little worse than this number.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-takeprofit">Take-profit %</label>
    <div class="field-desc">Sell a position automatically once it has risen this far above
      where it was bought.</div>
    <input id="cx-takeprofit" type="number" step="any" min="0">
    <details class="help">
      <summary>What does this do?</summary>
      <p>The upside mirror of stop-loss: once an open position is up this percentage from its
        entry price, it is sold and the gain is banked — again regardless of what the
        strategy's own signal says. <strong>A smaller number</strong> locks in profit sooner
        but caps how much of a strong run this can capture. <strong>A bigger number</strong>
        lets a winner run further, at the cost of giving back more of the gain if it reverses
        before reaching the target.</p>
    </details>
  </div>

  <h2>Advanced</h2>
  <p class="section-intro">Timing and precision — the defaults are fine for most pairs.</p>

  <div class="field">
    <label for="cx-poll">Poll interval (seconds)</label>
    <div class="field-desc">How often prices are checked and a decision is made. Minimum 30.</div>
    <input id="cx-poll" type="number" min="30">
    <details class="help">
      <summary>What does this do?</summary>
      <p><strong>Shorter</strong> (e.g. 60s) reacts to price moves faster — including a
        stop-loss or take-profit triggering closer to the exact moment it was crossed — but
        calls the exchange's API far more often, which risks hitting its rate limits, and can
        over-react to short-lived noise. <strong>Longer</strong> (e.g. 900s) is calmer and
        uses fewer API calls, but a stop-loss can only ever be checked once per interval, so
        the actual loss by the time it sells can run further past the configured percentage
        than with a short interval.</p>
    </details>
  </div>

  <div class="field">
    <label for="cx-qdecimals">Quantity decimals</label>
    <div class="field-desc">How many decimal places of a coin the exchange accepts per order.</div>
    <input id="cx-qdecimals" type="number" min="0" max="12">
    <details class="help">
      <summary>What does this do?</summary>
      <p>Exchanges only accept order sizes to a certain number of decimal places (e.g. Bitcoin
        orders might be limited to 6: 0.000001 BTC). Every order size is rounded <strong>down</strong>
        to this many decimals, so it can only ever end up smaller than approved, never bigger.
        Set this too low for an expensive coin and a small per-trade cap can round all the way
        down to zero and get skipped entirely — if that starts happening, either raise this
        (check what the exchange actually allows for that pair) or raise the per-trade cap.</p>
    </details>
  </div>

  <button class="force-btn" id="crypto-save-btn" onclick="cryptoSaveSettings()">&#128190; Save settings</button>
  <div id="crypto-save-result"></div>

  <script>
    function cryptoLoadSettings() {
      fetch('/api/crypto/settings').then(function(r) { return r.json(); }).then(function(s) {
        document.getElementById(s.mode === 'live' ? 'cx-mode-live' : 'cx-mode-paper').checked = true;
        document.getElementById('cx-exchange').value = s.exchange;
        document.getElementById('cx-pairs').value = s.pairs.join('\\n');
        document.getElementById('cx-strategy').value = s.strategy;
        document.getElementById('cx-minscore').value = s.min_entry_score;
        document.getElementById('cx-maxvol').value = s.max_entry_vol_pct;
        document.getElementById('cx-entryz').value = s.entry_z;
        document.getElementById('cx-exitz').value = s.exit_z;
        document.getElementById('cx-capital').value = s.max_capital;
        document.getElementById('cx-pertrade').value = s.per_trade_cap_pct;
        document.getElementById('cx-minorder').value = s.min_order;
        document.getElementById('cx-maxpos').value = s.max_positions;
        document.getElementById('cx-maxtrades').value = s.max_trades_per_day;
        document.getElementById('cx-stoploss').value = s.stop_loss_pct;
        document.getElementById('cx-takeprofit').value = s.take_profit_pct;
        document.getElementById('cx-poll').value = s.poll_interval_seconds;
        document.getElementById('cx-qdecimals').value = s.quantity_decimals;
        document.getElementById('cx-apikey').placeholder =
          s.has_api_key ? 'leave blank to keep the saved key' : 'no key saved yet';
      }).catch(function() {});
    }

    function cryptoSaveSettings() {
      var btn = document.getElementById('crypto-save-btn');
      var res = document.getElementById('crypto-save-result');
      var pairs = document.getElementById('cx-pairs').value
        .split('\\n').map(function(s) { return s.trim(); }).filter(Boolean);
      var mode = document.getElementById('cx-mode-live').checked ? 'live' : 'paper';
      var body = {
        mode: mode,
        exchange: document.getElementById('cx-exchange').value,
        pairs: pairs,
        strategy: document.getElementById('cx-strategy').value,
        min_entry_score: document.getElementById('cx-minscore').value,
        max_entry_vol_pct: document.getElementById('cx-maxvol').value,
        entry_z: document.getElementById('cx-entryz').value,
        exit_z: document.getElementById('cx-exitz').value,
        max_capital: document.getElementById('cx-capital').value,
        per_trade_cap_pct: document.getElementById('cx-pertrade').value,
        min_order: document.getElementById('cx-minorder').value,
        max_positions: document.getElementById('cx-maxpos').value,
        max_trades_per_day: document.getElementById('cx-maxtrades').value,
        stop_loss_pct: document.getElementById('cx-stoploss').value,
        take_profit_pct: document.getElementById('cx-takeprofit').value,
        poll_interval_seconds: document.getElementById('cx-poll').value,
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
        box.className = 'banner ' + (s.mode === 'live' ? 'live' : 'paper');
        box.textContent = (s.running ? 'Running' : 'Stopped') + ' — mode ' + s.mode.toUpperCase() +
          (s.mode === 'live' ? ' — REAL MONEY' : '') +
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

    @app.get("/crypto")
    def crypto_page() -> str:
        return render_template_string(CRYPTO_PAGE)

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
