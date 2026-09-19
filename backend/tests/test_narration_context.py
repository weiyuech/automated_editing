import json

import pytest

from automated_video_editing_backend.core.composition import NarrationPromptRequest
from automated_video_editing_backend.services.narration_context import (
    narration_context,
    original_recording_ids,
)
from automated_video_editing_backend.services.narration_prompts import composition_prompt


def node(key, origins=None, **extra):
    value = dict(
        id=key,
        kind="recording",
        label=key,
        start=0,
        end=41,
        duration=41,
        notes=["拍摄备注不得泄漏到跨录制提示词"],
    )
    if origins is not None:
        value["source_recording_ids"] = origins
    return dict(value, **extra)


@pytest.mark.parametrize(
    "tree,expected",
    [
        ([node("one", ["capture:A"]), node("two", ["capture:A"])], "single_recording"),
        ([node("saved", ["capture:A", "capture:B"])], "multiple_recordings"),
        ([node("wrapper", children=[node("A"), node("B")])], "multiple_recordings"),
        ([node("missing", [])], "unknown"),
        ([], "unknown"),
    ],
)
def test_policy_uses_original_recordings_not_current_file_count(tree, expected):
    context = narration_context({"composition_tree": tree, "duration_seconds": 41})
    assert context["mode"] == expected
    assert bool(context["windows"]) == (expected == "single_recording")


def test_multi_prompt_excludes_notes_and_old_alignment_even_with_custom_rules():
    context = narration_context(
        {"composition_tree": [node("saved", ["A", "B"])], "duration_seconds": 173}
    )
    request = NarrationPromptRequest(
        text="用户文案", instructions="语气自然", system_prompt="自定义规则"
    )
    prompt = composition_prompt(
        context,
        request,
        feedback={
            "source_seconds": 180,
            "actual_seconds": 174,
            "available_seconds": 173,
            "checks": [{"notes": "旧备注"}],
            "text": "旧文案",
            "mappings": ["旧映射"],
        },
    )
    assert json.loads(prompt["user"]) == {
        "文案": "用户文案",
        "本次补充要求": "语气自然",
        "组合总时长（秒）": 173,
        "上一版实测时长": {"source_seconds": 180, "actual_seconds": 174, "available_seconds": 173},
    }
    assert prompt["system"] == "自定义规则"
    assert prompt["windows"] == []
    baseline = composition_prompt(context, request.model_copy(update={"system_prompt": None}))
    assert baseline["system"] == prompt["baseline_system"]


def test_single_prompt_keeps_notes_and_exact_combined_clock():
    context = narration_context(
        {"composition_tree": [node("A", ["capture:A"])], "duration_seconds": 41}
    )
    prompt = composition_prompt(
        context, NarrationPromptRequest(text="文案", instructions="不要开场白")
    )
    payload = json.loads(prompt["user"])
    assert payload["组合时长"] == 41
    assert payload["拍摄备注原文"] == "拍摄备注不得泄漏到跨录制提示词"
    assert "notes" not in payload["画面时间表"][0]
    assert payload["本次补充要求"] == "不要开场白"
    assert original_recording_ids(
        [node("outer", children=[node("a", ["A"]), node("b", ["A"])])]
    ) == ["A"]
