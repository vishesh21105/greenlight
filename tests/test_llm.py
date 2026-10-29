import httpx
import openai

from app.llm import OpenAICompatibleProvider, _retry_delay


def test_retry_delay_uses_server_hint_or_backoff():
    assert _retry_delay(Exception("Please retry in 12.5s."), 0) == 13.5
    assert _retry_delay(Exception("'retryDelay': '30s'"), 0) == 31.0
    assert _retry_delay(Exception("too many requests"), 0) == 10.0
    assert _retry_delay(Exception("too many requests"), 5) == 90.0


def _rate_limit_error() -> openai.RateLimitError:
    req = httpx.Request("POST", "https://example.com/v1/chat/completions")
    return openai.RateLimitError("Error code: 429 retry in 2s", response=httpx.Response(429, request=req), body=None)


class _Msg:
    content = "ok"
    tool_calls = None


class _Resp:
    choices = [type("C", (), {"message": _Msg()})()]
    usage = type("U", (), {"prompt_tokens": 5, "completion_tokens": 1})()


def test_rate_limits_are_retried_then_succeed():
    sleeps = []
    provider = OpenAICompatibleProvider("k", "m", "https://example.com/v1", sleep=sleeps.append)
    calls = {"n": 0}

    def create(**kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise _rate_limit_error()
        return _Resp()

    provider.client.chat.completions.create = create
    resp = provider.chat([{"role": "user", "content": "hi"}])
    assert resp.text == "ok" and calls["n"] == 3
    assert sleeps == [3.0, 3.0]


def test_gives_up_after_max_retries():
    provider = OpenAICompatibleProvider("k", "m", "https://example.com/v1", max_rate_retries=2, sleep=lambda s: None)

    def create(**kwargs):
        raise _rate_limit_error()

    provider.client.chat.completions.create = create
    try:
        provider.chat([{"role": "user", "content": "hi"}])
        raise AssertionError("should have raised")
    except openai.RateLimitError:
        pass


def test_min_interval_spaces_calls_out():
    sleeps = []
    provider = OpenAICompatibleProvider("k", "m", "https://example.com/v1", min_interval=7, sleep=sleeps.append)
    provider.client.chat.completions.create = lambda **kw: _Resp()
    provider.chat([{"role": "user", "content": "a"}])
    provider.chat([{"role": "user", "content": "b"}])
    assert len(sleeps) == 1 and 6.5 < sleeps[0] <= 7


def test_switches_to_fallback_model_when_quota_is_used_up():
    provider = OpenAICompatibleProvider("k", "main-model", "https://example.com/v1", sleep=lambda s: None,
                                        fallback_models=["backup-model"])
    used = []

    def create(**kwargs):
        used.append(kwargs["model"])
        if kwargs["model"] == "main-model":
            raise _rate_limit_error()
        return _Resp()

    provider.client.chat.completions.create = create
    assert provider.chat([{"role": "user", "content": "hi"}]).text == "ok"
    assert used == ["main-model"] * 4 + ["backup-model"]
    provider.chat([{"role": "user", "content": "again"}])
    assert used[-1] == "backup-model"          # stays on the working model


def test_tool_calls_are_sent_back_exactly_as_received():
    """Newer Gemini models reject the next request unless hidden fields come back unchanged."""
    from app.agent.loop import _assistant_message
    from app.llm import LLMResponse, ToolCall
    raw = {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": "{\"path\": \"a.py\"}"},
           "extra_content": {"google": {"thought_signature": "abc123"}}}
    msg = _assistant_message(LLMResponse(text=None, tool_calls=[ToolCall("c1", "read_file", {"path": "a.py"}, raw["function"]["arguments"], raw)]))
    assert msg["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "abc123"


def test_daily_quota_switches_model_without_waiting():
    req = httpx.Request("POST", "https://example.com/v1/chat/completions")
    daily = openai.RateLimitError("Error code: 429 - generate_content_free_tier_requests, limit: 20, "
                                  "description: Generate content requests per day limit exceeded. Please retry in 10m58s.",
                                  response=httpx.Response(429, request=req), body=None)
    sleeps, used = [], []
    provider = OpenAICompatibleProvider("k", "main", "https://example.com/v1", sleep=sleeps.append, fallback_models=["backup"])

    def create(**kwargs):
        used.append(kwargs["model"])
        if kwargs["model"] == "main":
            raise daily
        return _Resp()

    provider.client.chat.completions.create = create
    provider.chat([{"role": "user", "content": "hi"}])
    assert used == ["main", "backup"] and sleeps == []
    assert _retry_delay(daily, 0) == 90.0          # 10m58s is capped
