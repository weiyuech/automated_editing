"""Conservative text mapping and a single shared clock for audio and subtitles."""

from __future__ import annotations

import re

from automated_video_editing_backend.services import subtitles


def speech_chars(text):
    return "".join(c.lower() for c in text if c.isalnum())


def sentences(text):
    return [s.strip() for s in re.findall(r"[^。！？!?\n]+[。！？!?]*", text) if speech_chars(s)]


def map_script(text, references, windows, embedder):
    """Keep known bindings; suggest only unambiguous, forward semantic matches.

    Scores are similarities, not probabilities. Conservative gates deliberately leave
    ambiguous or backwards text unassigned, to be checked in the synchronized preview.
    """
    refs = [r.model_dump() if hasattr(r, "model_dump") else r for r in references]
    order = {w["id"]: i for i, w in enumerate(windows)}
    complete = speech_chars(text) == speech_chars("".join(r["text"] for r in refs))
    units = [r["text"] for r in refs] if complete else sentences(text)
    known = {}
    for ref in refs:
        for value in [ref["text"], *sentences(ref["text"])]:
            known.setdefault(speech_chars(value), set()).add(ref["node_id"])
    mapped = []
    for index, unit in enumerate(units):
        ids = known.get(speech_chars(unit), set())
        node_id = refs[index]["node_id"] if complete else next(iter(ids)) if len(ids) == 1 else None
        mapped.append(
            {"text": unit, "node_id": node_id, "method": "reference" if node_id else "unmapped"}
        )
    descriptions = []
    for w in windows:
        notes = w.get("notes") or []
        descriptions.append(
            w["label"]
            + "："
            + "；".join(
                [
                    *(notes if isinstance(notes, list) else [str(notes)]),
                    *(r["text"] for r in refs if r["node_id"] == w["id"]),
                ]
            )
        )
    matrix = embedder.similarity(units, descriptions) if units and windows else None
    previous = -1
    for index, entry in enumerate(mapped):
        scores = list(matrix[index]) if matrix is not None else []
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        best = ranked[0] if ranked else None
        confident = (
            best is not None
            and scores[best] >= 0.68
            and (len(ranked) == 1 or scores[best] - scores[ranked[1]] >= 0.08)
        )
        if entry["node_id"]:
            target = order[entry["node_id"]]
            if target < previous:
                entry.update(node_id=None, method="unmapped", warning="文案顺序与画面不一致")
                continue
            if confident and best != target:
                entry["warning"] = "语义检查与原段落绑定不一致，请试听核对"
            previous = target
        elif confident:
            following = next(
                (order[e["node_id"]] for e in mapped[index + 1 :] if e["node_id"]), len(windows) - 1
            )
            if previous <= best <= following:
                entry.update(
                    node_id=windows[best]["id"],
                    method="semantic",
                    score=round(float(scores[best]), 4),
                )
                previous = best
    # Neighboring sentences belonging to one point share one audio block, not separate TTS calls.
    merged = []
    for entry in mapped:
        if merged and entry["node_id"] and entry["node_id"] == merged[-1]["node_id"]:
            merged[-1]["text"] += entry["text"]
            if entry.get("warning"):
                merged[-1]["warning"] = entry["warning"]
        else:
            merged.append(dict(entry))
    for entry in merged:
        entry["label"] = (
            windows[order[entry["node_id"]]]["label"] if entry["node_id"] else "未确定画面"
        )
    return merged, "bge-small-zh-v1.5-int8" if matrix is not None else "unavailable"


def reliable_words(text, words, quality, duration):
    normalized = subtitles.normalise_words(words)
    if quality != "exact" or not normalized:
        return False
    if speech_chars(text) != speech_chars("".join(w[0] for w in normalized)):
        return False
    last = 0.0
    for _, start, end in normalized:
        if not 0 <= start < end <= duration + 0.001 or start < last - 0.001:
            return False
        last = end
    return True


def playback_plan(duration, available, checks, *, complete, rate=1.0, auto_tempo=True):
    """Partition on real word gaps. Every input sample belongs to exactly one block."""
    local = complete and checks and all(c.get("actual_start") is not None for c in checks)
    boundaries = [0.0]
    if local:
        for left, right in zip(checks, checks[1:]):
            if left["actual_end"] > right["actual_start"] + 0.001:
                local = False  # Provider word straddles a paragraph boundary: do not cut it.
                break
            boundaries.append((left["actual_end"] + right["actual_start"]) / 2)
    if not local:
        checks = [{"planned_start": 0.0, "planned_end": available}]
        boundaries = [0.0]
    boundaries.append(duration)
    blocks, cursor = [], 0.0
    for i, check in enumerate(checks):
        start, end = boundaries[i : i + 2]
        target = max(cursor, check["planned_start"])
        room = min(available, check["planned_end"]) - target
        if room <= 0:
            raise ValueError("该画面没有可用旁白时间，请调整文案对应")
        speed = max(rate, (end - start) / room) if auto_tempo else rate
        if speed > 1.1 + 1e-6 or (end - start) / speed > room + 0.001:
            raise ValueError(
                f"{check.get('label', '完整旁白')}在 1.1 倍内仍放不下，请精简文案后重新合成"
            )
        speed = min(1.1, max(0.9, speed))
        cursor = target + (end - start) / speed
        blocks.append(
            {
                "source_start": start,
                "source_end": end,
                "start": target,
                "end": cursor,
                "rate": speed,
            }
        )
    return blocks, bool(local)


def retime_words(words, blocks):
    output = []
    for text, start, end in subtitles.normalise_words(words):
        block = next(
            (
                b
                for b in blocks
                if start >= b["source_start"] - 0.001 and end <= b["source_end"] + 0.001
            ),
            None,
        )
        if block is None:
            raise ValueError("语音时间跨越了调整边界，无法安全同步字幕")
        output.append(
            {
                "text": text,
                "start_time": round(
                    (block["start"] + (start - block["source_start"]) / block["rate"]) * 1000
                ),
                "end_time": round(
                    (block["start"] + (end - block["source_start"]) / block["rate"]) * 1000
                ),
            }
        )
    return output
