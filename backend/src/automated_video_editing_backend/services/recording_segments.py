"""Pure recording timeline and selection rules, shared by files and editing.

Intervals are half-open and cover the complete recording. The transport clock is an
estimate of camera time; its provenance is retained rather than claiming frame-accurate
physical arrival. No camera movement or subtitle state participates in this timeline.
"""

from __future__ import annotations

import math
from typing import Any


def timestamp(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def build_timeline(payload: dict, duration: float, offset: float = 0.0) -> list[dict]:
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(offset):
        raise ValueError("录制时长或校准偏移无效")
    transitions: list[tuple[float, str, str, str, str]] = [
        (0.0, "preparation", "开始准备", "start", "recording_start_estimate")
    ]
    events = payload.get("recording_events") or []
    visits = payload.get("segments") or []
    previous = "起点"
    for ordinal, visit in enumerate(visits):
        if not isinstance(visit, dict):
            continue
        index = visit.get("index", ordinal)
        visit_id = f"visit-{index}"
        name = f"{visit.get('path_name', '点位')}#{visit.get('goal_id', index)}"
        relevant = [e for e in events if isinstance(e, dict) and e.get("visit_index") == index]
        by_type = {}
        for event in relevant:
            t = timestamp(event.get("seconds"))
            if t is not None:
                by_type.setdefault(event.get("type"), t)
        if relevant:
            going = by_type.get("goal_going")
            written = by_type.get("goal_write")
            accepted = "goal_accepted" in by_type
            rejected = "goal_rejected" in by_type
            arrival = by_type.get("goal_done")
            start = going if going is not None else written
            source = "goal_going_feedback" if going is not None else "goal_write_estimate"
            if rejected and going is None and arrival is None:
                continue
            kind = "transit" if going is not None or accepted or arrival is not None else "unknown"
            failed = by_type.get("goal_failed", by_type.get("navigation_uncertain"))
        else:
            start = timestamp(visit.get("transit_start_seconds"))
            arrival = timestamp(visit.get("arrived_at_seconds"))
            source = "legacy_estimate"
            kind = "transit" if visit.get("status") == "arrived" else "unknown"
            failed = (
                timestamp(visit.get("departed_at_seconds"))
                if visit.get("status") == "failed"
                else None
            )
        if start is not None:
            transitions.append(
                (
                    start + offset,
                    kind,
                    f"{previous} → {name} · {'行进' if kind == 'transit' else '状态未确认'}",
                    visit_id + "-transit",
                    source,
                )
            )
        if arrival is not None and (start is None or arrival >= start):
            transitions.append(
                (
                    arrival + offset,
                    "dwell",
                    f"{name} · 停留",
                    visit_id + "-dwell",
                    "goal_done_feedback" if relevant else "legacy_estimate",
                )
            )
            previous = name
        elif failed is not None:
            transitions.append(
                (
                    failed + offset,
                    "unknown",
                    f"前往 {name} · 状态未确认",
                    visit_id + "-unknown",
                    "navigation_uncertain",
                )
            )

    # Stable ordering preserves an immediate done after going at the same timestamp.
    transitions.sort(key=lambda entry: entry[0])
    bounded: list[tuple[float, str, str, str, str]] = []
    for entry in transitions:
        at = max(0.0, min(duration, entry[0]))
        if at >= duration:
            continue
        normalized = (at, *entry[1:])
        if bounded and abs(bounded[-1][0] - at) < 1e-9:
            bounded[-1] = normalized
        else:
            bounded.append(normalized)
    result = []
    for order, entry in enumerate(bounded):
        start, kind, label, segment_id, source = entry
        end = bounded[order + 1][0] if order + 1 < len(bounded) else duration
        if end <= start:
            continue
        result.append(
            {
                "id": segment_id,
                "order": order,
                "kind": kind,
                "label": label,
                "start": start,
                "end": end,
                "boundary_source": source,
            }
        )
    visits_by_id = {
        f"visit-{v.get('index', i)}-dwell": v for i, v in enumerate(visits) if isinstance(v, dict)
    }
    for node in result:
        visit = visits_by_id.get(node["id"])
        if not visit or not visit.get("shots"):
            continue
        children = []
        cursor = node["start"]

        def append_child(key, label, kind, start, end, source, complete=True):
            if end <= start:
                return
            children.append(
                {
                    "id": f"{node['id']}:{key}",
                    "order": len(children),
                    "label": label,
                    "kind": kind,
                    "start": start,
                    "end": end,
                    "boundary_source": source,
                    "complete": complete,
                }
            )

        for shot in visit["shots"]:
            start, end = timestamp(shot.get("start")), timestamp(shot.get("end"))
            if start is None or end is None:
                continue
            start, end = max(cursor, start + offset), min(node["end"], end + offset)
            if start > node["end"]:
                break
            append_child(
                f"gap-{len(children)}",
                "镜头准备",
                "preparation",
                cursor,
                start,
                "application_estimate",
            )
            append_child(
                shot["id"],
                shot["label"],
                shot["kind"],
                start,
                end,
                shot.get("boundary_source", "application_estimate"),
                shot.get("status") == "complete",
            )
            cursor = max(cursor, end)
        append_child("tail", "结束准备", "preparation", cursor, node["end"], "application_estimate")
        node["children"] = children
    return result


def iter_nodes(nodes):
    """Traverse for validation/file operations only; persisted representation stays nested."""
    for node in nodes:
        yield node
        yield from iter_nodes(node.get("children", []))


def selected_nodes(nodes: list[dict], ids: set[str]) -> list[dict]:
    result = []
    for node in nodes:
        if node["id"] in ids:
            result.append(node)
        else:
            result.extend(selected_nodes(node.get("children", []), ids))
    return result


def validate_tree(nodes, start, end, seen=None):
    seen = set() if seen is None else seen
    cursor = start
    for order, node in enumerate(nodes):
        lo, hi = timestamp(node.get("start")), timestamp(node.get("end"))
        if (
            lo is None
            or hi is None
            or lo < cursor - 1e-6
            or hi <= lo
            or hi > end + 1e-6
            or not isinstance(node.get("id"), str)
            or node["id"] in seen
        ):
            raise ValueError("拍摄树节点时间或标识无效")
        seen.add(node["id"])
        cursor = hi
        validate_tree(node.get("children", []), lo, hi, seen)


def selected_ranges(group: dict, selection: dict) -> list[tuple[float, float]]:
    """Resolve a group's child choice to a chronological union, never a source list."""
    children = group["segments"]
    ids = set(selection.get("segment_ids") or [])
    known = {s["id"] for s in iter_nodes(children)}
    if selection.get("include_full"):
        return [(0.0, group["duration"])]
    if ids - known:
        raise ValueError("所选分段已变化，请重新选择这次拍摄")
    ranges: list[tuple[float, float]] = []
    for segment in selected_nodes(children, ids):
        start, end = segment["start"], segment["end"]
        if ranges and start <= ranges[-1][1] + 1e-9:
            ranges[-1] = (ranges[-1][0], max(end, ranges[-1][1]))
        else:
            ranges.append((start, end))
    if not ranges:
        raise ValueError("请为这次拍摄选择完整录制或至少一个子片段")
    return ranges


def rebase_timeline(segments: list[dict], ranges: list[tuple[float, float]]) -> list[dict]:
    """Preserve point meaning on the concatenated input's clock."""
    result = []
    cursor = 0.0
    for start, end in ranges:
        for segment in segments:
            lo, hi = max(start, segment["start"]), min(end, segment["end"])
            if hi > lo:
                result.append(
                    {
                        **segment,
                        "start": cursor + lo - start,
                        "end": cursor + hi - start,
                        "source_start": lo,
                        "source_end": hi,
                        "children": shift_nodes(
                            rebase_timeline(segment.get("children", []), [(lo, hi)]),
                            cursor + lo - start,
                        ),
                    }
                )
        cursor += end - start
    return result


def shift_nodes(nodes: list[dict], offset: float) -> list[dict]:
    return [
        {
            **node,
            "start": node["start"] + offset,
            "end": node["end"] + offset,
            "children": shift_nodes(node.get("children", []), offset),
        }
        for node in nodes
    ]
