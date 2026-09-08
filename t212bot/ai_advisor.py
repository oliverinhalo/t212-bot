"""Ask an LLM for a buy/hold/sell recommendation.

The advisor's output is a *proposal*, never an instruction. It is parsed
defensively, clamped to the allow-list by risk_manager, and can only ever be
shrunk from here. Anything unparseable becomes a ``hold``.

Providers are behind one small interface, so switching between OpenRouter,
OmniRoute and the Anthropic API is a config change (``ai.provider``) rather
than a code change. All are given the same prompt and all return raw text,
which is stored verbatim in the audit log.
"""

from __future__ import annotations

import json
import logging
import re
import time
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence

import httpx

from .config import AppConfig
from .indicators import TechnicalSignals, summary_lines
from .market_data import Snapshot
from .models import (
    ZERO,
    AccountState,
    AIResult,
    Proposal,
    maybe_dec,
    money,
)

log = logging.getLogger(__name__)

MAX_REASONING_CHARS = 600

SYSTEM_PROMPT = """\
You are the analyst for a small, active, short-term stock/ETF bot. It trades \
frequently in small size: capital preservation matters, but so does actually \
trading — each position is tiny, so the cost of being wrong on any one cycle is \
small, and sitting in "hold" every cycle when the data supports a trade wastes \
the whole point of running this often.

Rules you must follow:
1. You may only ever name a ticker from the ALLOW-LIST given in the message. \
Never invent, substitute, or guess a ticker.
2. You may recommend exactly one action per cycle: buy, sell, or hold.
3. This is a short-term, high-turnover strategy: propose a buy or sell whenever \
the technical signals reasonably support one, even a modest one. Do not default \
to "hold" out of caution alone — only hold when the data is genuinely mixed or \
gives no lean either way.
4. Every buy or sell should be sized within the LIMITS given in the message — \
between the stated minimum order and the stated per-trade cap. Never propose \
more than the per-trade cap, never propose below the minimum order, and never \
propose selling more than is actually held.
5. You cannot short, use leverage, or trade anything other than the listed \
instruments. Do not suggest them.
6. A TECHNICAL SIGNALS block and a MARKET REGIME line may be provided. They are \
deterministic, computed locally from daily closes. Treat them as real input to \
your decision, not decoration: a "bullish" tag with a decent score is a \
legitimate reason to buy small, and a "bearish" tag on something you hold is a \
legitimate reason to trim or exit. In a "defensive" regime, size and lean \
smaller rather than skipping the cycle entirely.

Reply with a single JSON object and nothing else. No prose, no markdown fences.

{
  "action": "buy" | "sell" | "hold",
  "ticker": "<exact ticker from the allow-list, or null when holding>",
  "notional_or_qty": <number, or null when holding>,
  "size_unit": "gbp" | "shares",
  "price": <the price per share you are assuming, or null>,
  "confidence": <number between 0 and 1>,
  "reasoning": "<one or two sentences, under 300 characters>"
}

"size_unit" says how to read "notional_or_qty": "gbp" means an amount of money \
to spend or raise, "shares" means a number of shares. Prefer "gbp" for buys.
A proposal that breaks any rule above is discarded by a separate risk system, \
and a discarded proposal is a wasted cycle."""


# Open-universe variant: the AI may name any Trading212 instrument, not just the
# watch-list. Rule 1 and 5 change; everything else is identical.
SYSTEM_PROMPT_OPEN = SYSTEM_PROMPT.replace(
    "1. You may only ever name a ticker from the ALLOW-LIST given in the message. "
    "Never invent, substitute, or guess a ticker.",
    "1. You may name any stock or ETF that trades on Trading212. Give its exact "
    "Trading212 ticker when you know it (e.g. AAPL_US_EQ, NVDA_US_EQ, SAPd_EQ); "
    "otherwise give a plain company name or symbol (e.g. \"Apple\", \"NVDA\") and "
    "it will be resolved. Never invent a ticker format. If a name cannot be "
    "resolved to a real instrument the proposal is discarded.",
).replace(
    "5. You cannot short, use leverage, or trade anything other than the listed "
    "instruments. Do not suggest them.",
    "5. You cannot short, use leverage, or trade options/derivatives. Cash equity "
    "and ETFs only. Foreign-currency instruments are converted to GBP for every "
    "limit, so size in GBP.",
).replace(
    '"ticker": "<exact ticker from the allow-list, or null when holding>"',
    '"ticker": "<Trading212 ticker or a resolvable name/symbol, or null when holding>"',
)


class ProviderError(Exception):
    """The provider could not be reached or returned nothing usable."""


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system: str, user: str) -> str: ...


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #


class StubProvider:
    """Always holds. Lets the whole pipeline be exercised with no API calls."""

    name = "stub"

    def __init__(self, model: str = "stub", response: str | None = None):
        self.model = model
        self._response = response

    def complete(self, system: str, user: str) -> str:
        if self._response is not None:
            return self._response
        return json.dumps(
            {
                "action": "hold",
                "ticker": None,
                "notional_or_qty": None,
                "size_unit": "gbp",
                "price": None,
                "confidence": 1.0,
                "reasoning": "Stub provider: no live model was called.",
            }
        )


class OpenRouterProvider:
    """OpenAI-compatible chat completions against OpenRouter.

    Given a chain of model slugs, each is tried in order until one answers, so a
    single free model being rate-limited or retired does not lose the cycle.
    ``openrouter/free`` auto-routes to whatever free model fits and is the
    natural last entry in the chain.

    ``last_http_calls`` records how many HTTP requests the most recent
    ``complete`` made — the bot meters this against the free-tier daily cap.
    ``last_model`` is the slug that actually answered, for the audit log.

    Also the base class for other OpenAI-compatible chat-completions backends
    (see ``OmniRouteProvider``) — the wire format is identical, only the key,
    default host and error labels differ.
    """

    name = "openrouter"
    _key_env_var = "OPENROUTER_API_KEY"
    _error_label = "OpenRouter"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        base_url: str = "https://openrouter.ai/api/v1",
        max_tokens: int = 1024,
        timeout: float = 60.0,
        app_url: str = "",
        app_title: str = "t212-bot",
        client: httpx.Client | None = None,
        models: "Sequence[str] | None" = None,
    ):
        if not api_key:
            raise ProviderError(f"{self._key_env_var} is not set")
        chain = list(models) if models else ([model] if model else ["openrouter/free"])
        self.models = [m.strip() for m in chain if m and m.strip()]
        if not self.models:
            raise ProviderError(f"{self._error_label} needs at least one model slug")
        self.model = self.models[0]
        self.last_model = self.models[0]
        self.last_http_calls = 0
        self._base_url = base_url.rstrip("/")
        self._max_tokens = max_tokens
        self._no_json_mode: set[str] = set()
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if app_url:
            self._headers["HTTP-Referer"] = app_url
        if app_title:
            self._headers["X-Title"] = app_title
        self._owns_client = client is None
        self._http = client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def complete(self, system: str, user: str) -> str:
        self.last_http_calls = 0
        errors: list[str] = []
        for model in self.models:
            try:
                content = self._complete_one(model, system, user)
                self.last_model = model
                return content
            except ProviderError as exc:
                errors.append(f"{model}: {exc}")
                log.warning("%s model %s unavailable: %s", self._error_label, model, exc)
        raise ProviderError(f"every {self._error_label} model failed — " + " | ".join(errors))

    def _complete_one(self, model: str, system: str, user: str) -> str:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": self._max_tokens,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        want_json = model not in self._no_json_mode
        if want_json:
            body["response_format"] = {"type": "json_object"}

        for _ in range(2):
            self.last_http_calls += 1
            try:
                response = self._http.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers,
                    json=body,
                )
            except httpx.HTTPError as exc:
                raise ProviderError(f"request failed: {exc}") from exc

            if response.status_code == 400 and "response_format" in body:
                # This model rejects structured-output mode. Drop it, remember
                # not to ask again, and retry once in plain-text mode.
                self._no_json_mode.add(model)
                body.pop("response_format", None)
                continue
            if response.status_code == 429:
                raise ProviderError("rate limit (free tier is ~20/min, ~50/day)")
            if response.status_code >= 400:
                raise ProviderError(f"HTTP {response.status_code}: {response.text[:300]}")

            payload = response.json()
            choices = payload.get("choices") or []
            if not choices:
                raise ProviderError(f"no choices: {str(payload)[:200]}")
            message = choices[0].get("message") or {}
            content = message.get("content") or message.get("reasoning")
            if not content:
                raise ProviderError(
                    f"{self._error_label} returned an empty message. "
                    f"model={payload.get('model')!r}, "
                    f"message={message!r}, "
                    f"usage={payload.get('usage')!r}"
                )
            return content

        raise ProviderError("structured-output retry exhausted")


class OmniRouteProvider(OpenRouterProvider):
    """An OmniRoute instance — a self-hosted or private OpenAI-compatible router.

    Wire-compatible with OpenRouter's chat completions endpoint (same request
    and response shape, same model-fallback-chain and JSON-mode-retry
    behaviour), just pointed at your own instance instead of openrouter.ai —
    e.g. ``http://127.0.0.1:20128/v1`` or ``https://omni.jacoblevy.co.uk/v1``.
    """

    name = "omniroute"
    _key_env_var = "OMNIROUTE_API_KEY"
    _error_label = "OmniRoute"

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        base_url: str = "",
        max_tokens: int = 1024,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
        models: "Sequence[str] | None" = None,
    ):
        if not base_url:
            raise ProviderError("OMNIROUTE_BASE_URL (ai.omniroute.base_url) is not set")
        super().__init__(
            api_key=api_key,
            model=model,
            base_url=base_url,
            max_tokens=max_tokens,
            timeout=timeout,
            client=client,
            models=models,
        )


class AnthropicProvider:
    """The Anthropic Messages API, via the official SDK.

    Haiku is the sensible default here: this is a short prompt, a tiny JSON
    reply, and a few calls a day.
    """

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        model: str = "claude-haiku-4-5",
        max_tokens: int = 1024,
        timeout: float = 60.0,
        client: Any = None,
    ):
        self.model = model
        self._max_tokens = max_tokens
        if client is not None:
            self._client = client
            return
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on install
            raise ProviderError(
                "the anthropic package is not installed — run: pip install 'anthropic>=1.0,<2'"
            ) from exc
        if not api_key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)

    def complete(self, system: str, user: str) -> str:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 - normalise every SDK error
            raise ProviderError(f"Anthropic request failed: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise ProviderError(f"Anthropic declined the request: {details}")

        text = "".join(
            block.text for block in response.content if getattr(block, "type", "") == "text"
        )
        if not text.strip():
            raise ProviderError("Anthropic returned no text content")
        return text


def build_provider(config: AppConfig, *, app_url: str = "", app_title: str = "t212-bot") -> Provider:
    """Construct the provider named by ``ai.provider`` (already resolved from 'auto')."""
    if config.ai.provider == "stub":
        return StubProvider()
    if config.ai.provider == "anthropic":
        return AnthropicProvider(
            api_key=config.secrets.anthropic_api_key,
            model=config.ai.anthropic_model,
            max_tokens=config.ai.max_tokens,
            timeout=config.ai.timeout_seconds,
        )
    if config.ai.provider == "openrouter":
        return OpenRouterProvider(
            api_key=config.secrets.openrouter_api_key,
            models=config.ai.openrouter_models,
            base_url=config.ai.openrouter_base_url,
            max_tokens=config.ai.max_tokens,
            timeout=config.ai.timeout_seconds,
            app_url=app_url,
            app_title=app_title,
        )
    if config.ai.provider == "omniroute":
        return OmniRouteProvider(
            api_key=config.secrets.omniroute_api_key,
            models=config.ai.omniroute_models,
            base_url=config.ai.omniroute_base_url,
            max_tokens=config.ai.max_tokens,
            timeout=config.ai.timeout_seconds,
        )
    raise ProviderError(f"unknown ai.provider: {config.ai.provider!r}")


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #


def build_prompt(
    config: AppConfig,
    account: AccountState,
    snapshot: Snapshot,
    *,
    trades_today: int,
    day_pnl: Decimal,
    signals: Mapping[str, TechnicalSignals] | None = None,
    regime: str = "",
    open_universe: bool = False,
) -> str:
    """Compact, deterministic prompt.

    In allow-list mode only watch-list tickers appear. In open-universe mode the
    watch-list is presented as the set with full local data, and the AI is told
    it may name others.
    """
    signals = signals or {}
    lines: list[str] = []

    lines.append("ACCOUNT (GBP)")
    lines.append(f"  cash available:      {money(account.cash)}")
    lines.append(f"  deployed in positions: {money(account.invested)}")
    lines.append(f"  total equity:        {money(account.equity)}")
    lines.append(f"  P&L today:           {money(day_pnl)}")
    lines.append("")

    lines.append("LIMITS (enforced outside your control; proposals that breach them are dropped)")
    lines.append(f"  max capital deployed:  {money(config.capital.max_capital)}")
    lines.append(
        f"  max per trade:         {money(config.capital.per_trade_cap)} "
        f"({config.capital.per_trade_cap_pct}% of capital)"
    )
    lines.append(f"  max per position:      {money(config.capital.max_position_value)}")
    lines.append(f"  min order:             {money(config.capital.min_order)}")
    lines.append(
        f"  trades used today:     {trades_today} of {config.risk.max_trades_per_day}"
    )
    lines.append(f"  minimum confidence:    {config.risk.min_confidence}")
    lines.append("")

    lines.append("CURRENT POSITIONS")
    if account.positions:
        for position in account.positions:
            lines.append(
                f"  {position.ticker}: {position.quantity} shares, avg {money(position.average_price)}, "
                f"now {money(position.current_price)}, value {money(position.value)}, "
                f"unrealised {money(position.unrealised_pnl)}"
            )
    else:
        lines.append("  (none)")
    lines.append("")

    if open_universe:
        lines.append("INSTRUMENTS WITH FULL LOCAL DATA (prefer these; you may also name others)")
    else:
        lines.append("ALLOW-LIST — you may name no other ticker")
    for item in config.watchlist:
        quote = snapshot.quotes.get(item.ticker)
        if quote is None:
            lines.append(f"  {item.ticker} ({item.name}): NO PRICE AVAILABLE — cannot be traded")
            continue
        history = snapshot.histories.get(item.ticker)
        parts = [f"last {money(quote.price)} {quote.currency}"]
        if history is not None:
            for label, days in (("1d", 1), ("5d", 5), ("20d", 20)):
                change = history.pct_change(days)
                if change is not None:
                    parts.append(f"{label} {change:+.2f}%")
        held = account.quantity_of(item.ticker)
        if held > ZERO:
            parts.append(f"held {held}")
        sig = signals.get(item.ticker)
        if sig is not None:
            parts.append(f"trend {sig.trend} (score {sig.score:+.2f})")
        lines.append(f"  {item.ticker} ({item.name}): " + ", ".join(parts))

    if open_universe:
        lines.append(
            "  You may also propose any other Trading212-listed stock or ETF by "
            "ticker or name. Those have no local signals and are priced in GBP "
            "on demand; prefer the instruments above unless you have a specific "
            "reason to go elsewhere."
        )

    technical = summary_lines(signals, regime) if signals else []
    if technical:
        lines.append("")
        lines.extend(technical)

    if snapshot.histories:
        lines.append("")
        lines.append("RECENT DAILY CLOSES (oldest first)")
        for item in config.watchlist:
            history = snapshot.histories.get(item.ticker)
            if not history or not history.bars:
                continue
            closes = ", ".join(str(money(bar.close)) for bar in history.bars[-10:])
            lines.append(f"  {item.ticker}: {closes}")

    lines.append("")
    lines.append("Respond with the JSON object described in your instructions, and nothing else.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_THINK = re.compile(r"<(think|reasoning|thinking)>.*?</\1>", re.DOTALL | re.IGNORECASE)


def _extract_json(raw: str) -> Mapping[str, Any]:
    """Pull one JSON object out of a model response.

    Models wrap JSON in fences, prefix it with "Here you go:", or emit two
    objects. Rather than trust any of that, find the first balanced object and
    parse it. Failure raises, and the caller turns that into a hold.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("empty response")

    # Reasoning models emit a <think>…</think> trace before the answer, and it
    # can contain braces that would derail the balanced-object scan below.
    text = _THINK.sub("", text).strip()
    if not text:
        raise ValueError("response was only a reasoning trace")

    fenced = _FENCE.search(text)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, Mapping):
            return parsed
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        candidate = json.loads(text[start : index + 1])
                    except json.JSONDecodeError:
                        break
                    if isinstance(candidate, Mapping):
                        return candidate
                    break
        start = text.find("{", start + 1)

    raise ValueError(f"no JSON object found in response: {text[:200]!r}")


def parse_proposal(raw: str) -> Proposal:
    """Turn a raw model response into a Proposal. Never raises."""
    try:
        data = _extract_json(raw)
    except ValueError as exc:
        return Proposal(action="hold", reasoning=f"unparseable AI response: {exc}")

    action = str(data.get("action", "hold")).strip().lower()
    if action not in ("buy", "sell", "hold"):
        return Proposal(action="hold", reasoning=f"unrecognised action {action!r}; holding")

    ticker_raw = data.get("ticker")
    ticker = str(ticker_raw).strip() if ticker_raw not in (None, "", "null") else None

    reasoning = str(data.get("reasoning", "") or "")[:MAX_REASONING_CHARS]

    try:
        confidence = maybe_dec(data.get("confidence")) or ZERO
    except ValueError:
        confidence = ZERO
    confidence = min(max(confidence, ZERO), Decimal(1))

    try:
        price = maybe_dec(data.get("price"))
    except ValueError:
        price = None
    if price is not None and price <= ZERO:
        price = None

    notional: Decimal | None = None
    quantity: Decimal | None = None
    if action in ("buy", "sell"):
        size = data.get("notional_or_qty")
        if size is None:
            size = data.get("notional") if data.get("notional") is not None else data.get("quantity")
        try:
            size_dec = maybe_dec(size)
        except ValueError:
            size_dec = None

        if size_dec is not None and size_dec > ZERO:
            unit = str(data.get("size_unit", "gbp")).strip().lower()
            if unit in ("shares", "share", "qty", "quantity", "units"):
                quantity = size_dec
            else:
                # Default to GBP: the prompt asks for money, and a money figure
                # misread as shares would be a far larger order than intended.
                notional = size_dec

    return Proposal(
        action=action,  # type: ignore[arg-type]
        ticker=ticker,
        notional=notional,
        quantity=quantity,
        price=price,
        confidence=confidence,
        reasoning=reasoning,
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def advise(
    provider: Provider,
    config: AppConfig,
    account: AccountState,
    snapshot: Snapshot,
    *,
    trades_today: int,
    day_pnl: Decimal,
    signals: Mapping[str, TechnicalSignals] | None = None,
    regime: str = "",
    open_universe: bool = False,
    system_prompt: str | None = None,
) -> AIResult:
    """Run one advisory call. A provider failure degrades to ``hold``, not a crash."""
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_OPEN if open_universe else SYSTEM_PROMPT
    prompt = build_prompt(
        config,
        account,
        snapshot,
        trades_today=trades_today,
        day_pnl=day_pnl,
        signals=signals,
        regime=regime,
        open_universe=open_universe,
    )
    started = time.monotonic()

    try:
        raw = provider.complete(system_prompt, prompt)
        error = None
    except ProviderError as exc:
        log.error("AI provider failed: %s", exc)
        raw = ""
        error = str(exc)

    latency_ms = int((time.monotonic() - started) * 1000)

    if error is not None:
        proposal = Proposal(action="hold", reasoning=f"AI unavailable: {error}")
    else:
        proposal = parse_proposal(raw)

    return AIResult(
        proposal=proposal,
        provider=getattr(provider, "name", "unknown"),
        model=getattr(provider, "last_model", getattr(provider, "model", "unknown")),
        prompt=prompt,
        raw_response=raw,
        latency_ms=latency_ms,
        error=error,
        http_calls=int(getattr(provider, "last_http_calls", 1)),
    )


__all__ = [
    "SYSTEM_PROMPT",
    "SYSTEM_PROMPT_OPEN",
    "Provider",
    "ProviderError",
    "StubProvider",
    "OpenRouterProvider",
    "OmniRouteProvider",
    "AnthropicProvider",
    "build_provider",
    "build_prompt",
    "parse_proposal",
    "advise",
]
