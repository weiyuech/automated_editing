"""The displayed prompt and the submitted prompt share this single builder."""

import json

MAPPED_NARRATION_SYSTEM_PROMPT = """为已确定顺序和时长的组合视频编写一篇连贯的中文旁白。
用户文案是唯一事实来源；补充要求控制重点、表达和留白。拍摄备注用于理解点位与拍摄意图，不把拍摄指令读出口，也不新增事实。
整段备注只提供一次。结合真实画面 ID、点位名称和上下文，理解哪些内容属于哪个点位；不按逗号机械拆分，不对应每个云台小镜头。无把握的归属放入 general_notes，不猜测。
只讲当前组合中出现的内容，遵循时间表顺序；按各点位可用时长决定详略，移动画面可衔接或留白，不重复开场、不为凑时长扩写。
没有拍摄备注时，根据用户文案、补充要求和时间表安排内容，不假装知道未描述的画面。时长供控制篇幅，最终以语音实测为准。
若提供实测反馈，针对超时或提前讲到下一点精简调整。
仅返回 JSON：{"note_assignments":[{"node_id":"时间表中的id","text":"对应备注内容"}],"general_notes":["归属不确定或全局备注"],"sections":[{"node_id":"时间表中的id","text":"完整口播段落"}]}。
每组中同一 id 最多一次。可省略无需口播的时间段；sections 按播放顺序连起来应是一篇通顺完整旁白，不分别合成录音。"""

SIMPLE_COMPOSITION_SYSTEM_PROMPT = """根据用户提供的文案和补充要求，为当前组合视频润色一篇连贯自然的中文旁白。

规则：
- 以用户文案为事实依据，不新增事实。补充要求控制表达方式、重点和语气。
- 根据提供的组合总时长控制篇幅；总时长是上限，允许留白，不为填满时间扩写。
- 整篇衔接自然，不重复开场。
- 只返回口播正文，不返回点位 ID、JSON、标题或解释。"""


def mapped_narration_prompt(
    text,
    duration,
    windows,
    *,
    capture_notes=None,
    instructions="",
    feedback=None,
    system_prompt=None,
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
                    {key: value for key, value in window.items() if key != "notes"}
                    for window in windows
                ],
                "上一版实测": feedback,
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
        )
    else:
        if request.system_prompt is not None and not request.system_prompt.strip():
            raise ValueError("自定义提示词不能为空，可恢复默认提示词")
        payload = {
            "文案": request.text,
            "本次补充要求": request.instructions.strip(),
            "组合总时长（秒）": context["duration_seconds"],
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
