"""One small door to the AI model.

The rest of GreenLight only ever calls `provider.chat(...)`. Swapping the model or
the company behind it is a configuration change, not a code change.

`OpenAICompatibleProvider` speaks the widely supported "chat completions" format,
so it works with OpenAI, Azure OpenAI, Google Gemini's OpenAI-compatible endpoint,
Groq, a local Ollama server and others: set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from app import config


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""
    # The tool call exactly as the model sent it. Some models (newer Gemini) attach hidden fields,
    # such as a "thought signature", that must be sent back unchanged in the next request.
    raw: Optional[dict] = None


@dataclass
class LLMResponse:
    text: Optional[str]
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    model: str = ""
    provider: str = ""


class LLMProvider(Protocol):
    name: str
    model: str

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
             max_tokens: int = 1500, temperature: float = 0.2) -> LLMResponse: ...


def estimate_cost(input_tokens: int, output_tokens: int) -> float:
    s = config.settings
    return (input_tokens * s.llm_input_price_per_m + output_tokens * s.llm_output_price_per_m) / 1_000_000


def _parse_args(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {"_invalid_arguments": raw}
    except json.JSONDecodeError:
        return {"_invalid_arguments": raw}


_RETRY_HINT = re.compile(r"retry(?:Delay)?[^0-9]{0,20}(?:(\d+)m)?(\d+(?:\.\d+)?)\s*s", re.I)
_DAILY = re.compile(r"per ?day|PerDay|daily", re.I)


def _error_text(exc: Exception) -> str:
    return f"{exc} {getattr(exc, 'body', '')}"


def _retry_delay(exc: Exception, attempt: int) -> float:
    """How long to wait after a 'too many requests' error: the server's hint, else exponential backoff."""
    m = _RETRY_HINT.search(_error_text(exc))
    if m:
        minutes = float(m.group(1) or 0)
        return min(minutes * 60 + float(m.group(2)) + 1.0, 90.0)
    return min(10.0 * (2 ** attempt), 90.0)


def _quota_used_up_for_today(exc: Exception) -> bool:
    """A per-day quota will not come back by waiting a minute: switch models instead."""
    text = _error_text(exc)
    if _DAILY.search(text):
        return True
    m = _RETRY_HINT.search(text)
    return bool(m and float(m.group(1) or 0) >= 5)


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, api_key: str, model: str, base_url: str = "", timeout: float = 120.0,
                 min_interval: float = 0.0, max_rate_retries: int = 5, reasoning_effort: str = "",
                 sleep: Callable[[float], None] = time.sleep, fallback_models: Optional[list[str]] = None) -> None:
        from openai import OpenAI  # imported here so tests do not need network setup
        # The first model is preferred; later ones are used when it runs out of quota.
        self.models = [model] + [m for m in (fallback_models or []) if m and m != model]
        self._model_idx = 0
        self.client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout, max_retries=1)
        self.min_interval = min_interval          # free tiers allow only a few calls per minute
        self.max_rate_retries = max_rate_retries
        self.reasoning_effort = reasoning_effort
        self._sleep = sleep
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.min_interval - (time.monotonic() - self._last_call)
        if wait > 0:
            self._sleep(wait)
        self._last_call = time.monotonic()

    @property
    def model(self) -> str:
        return self.models[self._model_idx]

    def _create(self, kwargs: dict):
        """Call the model; wait and retry on rate limits; move to the next model when one is exhausted."""
        from openai import APIConnectionError, InternalServerError, NotFoundError, RateLimitError
        while True:
            is_last_model = self._model_idx == len(self.models) - 1
            # Per-minute limits: wait and retry the same model a few times before switching.
            retries = self.max_rate_retries if is_last_model else 3
            for attempt in range(retries + 1):
                self._throttle()
                try:
                    return self.client.chat.completions.create(**{**kwargs, "model": self.model})
                except NotFoundError:
                    if is_last_model:
                        raise
                    break
                except (RateLimitError, InternalServerError, APIConnectionError) as exc:
                    daily = isinstance(exc, RateLimitError) and _quota_used_up_for_today(exc)
                    if daily and not is_last_model:
                        break                     # today's quota is gone: go straight to the next model
                    if attempt == retries or daily:
                        if is_last_model:
                            raise
                        break
                    self._sleep(_retry_delay(exc, attempt))
            self._model_idx += 1          # this model is exhausted for now; use the next one

    def chat(self, messages, tools=None, max_tokens=1500, temperature=0.2) -> LLMResponse:
        kwargs = dict(messages=messages, max_tokens=max_tokens, temperature=temperature)
        if self.reasoning_effort:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            kwargs["tool_choice"] = "auto"
        start = time.monotonic()
        resp = self._create(kwargs)
        latency = int((time.monotonic() - start) * 1000)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            try:
                raw_call = tc.model_dump(exclude_none=True)
            except Exception:
                raw_call = None
            calls.append(ToolCall(tc.id, tc.function.name, _parse_args(raw), raw, raw_call))
        usage = resp.usage
        return LLMResponse(
            text=msg.content, tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency, model=getattr(resp, "model", None) or self.model, provider=self.name,
        )


class ScriptedProvider:
    """A fake model that replays prepared answers. Used by tests and the offline demo.

    `script` is a list of LLMResponse objects, or a function(messages, tools) -> LLMResponse.
    """
    name = "scripted"

    def __init__(self, script: list[LLMResponse] | Callable[[list[dict], Optional[list[dict]]], LLMResponse],
                 model: str = "scripted-model") -> None:
        self.script = script
        self.model = model
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, max_tokens=1500, temperature=0.2) -> LLMResponse:
        self.calls.append([dict(m) for m in messages])
        if callable(self.script):
            resp = self.script(messages, tools)
        else:
            if not self.script:
                return LLMResponse(text="(script finished)", model=self.model, provider=self.name)
            resp = self.script.pop(0)
        resp.model = resp.model or self.model
        resp.provider = self.name
        return resp


def get_llm() -> Optional[LLMProvider]:
    """The configured model, or None if no model is set up (rules-only mode)."""
    s = config.settings
    if s.llm_provider == "openai_compatible" and s.llm_api_key and s.llm_model:
        return OpenAICompatibleProvider(s.llm_api_key, s.llm_model, s.llm_base_url,
                                        min_interval=s.llm_min_interval_seconds,
                                        reasoning_effort=s.llm_reasoning_effort,
                                        fallback_models=[m.strip() for m in s.llm_fallback_models.split(",")])
    return None
