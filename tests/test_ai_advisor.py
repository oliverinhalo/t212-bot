"""Prompt construction and defensive parsing of model output."""

from __future__ import annotations

import json
from datetime import date, timedelta

from t212bot.ai_advisor import (
    OmniRouteProvider,
    OpenRouterProvider,
    StubProvider,
    advise,
    build_prompt,
    parse_proposal,
)
from t212bot.market_data import Snapshot
from t212bot.models import Bar, PriceHistory, dec

from conftest import OTHER, TICKER, make_account, make_config, make_position, make_quote


def snapshot(prices=None, missing=()) -> Snapshot:
    prices = prices or {TICKER: dec(10), OTHER: dec(7)}
    quotes = {t: make_quote(t, p) for t, p in prices.items()}
    start = date(2026, 1, 1)
    histories = {
        t: PriceHistory(
            ticker=t,
            bars=tuple(
                Bar(day=start + timedelta(days=i), close=dec(10) + dec(i) / 10)
                for i in range(25)
            ),
        )
        for t in prices
    }
    return Snapshot(quotes=quotes, histories=histories, errors={t: "no data" for t in missing})


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def test_parses_a_clean_json_object():
    raw = json.dumps(
        {
            "action": "buy",
            "ticker": TICKER,
            "notional_or_qty": 12.5,
            "size_unit": "gbp",
            "price": 10.2,
            "confidence": 0.82,
            "reasoning": "trend is up",
        }
    )
    proposal = parse_proposal(raw)
    assert proposal.action == "buy"
    assert proposal.ticker == TICKER
    assert proposal.notional == dec("12.5")
    assert proposal.quantity is None
    assert proposal.price == dec("10.2")
    assert proposal.confidence == dec("0.82")


def test_parses_json_inside_a_markdown_fence():
    raw = '```json\n{"action": "hold", "confidence": 1, "reasoning": "wait"}\n```'
    assert parse_proposal(raw).action == "hold"


def test_parses_json_after_leading_prose():
    raw = 'Sure! Here is my answer:\n{"action": "sell", "ticker": "X", "confidence": 0.9}'
    proposal = parse_proposal(raw)
    assert proposal.action == "sell"
    assert proposal.ticker == "X"


def test_shares_unit_is_read_as_a_quantity():
    raw = json.dumps(
        {"action": "buy", "ticker": TICKER, "notional_or_qty": 3, "size_unit": "shares",
         "confidence": 0.9}
    )
    proposal = parse_proposal(raw)
    assert proposal.quantity == dec(3)
    assert proposal.notional is None


def test_missing_size_unit_defaults_to_money():
    """Reading £10 as 10 shares would be a far bigger order than intended."""
    raw = json.dumps({"action": "buy", "ticker": TICKER, "notional_or_qty": 10, "confidence": 0.9})
    proposal = parse_proposal(raw)
    assert proposal.notional == dec(10)
    assert proposal.quantity is None


def test_a_reply_cut_off_mid_field_keeps_what_arrived():
    # Verbatim shape of the failures seen in production: a reasoning model
    # spends its token budget thinking and stops part-way through the JSON.
    raw = '{\n  "action": "sell",\n  "ticker": "VUAGl_EQ",\n  "notional_or_qty": 10.0,'
    proposal = parse_proposal(raw)
    assert proposal.action == "sell"
    assert proposal.ticker == "VUAGl_EQ"
    assert proposal.notional == dec(10)
    assert "truncated" in proposal.reasoning


def test_a_reply_cut_off_before_the_size_keeps_the_action_but_no_size():
    raw = '{\n  "action": "buy",\n  "ticker": "VUSAl_EQ",\n  "notional'
    proposal = parse_proposal(raw)
    assert proposal.action == "buy"
    assert proposal.ticker == "VUSAl_EQ"
    # Nothing is invented: with no size the risk manager rejects it (R12).
    assert proposal.notional is None
    assert proposal.quantity is None


def test_a_recovered_reply_never_invents_a_confidence():
    raw = '{"action": "buy", "ticker": "VUSAl_EQ", "notional_or_qty": 5,'
    assert parse_proposal(raw).confidence == dec(0)


def test_a_fragment_with_no_object_start_is_not_salvaged():
    # A tail of a reasoning trace, with no opening brace: acting on it would
    # mean trading on the model's scratch work rather than its answer.
    raw = 'VUSAl_EQ",\n  "notional_or_qty": 6.00,\n  "size_unit": "gbp",\n  "price'
    proposal = parse_proposal(raw)
    assert proposal.action == "hold"
    assert "unparseable" in proposal.reasoning


def test_a_complete_object_is_not_flagged_as_recovered():
    raw = json.dumps({"action": "hold", "reasoning": "nothing doing"})
    assert parse_proposal(raw).reasoning == "nothing doing"


def test_unparseable_output_becomes_a_hold():
    proposal = parse_proposal("I'm afraid I can't help with that.")
    assert proposal.action == "hold"
    assert "unparseable" in proposal.reasoning


def test_empty_output_becomes_a_hold():
    assert parse_proposal("").action == "hold"


def test_unknown_action_becomes_a_hold():
    raw = json.dumps({"action": "short", "ticker": TICKER, "confidence": 1})
    proposal = parse_proposal(raw)
    assert proposal.action == "hold"


def test_confidence_is_clamped_to_zero_and_one():
    assert parse_proposal(json.dumps({"action": "hold", "confidence": 42})).confidence == dec(1)
    assert parse_proposal(json.dumps({"action": "hold", "confidence": -3})).confidence == dec(0)


def test_garbage_confidence_becomes_zero():
    raw = json.dumps({"action": "buy", "ticker": TICKER, "confidence": "very high"})
    assert parse_proposal(raw).confidence == dec(0)


def test_null_ticker_is_none():
    assert parse_proposal(json.dumps({"action": "hold", "ticker": None})).ticker is None


def test_negative_size_is_dropped_rather_than_flipping_the_side():
    raw = json.dumps({"action": "buy", "ticker": TICKER, "notional_or_qty": -50, "confidence": 1})
    proposal = parse_proposal(raw)
    assert proposal.notional is None
    assert proposal.quantity is None


def test_nonsense_price_is_ignored():
    raw = json.dumps({"action": "buy", "ticker": TICKER, "price": 0, "confidence": 1})
    assert parse_proposal(raw).price is None


def test_reasoning_is_truncated():
    raw = json.dumps({"action": "hold", "reasoning": "x" * 5000})
    assert len(parse_proposal(raw).reasoning) <= 600


def test_two_json_objects_takes_the_first():
    raw = '{"action": "hold", "confidence": 1}\n{"action": "buy", "ticker": "X"}'
    assert parse_proposal(raw).action == "hold"


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def test_prompt_lists_only_allow_listed_tickers():
    config = make_config()
    prompt = build_prompt(
        config, make_account(cash=50), snapshot(), trades_today=0, day_pnl=dec(0)
    )
    assert TICKER in prompt
    assert OTHER in prompt
    assert "TSLA" not in prompt


def test_prompt_states_the_caps_and_the_budget_used():
    config = make_config(max_capital=50)
    prompt = build_prompt(
        config, make_account(cash=50), snapshot(), trades_today=3, day_pnl=dec("-1.25")
    )
    assert "50.00" in prompt
    assert "12.50" in prompt  # per-trade cap
    assert "3 of 5" in prompt
    assert "-1.25" in prompt


def test_prompt_marks_tickers_with_no_price_as_untradeable():
    config = make_config()
    prompt = build_prompt(
        config,
        make_account(cash=50),
        snapshot(prices={TICKER: dec(10)}, missing=[OTHER]),
        trades_today=0,
        day_pnl=dec(0),
    )
    assert "NO PRICE AVAILABLE" in prompt


def test_prompt_shows_open_positions():
    config = make_config()
    account = make_account(cash=40, positions=[make_position(quantity=1, current_price=10)])
    prompt = build_prompt(config, account, snapshot(), trades_today=0, day_pnl=dec(0))
    assert "held 1" in prompt


def test_prompt_is_deterministic_for_the_same_inputs():
    config = make_config()
    snap = snapshot()
    account = make_account(cash=50)
    first = build_prompt(config, account, snap, trades_today=0, day_pnl=dec(0))
    second = build_prompt(config, account, snap, trades_today=0, day_pnl=dec(0))
    assert first == second


# --------------------------------------------------------------------------- #
# advise()
# --------------------------------------------------------------------------- #


def test_stub_provider_always_holds():
    config = make_config()
    result = advise(
        StubProvider(), config, make_account(), snapshot(), trades_today=0, day_pnl=dec(0)
    )
    assert result.proposal.action == "hold"
    assert result.error is None
    assert result.prompt


def test_a_provider_failure_degrades_to_hold_rather_than_crashing():
    class Broken:
        name = "broken"
        model = "none"

        def complete(self, system, user):
            from t212bot.ai_advisor import ProviderError

            raise ProviderError("network down")

    result = advise(
        Broken(), make_config(), make_account(), snapshot(), trades_today=0, day_pnl=dec(0)
    )
    assert result.proposal.action == "hold"
    assert result.error == "network down"
    assert "AI unavailable" in result.proposal.reasoning


def test_the_raw_response_is_preserved_for_the_audit_log():
    provider = StubProvider(response='{"action": "hold", "reasoning": "verbatim"}')
    result = advise(
        provider, make_config(), make_account(), snapshot(), trades_today=0, day_pnl=dec(0)
    )
    assert result.raw_response == '{"action": "hold", "reasoning": "verbatim"}'


def test_openrouter_requires_a_key():
    import pytest

    from t212bot.ai_advisor import ProviderError

    with pytest.raises(ProviderError, match="OPENROUTER_API_KEY"):
        OpenRouterProvider(api_key="", model="openrouter/free")


# --------------------------------------------------------------------------- #
# OpenRouter provider: model fallback chain, JSON-mode retry, reasoning traces
# --------------------------------------------------------------------------- #

import json as _json

import httpx
import pytest

from t212bot.ai_advisor import ProviderError, _extract_json


def _provider(handler, models):
    return OpenRouterProvider(
        api_key="k",
        models=models,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _ok_body() -> dict:
    return {"choices": [{"message": {"content": '{"action": "hold", "confidence": 1}'}}]}


def test_openrouter_falls_back_to_the_next_model_on_a_rate_limit():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        model = _json.loads(request.content)["model"]
        seen.append(model)
        if model == "first/free":
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, json=_ok_body())

    provider = _provider(handler, ["first/free", "second/free"])
    assert provider.complete("sys", "user") == '{"action": "hold", "confidence": 1}'
    assert provider.last_model == "second/free"
    assert provider.last_http_calls == 2
    assert seen == ["first/free", "second/free"]


def test_openrouter_raises_only_when_every_model_fails():
    provider = _provider(lambda r: httpx.Response(429), ["a/free", "b/free"])
    with pytest.raises(ProviderError, match="every OpenRouter model failed"):
        provider.complete("sys", "user")


def test_openrouter_retries_without_json_mode_on_a_400():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        calls.append("response_format" in body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format unsupported")
        return httpx.Response(200, json=_ok_body())

    provider = _provider(handler, ["m/free"])
    assert provider.complete("s", "u").startswith("{")
    assert calls == [True, False]
    # It remembers, so a second call skips structured mode entirely.
    provider.complete("s", "u")
    assert calls == [True, False, False]


def _truncated_body(content: str | None, reasoning: str | None = None) -> dict:
    message: dict = {"content": content}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return {"choices": [{"message": message, "finish_reason": "length"}]}


def test_openrouter_retries_with_a_bigger_budget_when_it_runs_out_of_tokens():
    budgets = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = _json.loads(request.content)
        budgets.append(body["max_tokens"])
        if len(budgets) == 1:
            return httpx.Response(200, json=_truncated_body('{"action": "buy"'))
        return httpx.Response(200, json=_ok_body())

    provider = OpenRouterProvider(
        api_key="k",
        models=["m/free"],
        max_tokens=1024,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.complete("s", "u") == '{"action": "hold", "confidence": 1}'
    assert budgets == [1024, 4096]


def test_openrouter_returns_the_truncated_text_when_the_bigger_budget_also_runs_out():
    # Better a salvageable fragment than nothing: parse_proposal recovers the
    # fields that did arrive.
    handler = lambda r: httpx.Response(  # noqa: E731
        200, json=_truncated_body('{"action": "sell", "ticker": "X_EQ",')
    )
    provider = _provider(handler, ["m/free"])
    assert provider.complete("s", "u").startswith('{"action": "sell"')
    assert provider.last_http_calls == 2


def test_openrouter_names_the_token_ceiling_when_nothing_at_all_came_back():
    provider = _provider(lambda r: httpx.Response(200, json=_truncated_body(None)), ["m/free"])
    with pytest.raises(ProviderError, match="ran out of tokens"):
        provider.complete("s", "u")


def test_extract_json_strips_a_reasoning_trace():
    raw = "<think>The user wants me to {consider} things.</think>\n{\"action\": \"buy\"}"
    assert _extract_json(raw)["action"] == "buy"


# --------------------------------------------------------------------------- #
# OmniRoute provider: same wire format as OpenRouter, its own key/base_url
# --------------------------------------------------------------------------- #


def test_omniroute_requires_a_key():
    with pytest.raises(ProviderError, match="OMNIROUTE_API_KEY"):
        OmniRouteProvider(api_key="", base_url="http://127.0.0.1:20128/v1", model="default")


def test_omniroute_requires_a_base_url():
    with pytest.raises(ProviderError, match="base_url"):
        OmniRouteProvider(api_key="k", base_url="", model="default")


def test_omniroute_hits_its_own_base_url_and_reports_its_own_name():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_ok_body())

    provider = OmniRouteProvider(
        api_key="k",
        base_url="http://127.0.0.1:20128/v1",
        model="default",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    assert provider.name == "omniroute"
    assert provider.complete("sys", "user") == '{"action": "hold", "confidence": 1}'
    assert seen == ["http://127.0.0.1:20128/v1/chat/completions"]


def test_omniroute_raises_only_when_every_model_fails():
    provider = OmniRouteProvider(
        api_key="k",
        base_url="http://127.0.0.1:20128/v1",
        models=["a", "b"],
        client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429))),
    )
    with pytest.raises(ProviderError, match="every OmniRoute model failed"):
        provider.complete("sys", "user")


def test_signals_appear_in_the_prompt_when_supplied():
    from t212bot.config import IndicatorConfig
    from t212bot.indicators import compute_all

    config = make_config()
    snap = snapshot()
    signals = compute_all(config.watchlist, snap.quotes, snap.histories, IndicatorConfig(min_bars=5))
    prompt = build_prompt(
        config, make_account(cash=50), snap,
        trades_today=0, day_pnl=dec(0), signals=signals, regime="risk_on",
    )
    assert "TECHNICAL SIGNALS" in prompt
    assert "MARKET REGIME: risk on" in prompt
    assert "trend " in prompt
