import json
from types import SimpleNamespace

import httpx
import pytest

from automated_video_editing_backend.services import llm as module


def service_with_response(monkeypatch, data, status=200):
    requests, events = [], []
    original_client = httpx.AsyncClient

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(status, json=data)

    monkeypatch.setattr(
        module.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    monkeypatch.setattr(module, "log_event", lambda *args, **kwargs: events.append(kwargs))
    return module.LLMService(SimpleNamespace()), requests, events


def response(reason="stop", content="完整文案"):
    return {
        "id": "request-1",
        "model": "doubao-1-5-lite-32k-250115",
        "choices": [{"finish_reason": reason, "message": {"content": content}}],
        "usage": {
            "prompt_tokens": 80,
            "completion_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


async def chat(service, model="doubao-1-5-lite-32k-250115", json_output=True):
    return await service._chat(
        {"model": model, "api_key": "secret-key"},
        system="private prompt",
        user="private input",
        max_tokens=8192,
        temperature=0.2,
        json_output=json_output,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,json_output,expected",
    [
        ("doubao-1-5-lite-32k-250115", True, True),
        ("doubao-1-5-lite-32k-250115", False, False),
        ("unverified-model", True, False),
    ],
)
async def test_format_capability_and_metadata_without_private_content(
    monkeypatch, model, json_output, expected
):
    service, requests, events = service_with_response(monkeypatch, response())
    assert await chat(service, model, json_output) == "完整文案"
    assert len(requests) == 1
    assert (requests[0].get("response_format") == {"type": "json_object"}) is expected
    assert events[0]["completion_tokens"] == 30
    assert events[0]["reasoning_tokens"] == 0
    assert events[0]["response_id"] == "request-1"
    assert not any(
        value in json.dumps(events) for value in ("private prompt", "private input", "secret-key")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,content,message",
    [
        ("length", '{"sections":[', "长度上限"),
        ("content_filter", None, "审核"),
        ("tool_calls", None, "未返回完整"),
        (None, "看似完整", "未返回完整"),
        ("stop", None, "未返回完整"),
        ("stop", "  ", "未返回完整"),
    ],
)
async def test_incomplete_output_never_becomes_a_script_or_retries(
    monkeypatch, reason, content, message
):
    service, requests, _ = service_with_response(monkeypatch, response(reason, content))
    with pytest.raises(ValueError, match=message):
        await chat(service)
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_provider_error_does_not_retry(monkeypatch):
    service, requests, _ = service_with_response(
        monkeypatch, {"error": {"code": "RateLimitExceeded"}}, 429
    )
    with pytest.raises(ValueError, match="429"):
        await chat(service)
    assert len(requests) == 1


def test_output_budget_grows_and_is_bounded():
    assert module.narration_token_budget(30) == 2048
    assert module.narration_token_budget(1000) > module.narration_token_budget(30)
    assert module.narration_token_budget(4000, 12) == 8192
