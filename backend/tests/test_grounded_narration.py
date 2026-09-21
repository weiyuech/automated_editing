import json
from types import SimpleNamespace

import pytest

from automated_video_editing_backend.core.composition import NarrationAllocateRequest
from automated_video_editing_backend.services.grounded_narration import (
    GroundedNarration,
    ROUTING_PROMPT,
    fit_facts,
    source_facts,
    text_units,
    usable_edit,
    validate_routes,
    writing_limit,
    validate_edits,
    DraftFormatError,
    borrows_other_point,
)


def context(seconds=8):
    return {
        "capture_notes": ["A拍水箱；不要拍到电线。"],
        "windows": [
            {"id": "A", "label": "水箱", "kind": "dwell", "duration": seconds},
            {"id": "move", "label": "移动", "kind": "transit", "duration": 4},
        ],
    }


class FakeLLM:
    def __init__(self, rewrite="水箱容量1.2升。"):
        self.calls = []
        self.rewrite = rewrite
        self.settings = SimpleNamespace(llm_config=lambda: {"model": "test", "api_key": "private"})

    async def _chat(self, cfg, **prompt):
        self.calls.append(prompt)
        return (
            '{"f1":"A"}'
            if prompt["system"] == ROUTING_PROMPT
            else json.dumps({"sections": [{"node_id": "A", "text": self.rewrite}]})
        )


def test_split_facts_without_detaching_conditions_or_splitting_decimal_prices():
    assert [f["text"] for f in source_facts("水箱1.2升，接水盘可拆卸。售价1,299元。")] == [
        "水箱1.2升",
        "接水盘可拆卸",
        "售价1,299元",
    ]
    assert [
        f["text"] for f in source_facts("只有购买套装时，才赠送杯子。需另付费，不含接送。")
    ] == [
        "只有购买套装时，才赠送杯子",
        "需另付费，不含接送",
    ]


def test_budget_keeps_complete_source_facts_and_conditions():
    assert fit_facts(["后排座椅支持按比例放倒", "中控屏为15.6英寸"], 9) == ["中控屏为15.6英寸"]
    assert fit_facts(["只有购买套装时，才赠送杯子"], 5) == []
    assert text_units("1.2 升。") == 3


@pytest.mark.parametrize(
    "raw",
    [
        '{"unknown":"A"}',
        '{"f1":"unknown"}',
        '{"f1":["A","A"]}',
        '{"f1":{"A":1}}',
        '{"f1":42}',
        "[]",
        "not json",
    ],
)
def test_routes_reject_invented_ids_and_malformed_shapes(raw):
    with pytest.raises(ValueError):
        validate_routes(raw, {"facts": [{"id": "f1"}], "points": [{"id": "A"}]})


def test_routes_do_not_duplicate_a_fact_across_overview_and_specific_point():
    with pytest.raises(ValueError, match="多个点位"):
        validate_routes(
            '{"f1":"A","f1":"B"}', {"facts": [{"id": "f1"}], "points": [{"id": "A"}, {"id": "B"}]}
        )


@pytest.mark.parametrize(
    "edit",
    [
        "容量2升。",
        "容量一点二升。",
        "水箱容量1.2升，这是一段超过预算的长文案。",
        '{"text":"水箱容量1.2升"}',
    ],
)
def test_invalid_edit_is_detected_without_another_model_call(edit):
    assert not usable_edit(edit, ["水箱容量1.2升"], 10)


def test_numeric_details_and_qualifiers_are_preserved():
    assert usable_edit("水箱容量1.2升。", ["水箱容量1.2升"], 12)
    assert not usable_edit("支持无线充电。", ["不支持无线充电"], 20)
    assert not usable_edit("座椅可以放倒。", ["后排座椅按比例放倒"], 20)
    assert not usable_edit("液体温度5到60度。", ["液体温度-5到60度"], 20)


@pytest.mark.parametrize(
    "source,edit",
    [
        ("仅预订含早套餐时，才提供双人早餐", "只有预订含早套餐，才提供双人早餐。"),
        ("停车需另付费，不提供机场接送", "停车要另付费，而且不提供机场接送服务。"),
        ("使用需提前预约", "使用须提前预约。"),
    ],
)
def test_narrow_condition_equivalents_do_not_discard_correct_rewrites(source, edit):
    assert usable_edit(edit, [source], 60)


@pytest.mark.parametrize(
    "source,edit",
    [
        ("停车需另付费，不提供机场接送", "停车不需另付费，不提供机场接送。"),
        ("停车无需付费", "停车需要付费。"),
        ("仅预订含早套餐时，才提供双人早餐", "预订套餐提供双人早餐。"),
        ("床宽1.2米", "床宽1.2英寸。"),
        ("水箱容量1.2升", "水箱容量1.2。"),
        ("最多容纳20人", "至少容纳20人。"),
        ("只有购买2盒时，才赠送1只茶杯", "只有购买至少2盒时，才赠送1只茶杯。"),
    ],
)
def test_factual_changes_are_not_allowed_by_condition_normalization(source, edit):
    assert not usable_edit(edit, [source], 60)


def test_obvious_fact_copy_is_checked_against_its_point_not_the_whole_script():
    assert borrows_other_point(
        "红茶与绿茶，每盒12包，独立包装。", ["红茶和绿茶", "每盒12包"], ["每包独立包装"]
    )
    assert not borrows_other_point("每包独立包装。", ["每包独立包装"], ["红茶和绿茶"])
    assert not borrows_other_point(
        "后排座椅按比例放倒。", ["后排座椅按比例放倒"], ["后排座椅有独立头枕"]
    )


@pytest.mark.asyncio
async def test_cross_point_copy_falls_back_only_the_affected_section_without_more_calls():
    llm = FakeLLM()
    ctx = {
        "capture_notes": ["A拍种类，B拍包装"],
        "windows": [{"id": p, "label": p, "kind": "dwell", "duration": 15} for p in "AB"],
    }

    async def response(cfg, **prompt):
        llm.calls.append(prompt)
        if prompt["system"] == ROUTING_PROMPT:
            return '{"f1":"A","f2":"B"}'
        return json.dumps(
            {
                "sections": [
                    {"node_id": "A", "text": "有红茶和绿茶，独立包装。"},
                    {"node_id": "B", "text": "茶包每包独立包装。"},
                ]
            }
        )

    llm._chat = response
    result = await GroundedNarration(llm).draft(
        ctx, NarrationAllocateRequest(text="有红茶和绿茶。每包独立包装。")
    )
    assert [s["text"] for s in result["sections"]] == ["有红茶和绿茶。", "茶包每包独立包装。"]
    assert result["decisions"][0]["status"] == "source_fallback"
    assert result["decisions"][1]["status"] == "edited"
    assert "其他点位" in result["general_notes"][0]
    assert len(llm.calls) == 2


def test_measured_tts_feedback_tightens_local_writing_budget():
    window = {"id": "A", "duration": 6}
    assert writing_limit(window) == 18
    assert (
        writing_limit(
            window,
            {
                "checks": [
                    {
                        "node_id": "A",
                        "actual_start": 0,
                        "actual_end": 12,
                        "text": "水箱容量1.2升",
                    }
                ]
            },
        )
        == 3
    )


@pytest.mark.asyncio
async def test_exact_inputs_reuse_calls_but_changed_style_or_notes_invalidate_relevant_stage():
    llm = FakeLLM()
    service = GroundedNarration(llm)
    request = NarrationAllocateRequest(text="水箱容量1.2升。", instructions="介绍水箱")
    first = await service.draft(context(), request)
    assert first["text"] == "水箱容量1.2升。"
    assert len(llm.calls) == 2
    second = await service.draft(context(), request)
    assert len(llm.calls) == 2
    assert all(step["cached"] for step in second["prompt_trace"])
    assert "private" not in json.dumps(first)
    await service.draft(context(), request.model_copy(update={"narration_style": "poetic"}))
    assert len(llm.calls) == 3  # Same routing; one new writing request.
    changed = {**context(), "capture_notes": ["A拍水箱，备注已更新。"]}
    await service.draft(changed, request)
    assert len(llm.calls) == 4  # New routing; identical verified writer inputs can be reused.


@pytest.mark.asyncio
async def test_bad_edit_falls_back_to_complete_facts_without_paid_retry():
    llm = FakeLLM("水箱容量2升，还有很多优点。")
    result = await GroundedNarration(llm).draft(
        context(), NarrationAllocateRequest(text="水箱容量1.2升。")
    )
    assert result["text"] == "水箱容量1.2升。"
    assert result["decisions"][0]["status"] == "source_fallback"
    assert len(llm.calls) == 2
    assert result["general_notes"]


@pytest.mark.asyncio
async def test_unknown_mapping_does_not_write_or_guess_and_has_useful_error():
    llm = FakeLLM()

    async def unknown(cfg, **prompt):
        llm.calls.append(prompt)
        return '{"f1":null}'

    llm._chat = unknown
    with pytest.raises(ValueError, match="点位对应不明确"):
        await GroundedNarration(llm).draft(
            context(), NarrationAllocateRequest(text="水箱容量1.2升。")
        )
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_invalid_route_is_not_cached():
    llm = FakeLLM()
    real = llm._chat

    async def fail_once(cfg, **prompt):
        if not llm.calls:
            llm.calls.append(prompt)
            return '{"unknown":"A"}'
        return await real(cfg, **prompt)

    llm._chat = fail_once
    service = GroundedNarration(llm)
    request = NarrationAllocateRequest(text="水箱容量1.2升。")
    with pytest.raises(ValueError, match="格式无效"):
        await service.draft(context(), request)
    assert (await service.draft(context(), request))["text"] == "水箱容量1.2升。"
    assert len(llm.calls) == 3


@pytest.mark.asyncio
async def test_explicit_transit_narration_remains_supported():
    llm = FakeLLM("河道长2米。")

    async def transit(cfg, **prompt):
        llm.calls.append(prompt)
        if prompt["system"] == ROUTING_PROMPT:
            payload = json.loads(prompt["user"])
            assert any(p["id"] == "move" and p["kind"] == "transit" for p in payload["points"])
            return '{"f1":"move"}'
        return json.dumps({"sections": [{"node_id": "move", "text": "河道长2米。"}]})

    llm._chat = transit
    result = await GroundedNarration(llm).draft(
        context(),
        NarrationAllocateRequest(
            text="河道长2米。",
            instructions="移动画面讲河道，A点不讲。",
        ),
    )
    assert result["sections"] == [{"node_id": "move", "text": "河道长2米。"}]


@pytest.mark.parametrize("count", [1, 3, 5, 12])
@pytest.mark.asyncio
async def test_call_count_stays_two_for_any_number_of_points(count):
    llm = FakeLLM()
    ctx = {
        "capture_notes": ["按点位对应规格"],
        "windows": [
            {"id": f"p{i}", "label": f"点位{i}", "kind": "dwell", "duration": 10}
            for i in range(count)
        ],
    }

    async def batch(cfg, **prompt):
        llm.calls.append(prompt)
        if prompt["system"] == ROUTING_PROMPT:
            return json.dumps({f"f{i + 1}": f"p{i}" for i in range(count)})
        slots = json.loads(prompt["user"])["sections"]
        assert len(slots) == count
        # Provider order is not authoritative: final sections keep the video order.
        return json.dumps(
            {
                "sections": [
                    {"node_id": s["node_id"], "text": s["facts"][0] + "。"} for s in reversed(slots)
                ]
            }
        )

    llm._chat = batch
    result = await GroundedNarration(llm).draft(
        ctx, NarrationAllocateRequest(text="。".join(f"产品{i}容量{i + 1}升" for i in range(count)))
    )
    assert len(llm.calls) == 2
    assert [s["node_id"] for s in result["sections"]] == [f"p{i}" for i in range(count)]
    assert all(s["status"] == "edited" for s in result["decisions"])


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"sections":"text"}',
        '{"sections":[null]}',
        '{"sections":[{"node_id":"unknown","text":"text"}]}',
        '{"sections":[{"node_id":"A","text":42}]}',
        '{"sections":[{"node_id":[],"text":"text"}]}',
        '{"sections":[{"node_id":"A","text":"one"},{"node_id":"A","text":"two"}]}',
    ],
)
def test_writer_schema_rejects_invalid_or_ambiguous_binding(raw):
    with pytest.raises(DraftFormatError):
        validate_edits(raw, [{"node_id": "A"}])


@pytest.mark.asyncio
async def test_malformed_whole_draft_falls_back_without_retry_and_is_not_cached():
    llm = FakeLLM()
    valid = llm._chat

    async def broken(cfg, **prompt):
        if prompt["system"] == ROUTING_PROMPT:
            return await valid(cfg, **prompt)
        llm.calls.append(prompt)
        return "malformed"

    llm._chat = broken
    service = GroundedNarration(llm)
    request = NarrationAllocateRequest(text="水箱容量1.2升。")
    first = await service.draft(context(), request)
    assert first["text"] == "水箱容量1.2升。"
    assert len(first["prompt_trace"]) == len(llm.calls) == 2
    await service.draft(context(), request)
    assert (
        len(llm.calls) == 3
    )  # Explicit second action reuses routing, not malformed writer output.


@pytest.mark.asyncio
async def test_missing_or_wrong_point_preserves_other_valid_edit_without_extra_call():
    llm = FakeLLM()
    ctx = {
        "capture_notes": ["A水箱，B屏幕，C颜色"],
        "windows": [{"id": p, "label": p, "kind": "dwell", "duration": 10} for p in "ABC"],
    }

    async def partial(cfg, **prompt):
        llm.calls.append(prompt)
        if prompt["system"] == ROUTING_PROMPT:
            return '{"f1":"A","f2":"B","f3":"C"}'
        # A valid, B borrows A's number, C absent.
        return json.dumps(
            {
                "sections": [
                    {"node_id": "A", "text": "水箱的容量为1.2升。"},
                    {"node_id": "B", "text": "屏幕1.2英寸。"},
                ]
            }
        )

    llm._chat = partial
    result = await GroundedNarration(llm).draft(
        ctx, NarrationAllocateRequest(text="水箱容量1.2升。屏幕15.6英寸。车身有白色。")
    )
    assert [s["text"] for s in result["sections"]] == [
        "水箱的容量为1.2升。",
        "屏幕15.6英寸。",
        "车身有白色。",
    ]
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_provider_failure_is_reported_not_disguised_as_bad_draft_or_retried():
    llm = FakeLLM()
    valid = llm._chat

    async def failed(cfg, **prompt):
        if prompt["system"] == ROUTING_PROMPT:
            return await valid(cfg, **prompt)
        llm.calls.append(prompt)
        raise ValueError("LLM API error 429")

    llm._chat = failed
    with pytest.raises(ValueError, match="429"):
        await GroundedNarration(llm).draft(
            context(), NarrationAllocateRequest(text="水箱容量1.2升。")
        )
    assert len(llm.calls) == 2
