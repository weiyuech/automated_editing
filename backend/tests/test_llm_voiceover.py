import pytest

from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.llm import (
    LLMService,
    VOICEOVER_SYSTEM_PROMPT,
    VOICEOVER_TARGET_SYSTEM_PROMPT,
)


def _service(tmp_path, speed=1.0, enabled=True):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "llm": {"enabled": enabled, "api_key": "k", "model": "m"},
        "tts": {"speed_ratio": speed},
    })
    return LLMService(settings)


def _capture(service):
    captured = {}

    async def fake_chat(cfg, *, system, user, max_tokens, temperature):
        captured.update(system=system, user=user, max_tokens=max_tokens, temperature=temperature)
        return "稿子"

    service._chat = fake_chat
    return captured


@pytest.mark.asyncio
async def test_no_target_keeps_original_prompt(tmp_path):
    service = _service(tmp_path)
    captured = _capture(service)
    out = await service.draft_voiceover("六和桥OPC，适合创业")
    assert out == "稿子"
    assert captured["system"] == VOICEOVER_SYSTEM_PROMPT
    assert "目标字数" not in captured["user"]
    assert captured["temperature"] == 0.55
    assert captured["max_tokens"] == 380


@pytest.mark.asyncio
async def test_target_seconds_embeds_char_count_in_user_message(tmp_path):
    service = _service(tmp_path, speed=1.0)
    captured = _capture(service)
    await service.draft_voiceover("六和桥OPC，适合创业", 30)
    # 30s x 4.0 x 1.0 = 120 字, embedded in the *user* message, length-first system prompt.
    assert captured["system"] == VOICEOVER_TARGET_SYSTEM_PROMPT
    assert "目标字数：约 120 字" in captured["user"]
    assert "六和桥OPC，适合创业" in captured["user"]
    assert captured["temperature"] == 0.7
    assert captured["max_tokens"] == 480  # min(2000, max(380, 120*4))


def test_target_chars_scales_with_speed_ratio(tmp_path):
    assert _service(tmp_path, speed=1.0)._target_chars(30) == 120
    assert _service(tmp_path, speed=2.0)._target_chars(30) == 240
    assert _service(tmp_path, speed=1.0)._target_chars(10) == 40
    assert _service(tmp_path)._target_chars(None) is None
    assert _service(tmp_path)._target_chars(0) is None


@pytest.mark.asyncio
async def test_disabled_llm_returns_raw_text_and_ignores_target(tmp_path):
    service = _service(tmp_path, enabled=False)
    assert await service.draft_voiceover("  原文  ", 30) == "原文"
