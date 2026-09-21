"""The displayed prompt and the submitted prompt share this single builder."""

import json

from automated_video_editing_backend.services.narration_styles import style_context

MAPPED_NARRATION_SYSTEM_PROMPT = """你是旁白编辑。原文给事实，整段拍摄备注和点位名称只帮助判断对应关系；你没有看过视频。
把相关事实写到对应点位，每点一句或两句，整篇连贯。只保留此处相关信息，不将其他点位的信息移到这里。用原文的具体事实代替评价；不添加性能、感受或现场所见。
每项 text 不超过该点的 max_chars（汉字、字母、数字计数）。短到放不下时只选一个完整事实，仍放不下就不写；不是每点都必须填满。数字和限定条件不能随意省掉。风格通过句式体现，不改变事实。
不确定哪个点展示什么就不分配，写进 general_notes；不得把原文按顺序硬塞进未知点位。默认移动段不写。
仅返回 JSON：{"sections":[{"node_id":"真实id","text":"正文"}],"general_notes":[]}。空段省略；id 不重复，按画面顺序。
示例：原文“青禾杯容量300毫升。杯盖可拆洗。”，杯身点预算12字、杯盖点预算10字。可返回 {"sections":[{"node_id":"杯身点","text":"青禾杯容量300毫升。"},{"node_id":"杯盖点","text":"杯盖可拆洗。"}],"general_notes":[]}。示例产品不能出现在本稿。"""

SIMPLE_COMPOSITION_SYSTEM_PROMPT = """你是中文口播编辑。只改写用户原文，不补充产品事实或评价。保留数字、名称和限制条件；不要从参数推断效果。写一篇适合朗读的短文，风格只改变句式。只返回正文。
例：原文“杯盖可拆卸”，可改为“杯盖可以拆下来”；不可改成“杯盖水洗很方便”。
这是一条组合视频，以给定总时长为上限，宁可简短；不推断点位对应。"""


def mapped_narration_prompt(
    text,
    duration,
    windows,
    *,
    capture_notes=None,
    instructions="",
    feedback=None,
    system_prompt=None,
    narration_style=None,
):
    if system_prompt is not None and not system_prompt.strip():
        raise ValueError("自定义提示词不能为空，可恢复默认提示词")
    return {
        "system": system_prompt if system_prompt is not None else MAPPED_NARRATION_SYSTEM_PROMPT,
        "user": json.dumps(
            {
                "文案": text,
                "本次补充要求": instructions.strip(),
                "组合时长": duration,
                "拍摄备注原文": "\n".join(capture_notes or []),
                "画面时间表": [
                    {**{key: value for key, value in window.items() if key != "notes"},
                     "max_chars": max(0, int(window["duration"] * 3))}
                    for window in windows
                ],
                "上一版实测": feedback,
                **style_context(narration_style),
            },
            ensure_ascii=False,
            indent=2,
        ),
    }


def composition_prompt(context, request, *, feedback=None):
    single = context["mode"] == "single_recording"
    if single:
        prompt = mapped_narration_prompt(
            request.text,
            context["duration_seconds"],
            context["windows"],
            capture_notes=context["capture_notes"],
            instructions=request.instructions,
            system_prompt=request.system_prompt,
            feedback=feedback,
            narration_style=request.narration_style,
        )
    else:
        if request.system_prompt is not None and not request.system_prompt.strip():
            raise ValueError("自定义提示词不能为空，可恢复默认提示词")
        payload = {
            "文案": request.text,
            "本次补充要求": request.instructions.strip(),
            "组合总时长（秒）": context["duration_seconds"],
            **style_context(request.narration_style),
        }
        if feedback:
            payload["上一版实测时长"] = {
                key: feedback.get(key)
                for key in ("source_seconds", "actual_seconds", "available_seconds")
            }
        prompt = {
            "system": request.system_prompt
            if request.system_prompt is not None
            else SIMPLE_COMPOSITION_SYSTEM_PROMPT,
            "user": json.dumps(payload, ensure_ascii=False, indent=2),
        }
    return {
        **context,
        **prompt,
        "baseline_system": MAPPED_NARRATION_SYSTEM_PROMPT
        if single
        else SIMPLE_COMPOSITION_SYSTEM_PROMPT,
    }
