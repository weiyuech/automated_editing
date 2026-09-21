"""Optional writing direction, kept separate from the operator's system prompt."""

from automated_video_editing_backend.core.models import NarrationStyle


STYLE_RULES = {
    "natural": ("自然讲解", "顺着资料用口语解释，完整短句，少用介绍套话。"),
    "professional": (
        "专业解析",
        "先对象，再规格、功能；用准确名词与限定条件，不给没有依据的性能结论。",
    ),
    "concise": (
        "简洁有力",
        "删铺垫，用短句报重点；可以少选事实，但不能把“后排座椅按比例放倒”改成“座椅放倒”。",
    ),
    "humorous": (
        "轻松幽默",
        "轻巧的口语反差或自问自答，不固定开场，不吹效果。例如原文“有红色和蓝色”，可写“颜色不用猜，红色、蓝色，两种选择。”示例只是句式。",
    ),
    "poetic": (
        "诗意抒情",
        "用短对句的节奏，少用形容词。例如原文“有红茶和绿茶”，可写“两款茶，两种选择：红茶，绿茶。”不能添加香味、景色、心情或功效。示例只是句式。",
    ),
    "classical": (
        "文言雅述",
        "简洁文白相间，句法通顺。例如“有红色和蓝色”可写“有红，亦有蓝。”保留现代型号、数字、单位，不换算古代时辰。示例只是句式。",
    ),
}

# Mapped writing has strict per-point budgets; examples stay out of ordinary/custom prompts.
_MAPPED_RULES = {
    "natural": "用自然的完整短句讲清楚，像面对面说明，不重复开场。",
    "professional": "按对象、规格、功能组织，准确简练，不把参数推断成优势。",
    "concise": "直接报重点，用短句，删掉介绍性铺垫。",
    "humorous": "适度设问、自答或口语反差，有趣但不编效果，不连续提问。",
    "poetic": "用现代散文诗的整齐句式朗读事实，不写比喻或感想。"
    "本段有并列的颜色、种类或物件时，写一组对应的短句，如“有X，也有Y”或“X，是……；Y，是……”。"
    "其余规格平实说明。条件、收费、否定和时间完整照录，不押韵缩写；不借用别段事实。"
    "没有适合并列的内容就自然陈述。",
    "classical": "用简短对句，适量使用“有…亦有…”“可…”句式；现代名词、数字、单位原样保留。",
}

# Most styles share facts for a comparable example. Poetic wording needs to show
# where its rhythm stops: commercial conditions remain complete, ordinary sentences.
_EXAMPLE_FACTS = ("书架有白色和原木色", "书架宽80厘米", "隔板3层，高度可调")
_POETIC_EXAMPLE_FACTS = ("灯罩有白色和灰色", "灯高30厘米", "只有购买套装时，才附带灯泡")
_STYLE_EXAMPLES = {
    "natural": "这款书架有白色和原木色，宽80厘米。隔板共3层，高度可以调节。",
    "professional": "书架提供白色、原木色，宽度80厘米。采用3层隔板，隔板高度可调。",
    "concise": "白色、原木色。宽80厘米，3层隔板，高度可调。",
    "humorous": "白色还是原木色？两种可选。宽80厘米，3层隔板，高度也能调。",
    "poetic": "白色，是灯罩的一种颜色；灰色，是另一种颜色。灯高30厘米。"
    "只有购买套装时，才附带灯泡。",
    "classical": "色有白色，亦有原木色；宽80厘米，隔板3层。层高可调。",
}


def mapped_style_options(style: NarrationStyle) -> dict:
    """Return a fresh example, visibly separate from the operator's facts."""
    return {
        "表达方式": _MAPPED_RULES[style],
        "句式示例（不属于本次事实）": {
            "原文": list(_POETIC_EXAMPLE_FACTS if style == "poetic" else _EXAMPLE_FACTS),
            "改写": _STYLE_EXAMPLES[style],
        },
    }


def style_context(style: NarrationStyle | None) -> dict:
    """Return no layer when disabled, including for older callers with no style field."""
    if style is None:
        return {}
    if style not in STYLE_RULES:
        raise ValueError("请选择支持的旁白风格")
    name, rules = STYLE_RULES[style]
    return {
        "旁白风格": {
            "名称": name,
            "表达要求": rules,
            "边界": "只改变已有事实的表达，不改变系统提示词要求的输出格式。"
            "你没有看到实际图片或视频，不编造画面、经历或效果。"
            "风格通过文字体现，不依赖语音停顿指令，不承诺停顿时长。",
        }
    }
