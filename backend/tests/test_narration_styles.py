import json

import pytest
from pydantic import ValidationError

from automated_video_editing_backend.core.composition import (
    MappedNarrationRequest,
    NarrationAllocateRequest,
    NarrationPromptRequest,
)
from automated_video_editing_backend.core.models import VoiceoverDraftRequest
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.narration_prompts import composition_prompt
from automated_video_editing_backend.services.narration_styles import (
    STYLE_RULES,
    style_context,
    mapped_style_options,
)
from automated_video_editing_backend.services.grounded_narration import writing_options
from automated_video_editing_backend.services.settings import SettingsService


@pytest.mark.parametrize(
    "model",
    [
        NarrationPromptRequest,
        NarrationAllocateRequest,
        MappedNarrationRequest,
        VoiceoverDraftRequest,
    ],
)
def test_only_concrete_supported_styles_cross_the_api_boundary(model):
    for style in STYLE_RULES:
        assert model(text="原文", narration_style=style).narration_style == style
    assert model(text="原文").narration_style is None
    for style in ("auto", "unknown", ""):
        with pytest.raises(ValidationError):
            model(text="原文", narration_style=style)


@pytest.mark.parametrize("mode", ["single_recording", "multiple_recordings"])
def test_style_is_optional_and_never_overwrites_custom_system_or_fact_context(mode):
    context = dict(mode=mode, duration_seconds=20, windows=[], capture_notes=["完整备注"])
    request = NarrationPromptRequest(
        text="原文", instructions="自定义重点", system_prompt="我的规则"
    )
    original = composition_prompt(context, request)
    styled = composition_prompt(context, request.model_copy(update={"narration_style": "poetic"}))
    assert styled["system"] == original["system"] == "我的规则"
    assert styled["baseline_system"] == original["baseline_system"]
    payload = json.loads(styled["user"])
    style = payload.pop("旁白风格")
    assert style["名称"] == "诗意抒情"
    assert "没有看到实际图片或视频" in style["边界"]
    assert "不承诺停顿时长" in style["边界"]
    assert payload == json.loads(original["user"])
    if mode == "multiple_recordings":
        assert "完整备注" not in styled["user"]


@pytest.mark.asyncio
async def test_ordinary_preview_equals_submitted_prompt_and_style_off_is_unchanged(tmp_path):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"llm": {"enabled": True, "api_key": "k", "model": "m"}})
    service = LLMService(settings)
    actual = {}

    async def capture(_config, **prompt):
        actual.update(prompt)
        return "润色稿"

    service._chat = capture
    kwargs = dict(instructions="重点简洁", system_prompt="自定义规则", narration_style="concise")
    preview = service.voiceover_prompt("用户事实", **kwargs)
    await service.draft_voiceover("用户事实", **kwargs)
    assert actual == preview
    assert preview["system"] == "自定义规则"
    assert "简洁有力" in preview["user"]
    assert service.voiceover_prompt("用户事实", narration_style=None) == service.voiceover_prompt(
        "用户事实"
    )
    assert "旁白风格" not in service.voiceover_prompt("用户事实")["user"]


def test_examples_are_shared_separate_from_facts_and_disappear_with_style_off():
    request = NarrationAllocateRequest(text="只有购买2盒时才送茶杯", narration_style="classical")
    options = writing_options(request)
    key = "句式示例（不属于本次事实）"
    assert options[key] == mapped_style_options("classical")[key]
    options[key]["原文"].clear()
    assert mapped_style_options("classical")[key]["原文"]  # No shared mutable example.
    assert key not in style_context("classical")["旁白风格"]  # Ordinary/custom path unchanged.
    assert writing_options(request.model_copy(update={"narration_style": None})) == {
        "本次补充要求": ""
    }
    assert style_context(None) == {}


def test_poetic_example_keeps_commercial_condition_and_never_mutates_user_facts():
    from automated_video_editing_backend.services.grounded_narration import (
        whole_writing_prompt,
        usable_edit,
    )

    slots = [{"node_id": "A", "max_chars": 54, "facts": ["每盒12包"]}]
    request = NarrationAllocateRequest(text="每盒12包", narration_style="poetic")
    payload = json.loads(whole_writing_prompt(slots, request)["user"])
    assert payload["sections"] == slots
    example = payload["句式示例（不属于本次事实）"]
    assert usable_edit(example["改写"], example["原文"], 100)
    assert example["原文"][-1] in example["改写"]
    example["原文"].clear()
    assert mapped_style_options("poetic")["句式示例（不属于本次事实）"]["原文"]
    # Switching style must replace the example, not carry over its lamp/condition facts.
    other = json.loads(
        whole_writing_prompt(slots, request.model_copy(update={"narration_style": "natural"}))[
            "user"
        ]
    )
    assert "灯泡" not in json.dumps(other, ensure_ascii=False)
