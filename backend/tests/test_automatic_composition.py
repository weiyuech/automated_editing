import asyncio
from copy import deepcopy
import json
import random
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from automated_video_editing_backend.core.automatic_composition import (
    AutomaticCompositionPlanRequest,
    AutomaticCompositionRequest,
)
from automated_video_editing_backend.services.automatic_composition import (
    AutomaticCompositionService,
    decode_choices,
    recording_choices,
    sample_codes,
)
from automated_video_editing_backend.services.composition import CompositionService
from automated_video_editing_backend.services.capture import sidecar_path
from automated_video_editing_backend.services.recording_segments import selected_ranges
from test_controlled_composition import evidence, video
from test_controlled_composition import studio as studio


def recording(key, count=3):
    nodes = [{"id": "arrival", "kind": "transit", "label": "起点→A", "start": 0, "end": 1}]
    clock = 1
    for point in range(2):
        children = [{
            "id": f"{point}:prep", "kind": "preparation", "label": "准备",
            "start": clock, "end": clock + 1, "complete": True,
        }]
        for shot in range(count):
            children.append({
                "id": f"{point}:{shot}", "kind": "shot", "label": f"镜头{shot}",
                "start": clock + shot + 1, "end": clock + shot + 2, "complete": True,
            })
        nodes.append({
            "id": str(point), "kind": "dwell", "label": f"点位{point}",
            "start": clock, "end": clock + count + 1, "children": children,
        })
        clock += count + 1
        if point == 0:
            nodes.append({
                "id": "transfer", "kind": "transit", "label": "A→B",
                "start": clock, "end": clock + 2,
            })
            clock += 2
    return {
        "id": key, "title": key, "fingerprint": key, "timeline_version": "v1",
        "duration": clock, "segments": nodes,
    }


class Captures:
    def __init__(self, groups):
        self.groups = groups

    async def ensure_timeline(self, key):
        return deepcopy(self.groups[key])


def media_library(tmp_path, counts):
    groups, items = {}, []
    for index, count in enumerate(counts):
        key = f"capture{index}"
        groups[key] = recording(key, count)
        path = tmp_path / f"{key}.mp4"
        path.write_bytes(b"source identity")
        items.append(SimpleNamespace(
            id=f"media{index}", kind="video", path=str(path),
            metadata={"capture_group": {"id": key}, "composition_order": index},
        ))
    return SimpleNamespace(list_items=lambda: items, captures=Captures(groups))


class Compositions:
    def __init__(self, media):
        self.media, self.calls = media, []

    async def create(self, request, *, automatic_snapshot):
        self.calls.append((request, automatic_snapshot))
        return {"id": str(len(self.calls)), "status": "queued"}

    async def cancel(self, key):
        pass


def test_count_must_exceed_recordings_and_no_duplicate_source():
    for kwargs in ({"media_ids": ["a", "b"], "count": 2},
                   {"media_ids": ["a", "a"], "count": 3}):
        with pytest.raises(ValidationError):
            AutomaticCompositionPlanRequest(**kwargs)


def test_sampling_is_unique_even_near_capacity_or_with_huge_products():
    rng = random.Random(17)
    assert set(sample_codes(12, 12, rng)) == set(range(12))
    result = sample_codes(12**100, 200, rng)
    assert len(set(result)) == 200
    assert all(0 <= code < 12**100 for code in result)


def test_only_complete_shots_are_eligible_and_only_between_point_transit():
    group = recording("capture")
    group["segments"][1]["children"][1]["complete"] = False
    points, transit, issues = recording_choices(group)
    assert not issues
    assert len(points[0]["shots"]) == 2
    assert all(shot["kind"] == "shot" for point in points for shot in point["shots"])
    assert transit == ["transfer"]
    assert len(decode_choices(3, points)) == 2


@pytest.mark.asyncio
async def test_plan_balances_in_library_order_not_click_order(tmp_path):
    service = AutomaticCompositionService(Compositions(media_library(tmp_path, [3] * 7)))
    plan = await service.plan(AutomaticCompositionPlanRequest(
        media_ids=[f"media{i}" for i in reversed(range(7))], count=20,
    ))
    assert plan["feasible"]
    assert [row["allocated_count"] for row in plan["recordings"]] == [3, 3, 3, 3, 3, 3, 2]
    assert [row["media_id"] for row in plan["recordings"]] == [f"media{i}" for i in range(7)]
    assert plan["available_count"] == 63
    assert plan["maximum_balanced_count"] == 63


@pytest.mark.asyncio
async def test_shortage_is_reported_without_silent_redistribution(tmp_path):
    service = AutomaticCompositionService(Compositions(media_library(tmp_path, [1, 3])))
    request = AutomaticCompositionPlanRequest(media_ids=["media0", "media1"], count=4)
    plan = await service.plan(request)
    assert plan["available_count"] == 10
    assert not plan["feasible"]
    assert plan["maximum_balanced_count"] == 2
    assert plan["minimum_count"] == 3  # this selection cannot meet the strict count rule
    assert plan["recordings"][0]["shortage"] == 1
    assert [row["allocated_count"] for row in plan["recordings"]] == [2, 2]
    with pytest.raises(ValueError, match="均匀分配"):
        await service.create(AutomaticCompositionRequest(
            **request.model_dump(), plan_fingerprint=plan["fingerprint"],
        ))
    assert not service.compositions.calls


@pytest.mark.asyncio
async def test_create_one_recording_per_output_one_shot_per_point_in_order(tmp_path):
    service = AutomaticCompositionService(
        Compositions(media_library(tmp_path, [3, 3])), rng=random.Random(2),
    )
    request = AutomaticCompositionPlanRequest(
        media_ids=["media0", "media1"], count=6, include_transit=True,
    )
    plan = await service.plan(request)
    result = await service.create(AutomaticCompositionRequest(
        **request.model_dump(), plan_fingerprint=plan["fingerprint"],
    ))
    assert len(result["records"]) == 6
    for source in ("media0", "media1"):
        calls = [(req, snap) for req, snap in service.compositions.calls if req.media_ids == [source]]
        assert len(calls) == 3
        assert len({snap["metadata"]["choice_code"] for _, snap in calls}) == 3
        for req, snap in calls:
            choice = req.capture_selections[0]
            assert len(choice.segment_ids) == 3  # two point shots + the one transit
            assert "arrival" not in choice.segment_ids
            assert all("prep" not in key for key in choice.segment_ids)
            group = service.media.captures.groups[choice.capture_id]
            ranges = selected_ranges(group, choice.model_dump())
            assert sum(end - start for start, end in ranges) == 4
            assert snap["source_recording_ids"] == [f"capture:{choice.capture_id}"]
            assert req.purpose == "library"


@pytest.mark.asyncio
async def test_create_rejects_changed_timeline_or_file_before_enqueuing(tmp_path):
    service = AutomaticCompositionService(Compositions(media_library(tmp_path, [3])))
    request = AutomaticCompositionPlanRequest(media_ids=["media0"], count=2)
    plan = await service.plan(request)
    service.media.captures.groups["capture0"]["timeline_version"] = "v2"
    with pytest.raises(ValueError, match="已改变"):
        await service.create(AutomaticCompositionRequest(
            **request.model_dump(), plan_fingerprint=plan["fingerprint"],
        ))
    assert not service.compositions.calls


@pytest.mark.asyncio
async def test_auto_builds_are_serial_and_revalidate_waiting_snapshot(tmp_path):
    media = media_library(tmp_path, [3])
    compositions = CompositionService(
        SimpleNamespace(media=media, renderer=None), directory=tmp_path / "previews",
    )
    started, release = asyncio.Event(), asyncio.Event()
    running, peak = 0, 0

    async def build(record, request, **kwargs):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        started.set()
        await release.wait()
        record["status"] = "ready"
        running -= 1

    compositions._build = build
    service = AutomaticCompositionService(compositions)
    request = AutomaticCompositionPlanRequest(media_ids=["media0"], count=2)
    plan = await service.plan(request)
    result = await service.create(AutomaticCompositionRequest(
        **request.model_dump(), plan_fingerprint=plan["fingerprint"],
    ))
    await started.wait()
    media.captures.groups["capture0"]["timeline_version"] = "changed while queued"
    tasks = list(compositions.tasks.values())
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 1
    records = [compositions.get(record["id"]) for record in result["records"]]
    assert records[0]["status"] == "ready"
    assert records[1]["status"] == "failed"
    assert "时间轴已改变" in records[1]["error"]
    assert not compositions.tasks


@pytest.mark.asyncio
async def test_real_auto_previews_preserve_provenance_and_require_explicit_save(studio, tmp_path):
    path = video(tmp_path / "recording.mp4", "red")
    sidecar_path(path).write_text(json.dumps(evidence()), encoding="utf8")
    root = studio.media.import_path(str(path))
    service = AutomaticCompositionService(studio, rng=random.Random(0))
    request = AutomaticCompositionPlanRequest(media_ids=[root.id], count=2)
    plan = await service.plan(request)
    result = await service.create(AutomaticCompositionRequest(
        **request.model_dump(), plan_fingerprint=plan["fingerprint"],
    ))
    await asyncio.wait_for(asyncio.gather(*list(studio.tasks.values())), timeout=30)
    records = [studio.get(row["id"]) for row in result["records"]]
    assert all(row["status"] == "ready" for row in records), records
    assert all(not row.get("material_path") for row in records)
    for row in records:
        assert len(row["tree"]) == 1
        assert row["source_recording_ids"] == ["capture:capture"]
        assert row["tree"][0]["source_recording_ids"] == ["capture:capture"]
        assert len(row["tree"][0]["children"][0]["children"]) == 1
    first = records[0]
    material = await studio.save_material(first["id"], first["signature"])
    assert material.metadata["source_recording_ids"] == ["capture:capture"]
    assert material.metadata["automatic"]["batch_id"] == result["batch_id"]
    assert not records[1].get("material_path")
