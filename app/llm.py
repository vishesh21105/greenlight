"""One small door to the AI model.

The rest of GreenLight only ever calls `provider.chat(...)`. Swapping the model or
the company behind it is a configuration change, not a code change.

`OpenAICompatibleProvider` speaks the widely supported "chat completions" format,
so it works with OpenAI, Azure OpenAI, Google Gemini's OpenAI-compatible endpoint,
Groq, a local Ollama server and others: set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL.
"""
from __future__ import annotations

import json
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


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(self, api_key: str, model: str, base_url: str = "", timeout: float = 120.0) -> None:
        from openai import OpenAI  # imported here so tests do not need network setup
        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout, max_retries=2)

    def chat(self, messages, tools=None, max_tokens=1500, temperature=0.2) -> LLMResponse:
        kwargs = dict(model=self.model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        if tools:
            kwargs["tools"] = [{"type": "function", "function": t} for t in tools]
            kwargs["tool_choice"] = "auto"
        start = time.monotonic()
        resp = self.client.chat.completions.create(**kwargs)
        latency = int((time.monotonic() - start) * 1000)
        msg = resp.choices[0].message
        calls = []
        for tc in msg.tool_calls or []:
            raw = tc.function.arguments or "{}"
            calls.append(ToolCall(tc.id, tc.function.name, _parse_args(raw), raw))
        usage = resp.usage
        return LLMResponse(
            text=msg.content, tool_calls=calls,
            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency, model=self.model, provider=self.name,
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
        return OpenAICompatibleProvider(s.llm_api_key, s.llm_model, s.llm_base_url)
    return None
