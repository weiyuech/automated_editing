"""Pure recording timeline and selection rules, shared by files and editing.

Intervals are half-open and cover the complete recording. The transport clock is an
estimate of camera time; its provenance is retained rather than claiming frame-accurate
physical arrival. No camera movement or subtitle state participates in this timeline.
"""

from __future__ import annotations

import math
from typing import Any

from automated_video_editing_backend.core.gimbal_limits import (
    POSE_SETTLED_DELTA_DEG,
    ZOOM_SETTLED_DELTA,
)


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
            # Keep the commanded shot's purpose through the derived tree. Its endpoint
            # alone cannot distinguish an intentional return shot from repositioning.
            for field in ("zoom_start", "zoom_end", "motion_axes", "preparation_reason"):
                if field in shot and end > start:
                    children[-1][field] = shot[field]
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


_FRAME_SECONDS = 1.0 / 30.0
MOTION_TRIM_VERSION = 3
_MIN_STILL_SECONDS = 1.0
_MAX_SAMPLE_GAP_SECONDS = 1.0
# A stall this long is a real stop, not the step-shaped gap between two heartbeat
# reports, so the movement on either side of it is a separate leg.
_MIN_HOLD_SECONDS = 2.5
# A still edge may creep slowly while the gimbal holds a pose. Allow this much drift
# over the whole edge before the drift itself counts as movement to keep.
_STILL_BAND = {1: 2.0, 2: 2.0, 3: 0.05}


def _motion_axes(child):
    known = {"yaw": 1, "pitch": 2, "zoom": 3}
    declared = child.get("motion_axes")
    if isinstance(declared, list) and declared and all(axis in known for axis in declared):
        return tuple(known[axis] for axis in declared)
    key = child["id"].rsplit(":", 1)[-1]
    # Old recordings carry program IDs and zoom endpoints but no explicit axes.
    if child.get("kind") == "zoom" or key in {"zoom-outbound", "zoom-return"} or (
        "zoom_start" in child and "zoom_end" in child
        and child["zoom_start"] != child["zoom_end"]
    ):
        return (3,)
    if key in {"origin-left", "left-right", "right-origin"}:
        return (1,)
    if key in {"origin-up", "up-down", "down-origin"}:
        return (2,)
    if key in {"upper-left", "upper-right", "lower-right", "lower-left"}:
        return (1, 2)
    return (1, 2, 3)


def apply_motion_trim(
    segments: list[dict],
    samples: list[tuple[float, float, float, float]],
    offset: float = 0.0,
    delta_yaw: float = POSE_SETTLED_DELTA_DEG,
    delta_pitch: float = POSE_SETTLED_DELTA_DEG,
    delta_zoom: float = ZOOM_SETTLED_DELTA,
) -> list[dict]:
    """Cut each shot's still head and tail, keeping the motion as one smooth span.

    Samples use the cruise clock; ``offset`` maps them onto the tree clock already used by
    ``build_timeline``. A shot with no detectable motion is left untouched, so a missed or
    undersampled move can never be silently dropped. The pan itself is never split, so a
    smooth sweep stays one continuous clip. The one thing richer than an edge that is cut
    is the gimbal's own overshoot correction: when a sweep stops, holds, and then travels
    back to the target, that trailing hold and return are preparation, not part of the
    shot, so the shot ends where the sweep itself ended.
    """
    points = sorted(
        (t + offset, yaw, pitch, zoom)
        for t, yaw, pitch, zoom in samples
        if all(math.isfinite(value) for value in (t, yaw, pitch, zoom))
    )
    if not points:
        return segments
    for node in segments:
        children = node.get("children") or []
        if not any(child.get("kind") in {"shot", "zoom"} for child in children):
            continue
        rebuilt: list[dict] = []
        for child in children:
            if child.get("kind") not in {"shot", "zoom"}:
                rebuilt.append(child)
                continue
            start, end = child["start"], child["end"]
            axes = _motion_axes(child)
            span = None if child.get("complete") is False else _shot_motion_span(
                points, start, end, delta_yaw, delta_pitch, delta_zoom, axes=axes,
            )
            if span is None or end - start <= _FRAME_SECONDS:
                rebuilt.append({**child, "trim_status": "retained_uncertain"})
                continue
            motion_start, motion_end = span
            if motion_start - start > _FRAME_SECONDS:
                rebuilt.append(_preparation_node(child, "prep-head", start, motion_start))
            rebuilt.append({
                **child, "start": motion_start, "end": motion_end,
                "command_start": start, "command_end": end,
                "trim_status": "heartbeat_head_tail",
            })
            if end - motion_end > _FRAME_SECONDS:
                rebuilt.append(_preparation_node(child, "prep-tail", motion_end, end))
        node["children"] = _merge_preparation(rebuilt)
    return segments


def _shot_motion_span(points, start, end, dy_t, dp_t, dz_t, *, axes=(1, 2, 3)):
    window = [point for point in points if start <= point[0] < end]
    if len(window) < 3 or any(
        not 0 < b[0] - a[0] <= _MAX_SAMPLE_GAP_SECONDS
        for a, b in zip(window, window[1:])
    ):
        return None
    thresholds = {1: dy_t, 2: dp_t, 3: dz_t}
    moving = [
        i for i in range(1, len(window))
        if any(abs(window[i][axis] - window[i - 1][axis]) > thresholds[axis] for axis in axes)
    ]
    ranges = {axis: (min(p[axis] for p in window), max(p[axis] for p in window))
              for axis in axes}
    # Endpoint-only telemetry does not locate the actual motion in the video. In
    # particular, yaw jitter must never turn a 1x/2x zoom report into a confident cut.
    if len(moving) < 2 or not any(
        any(
            ranges[axis][0] + thresholds[axis] < point[axis]
            < ranges[axis][1] - thresholds[axis]
            for point in window
        ) for axis in axes
    ):
        return None

    def band(axis):
        return max(thresholds[axis], _STILL_BAND[axis])

    def shift(first_index, last_index):
        return [window[last_index][axis] - window[first_index - 1][axis] for axis in axes]

    # Split the moving samples at long stalls, then re-join legs that keep travelling the
    # same way. A leg that goes back the other way is the gimbal's overshoot correction
    # rather than a second half of the shot.
    legs: list[list[int]] = [[moving[0]]]
    for index in moving[1:]:
        if window[index][0] - window[legs[-1][-1]][0] >= _MIN_HOLD_SECONDS:
            legs.append([index])
        else:
            legs[-1].append(index)
    phases: list[list[int]] = [list(legs[0])]
    heading = shift(legs[0][0], legs[0][-1])
    for candidate in legs[1:]:
        leg = shift(candidate[0], candidate[-1])
        if sum(a * b for a, b in zip(heading, leg)) < 0:
            phases.append(list(candidate))
            heading = leg
        else:
            phases[-1] = phases[-1] + candidate
            heading = shift(phases[-1][0], phases[-1][-1])
    # Keep the leg that travels furthest; a shot may wake up with a smaller nudge.
    chosen = max(phases, key=lambda phase: math.dist(
        [window[phase[0] - 1][axis] for axis in axes],
        [window[phase[-1]][axis] for axis in axes],
    ))
    first, last = chosen[0] - 1, chosen[-1]

    def still(edge):
        return (
            len(edge) >= 3 and edge[-1][0] - edge[0][0] >= _MIN_STILL_SECONDS
            and all(max(p[axis] for p in edge) - min(p[axis] for p in edge) <= band(axis)
                    for axis in axes)
        )

    # Retain one neighbouring sample as a timing guard. Do not cut a slowly moving
    # edge just because each individual heartbeat delta was below the threshold.
    # When the chosen movement is not the whole window, the earlier and later legs are
    # the tail of the overshoot/settle-back and are cut even though they still move.
    lo = window[max(0, first - 1)][0] if (
        window[0][0] - start <= _MAX_SAMPLE_GAP_SECONDS
        and (still(window[:first + 1]) or phases[0][-1] < last)
    ) else start
    hi = window[min(len(window) - 1, last + 1)][0] if (
        end - window[-1][0] <= _MAX_SAMPLE_GAP_SECONDS
        and (still(window[last:]) or last < phases[-1][-1])
    ) else end
    return lo, hi


def _preparation_node(source, suffix, start, end):
    return {
        "id": f"{source['id']}:{suffix}",
        "label": "镜头前等待" if suffix == "prep-head" else "镜头后等待",
        "kind": "preparation",
        "preparation_reason": "shot_wait",
        "source_shot_id": source["id"],
        "start": start,
        "end": end,
        "boundary_source": "gimbal_motion_trim",
        "complete": True,
    }


def _merge_preparation(children):
    merged: list[dict] = []
    for child in children:
        if (
            merged
            and child.get("kind") == "preparation"
            and merged[-1].get("kind") == "preparation"
            and child.get("preparation_reason") == merged[-1].get("preparation_reason")
            and child.get("source_shot_id") == merged[-1].get("source_shot_id")
            and abs(merged[-1]["end"] - child["start"]) < 1e-6
        ):
            merged[-1] = {**merged[-1], "end": child["end"]}
        else:
            merged.append(child)
    for order, child in enumerate(merged):
        child["order"] = order
    return merged
