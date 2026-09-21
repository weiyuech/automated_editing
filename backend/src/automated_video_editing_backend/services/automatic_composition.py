"""Plan within-recording combinations, then queue immutable previews for human review.

Each mixed-radix number represents one choice of shot per point. Sampling those
numbers avoids materializing the potentially enormous Cartesian product.
"""

from __future__ import annotations

import math
import random
from uuid import uuid4

from automated_video_editing_backend.core.automatic_composition import (
    MAX_AUTOMATIC_OUTPUTS,
    AutomaticCompositionPlanRequest,
    AutomaticCompositionRequest,
)
from automated_video_editing_backend.core.composition import CompositionRequest
from automated_video_editing_backend.core.models import CaptureSelection
from automated_video_editing_backend.services.capture_library import digest
from automated_video_editing_backend.services.composition import file_identity
from automated_video_editing_backend.services.recording_segments import (
    selected_ranges,
    validate_tree,
)

MAX_SAFE_JSON_INTEGER = 2**53 - 1


def sample_codes(capacity: int, count: int, rng) -> list[int]:
    """Floyd sampling: O(count) memory, including products larger than sys.maxsize."""
    if not 0 <= count <= capacity:
        raise ValueError("可用组合不足")
    chosen, result = set(), []
    for upper in range(capacity - count, capacity):
        candidate = rng.randrange(upper + 1)
        code = upper if candidate in chosen else candidate
        chosen.add(code)
        result.append(code)
    rng.shuffle(result)
    return result


def decode_choices(code: int, points: list[dict]) -> list[str]:
    selected = []
    for point in points:
        code, index = divmod(code, len(point["shots"]))
        selected.append(point["shots"][index]["id"])
    return selected


def recording_choices(group: dict) -> tuple[list[dict], list[str], list[str]]:
    """Use confirmed complete shots only; never substitute an entire point or a gap."""
    validate_tree(group["segments"], 0, group["duration"])
    points, issues = [], []
    for node in group["segments"]:
        if node["kind"] != "dwell":
            continue
        shots = [
            child for child in node.get("children", [])
            if child.get("kind") in {"shot", "zoom"}
            and child.get("complete") is True
            and child["end"] - child["start"] >= 1 / 30
        ]
        points.append({"id": node["id"], "label": node["label"], "shots": shots})
        if not shots:
            issues.append(f"{node['label']}没有完整镜头，请选择其他录制或使用手动组合")
    if not points:
        issues.append("没有可识别的点位，请使用手动组合")
    point_ids = {point["id"] for point in points}
    dwell_nodes = [node for node in group["segments"] if node["id"] in point_ids]
    transits = [
        node["id"] for node in group["segments"]
        if dwell_nodes and node["kind"] == "transit"
        and node["start"] >= dwell_nodes[0]["end"]
        and node["end"] <= dwell_nodes[-1]["start"]
        and node["end"] - node["start"] >= 1 / 30
    ]
    return points, transits, issues


class AutomaticCompositionService:
    def __init__(self, compositions, rng=None):
        self.compositions = compositions
        self.media = compositions.media
        self.rng = rng or random.SystemRandom()

    async def _snapshot(self, request: AutomaticCompositionPlanRequest):
        items = {item.id: item for item in self.media.list_items()}
        selected = []
        captures = set()
        for media_id in request.media_ids:
            item = items.get(media_id)
            group = item.metadata.get("capture_group") if item else None
            if (not item or item.kind != "video" or not group
                    or item.metadata.get("composition_id") or item.metadata.get("capture_input")):
                raise ValueError("自动组合只能选择机器人原始录制")
            if group["id"] in captures:
                raise ValueError("同一次录制只能选择一次")
            captures.add(group["id"])
            selected.append(item)
        # Match the library's order, independent of checkbox click order.
        selected.sort(key=lambda item: (item.metadata["composition_order"], item.path))
        sources = []
        for item in selected:
            capture_id = item.metadata["capture_group"]["id"]
            group = await self.media.captures.ensure_timeline(capture_id)
            points, transits, issues = recording_choices(group)
            capacity = math.prod(len(point["shots"]) for point in points) if points else 0
            try:
                source_identity = file_identity(item.path)
            except OSError as exc:
                raise ValueError("完整录制文件不可用，请恢复文件或使用手动组合") from exc
            sources.append({
                "media_id": item.id,
                "capture_id": capture_id,
                "title": group["title"],
                "group": group,
                "points": points,
                "transits": transits,
                "capacity": capacity,
                "issues": issues,
                "files": {item.path: source_identity},
                "groups": {capture_id: [group["fingerprint"], group["timeline_version"]]},
            })
        return sources

    @staticmethod
    def _plan(request, sources):
        base, extra = divmod(request.count, len(sources))
        recordings = []
        for index, source in enumerate(sources):
            allocation = base + (index < extra)
            recordings.append({
                "media_id": source["media_id"],
                "capture_id": source["capture_id"],
                "title": source["title"],
                "point_count": len(source["points"]),
                "capacity": min(source["capacity"], MAX_SAFE_JSON_INTEGER),
                "capacity_exact": str(source["capacity"]),
                "allocated_count": allocation,
                "shortage": max(0, allocation - source["capacity"]),
                "issues": source["issues"],
            })
        capacity = sum(source["capacity"] for source in sources)
        # A plentiful recording cannot silently absorb a deficient recording's share.
        # Extra outputs go to the first records in library order, just as above.
        smallest = min(source["capacity"] for source in sources)
        extra_capacity = next(
            index for index, source in enumerate(sources) if source["capacity"] == smallest
        )
        balanced_capacity = smallest * len(sources) + extra_capacity
        feasible = all(not row["shortage"] and not row["issues"] for row in recordings)
        return {
            "fingerprint": digest([
                request.count, request.include_transit,
                [{key: source[key] for key in (
                    "media_id", "groups", "files", "points", "transits"
                )} for source in sources],
            ]),
            "requested_count": request.count,
            "include_transit": request.include_transit,
            "available_count": min(capacity, MAX_SAFE_JSON_INTEGER),
            "available_count_exact": str(capacity),
            "minimum_count": len(sources) + 1,
            "maximum_balanced_count": min(balanced_capacity, MAX_AUTOMATIC_OUTPUTS),
            "maximum_request_count": MAX_AUTOMATIC_OUTPUTS,
            "feasible": feasible,
            "recordings": recordings,
            "issues": [] if feasible else [
                "部分录制无法满足均匀分配，请减少数量或调整录制选择；不会自动改分配到其他录制。"
            ],
        }

    async def plan(self, request: AutomaticCompositionPlanRequest):
        return self._plan(request, await self._snapshot(request))

    async def create(self, request: AutomaticCompositionRequest):
        sources = await self._snapshot(request)
        plan = self._plan(request, sources)
        if request.plan_fingerprint != plan["fingerprint"]:
            raise ValueError("录制或组合设置已改变，请重新查看可用组合数量")
        if not plan["feasible"]:
            raise ValueError("可用镜头不足以按当前数量均匀分配，请调整数量或录制选择")
        batch_id, records = str(uuid4()), []
        try:
            for source, allocation in zip(sources, plan["recordings"]):
                codes = sample_codes(source["capacity"], allocation["allocated_count"], self.rng)
                for index, code in enumerate(codes, start=1):
                    ids = decode_choices(code, source["points"])
                    if request.include_transit:
                        ids.extend(source["transits"])
                    # selected_ranges traverses the source tree in chronological order.
                    choice = CaptureSelection(capture_id=source["capture_id"], segment_ids=ids)
                    selected_ranges(source["group"], choice.model_dump())
                    snapshot = {
                        "files": source["files"],
                        "groups": source["groups"],
                        "source_recording_ids": [f"capture:{source['capture_id']}"],
                        "metadata": {
                            "batch_id": batch_id,
                            "index": index,
                            "capture_id": source["capture_id"],
                            "include_transit": request.include_transit,
                            "choice_code": str(code),
                            "plan_fingerprint": plan["fingerprint"],
                        },
                    }
                    record = await self.compositions.create(
                        CompositionRequest(
                            purpose="library",
                            title=f"{source['title'][:175]} · 组合 {index:02d}",
                            media_ids=[source["media_id"]],
                            capture_selections=[choice],
                        ),
                        automatic_snapshot=snapshot,
                    )
                    records.append(record)
        except BaseException:
            # No orphaned partial batch when registration or the request is interrupted.
            for record in records:
                await self.compositions.cancel(record["id"])
            raise
        return {"batch_id": batch_id, "plan": plan, "records": records}
