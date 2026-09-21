"""Route facts once, budget locally, then edit the whole narration in one call.

There is still one final script and one TTS request. These text-only stages keep the
model from juggling the entire recording, formatting and style in the same call.
"""

from __future__ import annotations

from collections import Counter, OrderedDict
import hashlib
import json
import math
import re

from automated_video_editing_backend.services.narration_styles import mapped_style_options
from automated_video_editing_backend.services.llm import narration_token_budget

ROUTING_PROMPT = """你只做事实归类，不写旁白。facts 是用户原文，唯一事实来源。根据 points 的名称和整段 notes，为每条事实选择一个最相关的点位。
有专门点位时优先专门点位，不要在总览和细节处重复讲同一事实；返回顺序将作为内容优先级，重要的事实放前面。
备注只说明哪个点拍什么，不提供产品事实。不要把完全无关的事实放进某点。售价、名称等整体信息可放在第一个相关点。只选部分点位时，其余事实不用。
没有对应依据就用 null；不知道区域一、二分别是什么，不能按原文顺序猜。
transit 是移动画面，默认不分配事实；如果补充要求或备注明确说明移动段要讲什么，也可对应相关原文事实。
仅返回 JSON 对象，键是事实 id，值是一个点位 id 或 null。例如 {"f1":"A","f2":"A","f3":null}。不得创造 id，每个事实键只出现一次。
例：A是产品外观，B是包装；原文里的产品名称、颜色应对应A，包装数量对应B，不要把这些明确相关的事实丢为null。
例：facts=[{"id":"f1","text":"有红茶和绿茶"}], points=[{"id":"A","label":"区域一"}], notes="不知道区域一拍了什么"，应返回 {"f1":null}。"""

WHOLE_WRITING_PROMPT = """按 sections 顺序写一篇适合朗读的中文旁白，分段返回便于绑定视频。
每段只使用本段 facts，保留其中的事实、数字、单位、否定和条件。语气和句式可以变化，不推断新的效果或评价。你没有看到图片或视频。
遵循本次表达方式，各段自然衔接，避免重复开场；短段先把事实说清楚。每段不超过 max_chars。
仅返回 JSON {"sections":[{"node_id":"输入id","text":"口播正文"}]}。每个输入 id 恰好一次，保持顺序。
以下是格式与事实边界的示例，不是本次内容：
输入：{"sections":[{"node_id":"示例A","max_chars":40,"facts":["水箱容量1.2升"]},{"node_id":"示例B","max_chars":40,"facts":["接水盘可拆卸","滤芯需另购"]}],"表达方式":"简洁说明"}
输出：{"sections":[{"node_id":"示例A","text":"水箱容量1.2升。"},{"node_id":"示例B","text":"接水盘可以拆卸，滤芯需另购。"}]}"""

# These dependencies must not be detached from the rest of their source sentence.
_CONDITION = re.compile(r"如果|除非|只有|仅|否则|才|当.+时|购买|套餐|条件|取决于|需要|须|需")
_QUALIFIERS = ("仅", "需", "才", "按比例", "常规", "另付费", "最多", "至少")
_NUMBER = re.compile(r"(?<![0-9.])[-−]?\d+(?:\.\d+)?")
_QUANTITY = re.compile(
    r"(?<![A-Za-z0-9.])([-−]?\d+(?:\.\d+)?)\s*"
    r"(平方米|摄氏度|厘米|毫米|千米|公里|千克|公斤|毫升|分钟|小时|英寸|米|升|克|瓦|秒|元|层|人|包|盒|只|个|W|%)"
)


def text_units(text):
    """Conservative writing budget; actual TTS clocks remain authoritative."""
    return sum(char.isalnum() for char in text)


def source_facts(text):
    facts = []
    for sentence in re.split(r"[。！？!?；;\n]+", text):
        sentence = sentence.strip()
        if not sentence:
            continue
        clauses = (
            [sentence] if _CONDITION.search(sentence) else re.split(r"，|(?<!\d),(?!\d)", sentence)
        )
        for clause in clauses:
            if clause.strip():
                facts.append({"id": f"f{len(facts) + 1}", "text": clause.strip()})
    return facts


def routing_payload(context, request):
    return {
        "facts": source_facts(request.text),
        "notes": "\n".join(context["capture_notes"]),
        "instructions": request.instructions.strip(),
        # Duration/style deliberately do not affect semantic routing or its cache key.
        "points": [
            {"id": w["id"], "label": w["label"], "kind": w["kind"]} for w in context["windows"]
        ],
    }


def validate_routes(raw, payload):
    def unique_keys(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise ValueError("同一事实被重复分配到多个点位")
            data[key] = value
        return data

    assignments = json.loads(raw, object_pairs_hook=unique_keys)
    points = {point["id"] for point in payload["points"]}
    facts = {fact["id"] for fact in payload["facts"]}
    if not isinstance(assignments, dict) or set(assignments) - facts:
        raise ValueError("事实对应返回了未知原文编号")
    routes = {point["id"]: [] for point in payload["points"]}
    for fact_id, point_id in assignments.items():
        if point_id is None:
            continue
        if not isinstance(point_id, str) or point_id not in points:
            raise ValueError("事实对应返回了无效点位")
        routes[point_id].append(fact_id)
    return routes


def fit_facts(facts, limit):
    """Select complete facts in model-ranked order; never truncate a condition or number."""
    chosen, used = [], 0
    for fact in facts:
        size = text_units(fact)
        if used + size <= limit:
            chosen.append(fact)
            used += size
    return chosen


def writing_limit(window, feedback=None):
    limit = max(0, math.floor(window["duration"] * 3))
    for check in (feedback or {}).get("checks") or []:
        if check.get("node_id") != window["id"]:
            continue
        start, end = check.get("actual_start"), check.get("actual_end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            spoken_seconds = end - start
            if math.isfinite(spoken_seconds) and spoken_seconds > window["duration"]:
                measured_limit = (
                    text_units(check.get("text", "")) * window["duration"] / spoken_seconds
                )
                limit = min(limit, math.floor(measured_limit * 0.9))
    return limit


def writing_options(request):
    """Concrete expression guidance avoids a style name overpowering factual constraints."""
    options = {"本次补充要求": request.instructions.strip()}
    if request.narration_style:
        options.update(mapped_style_options(request.narration_style))
    return options


def whole_writing_prompt(slots, request):
    return {
        "system": WHOLE_WRITING_PROMPT,
        "user": json.dumps(
            {"sections": slots, **writing_options(request)}, ensure_ascii=False, indent=2
        ),
    }


class DraftFormatError(ValueError):
    """Malformed writer output; distinct from a provider or connection failure."""


def validate_edits(raw, slots):
    """Never silently accept invented/duplicate IDs; missing entries fall back locally."""
    try:
        data = json.loads(raw)
        if not isinstance(data, dict) or not isinstance(data.get("sections"), list):
            raise ValueError("sections must be a list")
        allowed = {slot["node_id"] for slot in slots}
        edits = {}
        for section in data["sections"]:
            if not isinstance(section, dict):
                raise ValueError("section must be an object")
            node_id, text = section.get("node_id"), section.get("text")
            if node_id not in allowed or node_id in edits or not isinstance(text, str):
                raise ValueError("unknown/duplicate ID or invalid text")
            edits[node_id] = text.strip()
        return edits
    except (TypeError, ValueError) as exc:
        raise DraftFormatError("整篇润色返回格式无效") from exc


def _comparable_conditions(text):
    """Normalize a few explicit equivalents, not arbitrary semantic paraphrases."""
    for original, canonical in (
        ("无须", "不需"),
        ("无需", "不需"),
        ("只有", "仅"),
        ("需要", "需"),
        ("须", "需"),
        ("要另付费", "需另付费"),
    ):
        text = text.replace(original, canonical)
    return text


def _chinese_phrases(text):
    """Literal four-character overlaps catch copied facts, not general semantic drift."""
    return {
        phrase[i : i + 4]
        for phrase in re.findall(r"[\u3400-\u9fff]{4,}", text)
        for i in range(len(phrase) - 3)
    }


def borrows_other_point(text, facts, other_facts):
    own = set().union(*(_chinese_phrases(fact) for fact in facts))
    other = set().union(*(_chinese_phrases(fact) for fact in other_facts))
    return bool((_chinese_phrases(text) & other) - own)


def usable_edit(text, facts, limit):
    """Reject mechanically detectable regressions; this is not a semantic truth detector."""
    if not text.strip() or text_units(text) > limit or text.lstrip().startswith(("{", "[", "```")):
        return False
    source = "；".join(facts)
    numbers = set(_NUMBER.findall(source))
    if set(_NUMBER.findall(text)) - numbers:
        return False
    source_conditions = _comparable_conditions(source)
    edit_conditions = _comparable_conditions(text)
    if any(word in source_conditions and word not in edit_conditions for word in _QUALIFIERS):
        return False
    # Adding a second negation can reverse one fact while preserving another's "不".
    if Counter(re.findall(r"不|未|最多|至少", source_conditions)) != Counter(
        re.findall(r"不|未|最多|至少", edit_conditions)
    ):
        return False
    # Equal number sets alone cannot detect changing 1.2 metres into 1.2 feet.
    if Counter(_QUANTITY.findall(source)) != Counter(_QUANTITY.findall(text)):
        return False
    # An editor may shorten punctuation, but not convert numeric details into vague prose.
    if numbers - set(_NUMBER.findall(text)):
        return False
    return True


class GroundedNarration:
    def __init__(self, llm):
        self.llm = llm
        self._cache = OrderedDict()

    async def _call(self, cfg, prompt, max_tokens, trace, validate=None):
        key = hashlib.sha256(
            json.dumps(
                [cfg.get("model"), cfg.get("api_key"), prompt, max_tokens],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cached = key in self._cache
        if cached:
            raw = self._cache[key]
            self._cache.move_to_end(key)
        else:
            raw = await self.llm._chat(
                cfg, **prompt, max_tokens=max_tokens, temperature=0.2, json_output=True
            )
            trace.append({**prompt, "cached": False})
            if validate:
                validate(raw)
            self._cache[key] = raw
            if len(self._cache) > 128:
                self._cache.popitem(last=False)
        if cached:
            trace.append({**prompt, "cached": True})
        return raw

    async def draft(self, context, request):
        cfg = self.llm.settings.llm_config()
        payload = routing_payload(context, request)
        trace = []

        def checked_routes(raw):
            try:
                return validate_routes(raw, payload)
            except (TypeError, ValueError) as exc:
                raise ValueError("点位事实对应格式无效，请重新润色或使用原文") from exc

        raw = await self._call(
            cfg,
            {
                "system": ROUTING_PROMPT,
                "user": json.dumps(payload, ensure_ascii=False, indent=2),
            },
            min(8192, max(3500, len(payload["facts"]) * 128)),
            trace,
            validate=checked_routes,
        )
        routes = checked_routes(raw)
        facts = {fact["id"]: fact["text"] for fact in payload["facts"]}
        sections, notes, decisions, slots = [], [], [], []
        for window in context["windows"]:
            if window["kind"] == "transit" and not routes.get(window["id"]):
                continue
            node_id = window["id"]
            assigned = [facts[fact_id] for fact_id in routes.get(node_id, [])]
            limit = writing_limit(window, context.get("measured_feedback"))
            selected = fit_facts(assigned, limit)
            decision = {"node_id": node_id, "max_chars": limit, "facts": selected}
            if not selected:
                reason = "没有明确的文案对应依据" if not assigned else "时长较短，放不下完整事实"
                notes.append(f"{window['label']}：{reason}，本段未安排旁白。")
                decisions.append({**decision, "status": "omitted"})
                continue
            slots.append({**decision, "label": window["label"]})
            decisions.append(decision)
        if not slots:
            raise ValueError(
                "当前画面无法安排可靠旁白：点位对应不明确或时长过短。可补充本次要求，或关闭润色直接填写文案。"
            )

        # Exactly one writing request regardless of point count. There are no per-point
        # retries or paid review calls. Invalid text falls back only to that point's facts.
        try:
            raw = await self._call(
                cfg,
                whole_writing_prompt(slots, request),
                narration_token_budget(sum(slot["max_chars"] for slot in slots), len(slots)),
                trace,
                validate=lambda value: validate_edits(value, slots),
            )
            edits = validate_edits(raw, slots)
        except DraftFormatError:
            edits = {}
            notes.append("整篇润色返回格式无效，已按画面顺序保留原文事实，未追加请求。")
        decisions_by_id = {decision["node_id"]: decision for decision in decisions}
        for slot in slots:
            node_id, selected, limit = slot["node_id"], slot["facts"], slot["max_chars"]
            edited = edits.get(node_id, "")
            other_facts = [
                fact for other in slots if other["node_id"] != node_id for fact in other["facts"]
            ]
            borrowed = borrows_other_point(edited, selected, other_facts)
            accepted = usable_edit(edited, selected, limit) and not borrowed
            text = edited if accepted else "；".join(selected) + "。"
            if not accepted:
                reason = (
                    "改写含其他点位的原文词组"
                    if borrowed
                    else "改写缺失或未通过篇幅／数字单位与限定词检查"
                )
                notes.append(f"{slot['label']}：{reason}，已保留原文事实。")
            decisions_by_id[node_id]["status"] = "edited" if accepted else "source_fallback"
            sections.append({"node_id": node_id, "text": text})
        return {
            "text": "\n\n".join(section["text"] for section in sections),
            "sections": sections,
            "note_assignments": [],
            "general_notes": notes,
            "windows": context["windows"],
            "decisions": decisions,
            "prompt_trace": trace,
            "message": "已按点位事实组织完整文案；实际时长以语音合成后检查为准",
        }
