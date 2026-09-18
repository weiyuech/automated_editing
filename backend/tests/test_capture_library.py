import asyncio
import json
import subprocess
from copy import deepcopy
from itertools import pairwise
from pathlib import Path

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    MediaItem,
    TimelineClip,
)
from automated_video_editing_backend.services import capture_library as capture_library_module
from automated_video_editing_backend.services.capture import gimbal_sidecar_path, sidecar_path
from automated_video_editing_backend.services.capture_library import CaptureLibrary
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.recording_segments import (
    build_timeline,
    rebase_timeline,
    selected_ranges,
)
from automated_video_editing_backend.services.render import RenderService


def _evidence() -> dict:
    return {
        "capture_session_id": "capture-1",
        "title": "门店巡游",
        "started_at": "2026-09-17T08:00:00+08:00",
        "segments": [
            {"index": 0, "path_name": "主路线", "goal_id": 1, "status": "arrived"},
            {"index": 1, "path_name": "主路线", "goal_id": 2, "status": "arrived"},
        ],
        "recording_events": [
            {"visit_index": 0, "type": "goal_write", "seconds": 0.1},
            {"visit_index": 0, "type": "goal_going", "seconds": 0.2},
            {"visit_index": 0, "type": "goal_done", "seconds": 0.8},
            {"visit_index": 1, "type": "goal_write", "seconds": 1.0},
            {"visit_index": 1, "type": "goal_going", "seconds": 1.1},
            {"visit_index": 1, "type": "goal_done", "seconds": 1.6},
        ],
        "recording_clock": {"origin": "record_write_estimate"},
        "notes": ["新品区"],
    }


def _group(master: Path, segments: list[dict] | None = None, **values) -> dict:
    result = {
        "id": "capture-1",
        "title": "门店巡游",
        "started_at": "2026-09-17T08:00:00+08:00",
        "master_path": str(master),
        "fingerprint": "fingerprint-1",
        "status": "ready",
        "error": "",
        "duration": 2.0,
        "offset_seconds": 0.0,
        "segments": segments or [],
        "evidence": _evidence(),
        "timeline_version": "timeline-1",
        "timing_note": "测试",
    }
    result.update(values)
    return result


def _child(segment_id: str, order: int, start: float, end: float, path: Path) -> dict:
    return {
        "id": segment_id,
        "order": order,
        "kind": "transit" if order % 2 == 0 else "dwell",
        "label": segment_id,
        "start": start,
        "end": end,
        "boundary_source": "goal_done_feedback",
        "path": str(path),
        "status": "ready",
        "error": "",
    }


def _video(path: Path, seconds: float = 0.8, color: str = "blue") -> Path:
    subprocess.run(
        [
            RenderService().ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:size=64x64:rate=10:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def _video_with_audio(path: Path, seconds: float = 0.8, color: str = "blue") -> Path:
    subprocess.run(
        [
            RenderService().ffmpeg_binary(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:size=64x64:rate=10:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


def test_recording_timeline_is_ordered_gapless_and_uses_robot_feedback():
    timeline = build_timeline(_evidence(), duration=2.0)

    assert [(part["start"], part["end"]) for part in timeline] == [
        (0.0, 0.2),
        (0.2, 0.8),
        (0.8, 1.1),
        (1.1, 1.6),
        (1.6, 2.0),
    ]
    assert [part["kind"] for part in timeline] == [
        "preparation",
        "transit",
        "dwell",
        "transit",
        "dwell",
    ]
    assert timeline[1]["boundary_source"] == "goal_going_feedback"
    assert timeline[-1]["end"] == 2.0
    assert all(left["end"] == right["start"] for left, right in pairwise(timeline))


def test_selection_is_chronological_merged_and_rejects_stale_child_ids():
    timeline = build_timeline(_evidence(), duration=2.0)
    group = {"duration": 2.0, "segments": timeline}

    full = selected_ranges(
        group,
        {"include_full": False, "segment_ids": [part["id"] for part in timeline]},
    )
    partial = selected_ranges(
        group,
        {
            "include_full": False,
            # Deliberately reverse client order: manifest chronology is authoritative.
            "segment_ids": [timeline[3]["id"], timeline[1]["id"], timeline[2]["id"]],
        },
    )

    assert full == [(0.0, 2.0)]
    assert partial == [(0.2, 1.6)]
    assert rebase_timeline(timeline, partial)[0]["start"] == 0.0
    with pytest.raises(ValueError, match="所选分段已变化"):
        selected_ranges(group, {"include_full": False, "segment_ids": ["old-child"]})
    with pytest.raises(ValueError, match="至少一个子片段"):
        selected_ranges(group, {"include_full": False, "segment_ids": []})


def test_capture_manifest_survives_restart_and_resumes_interrupted_generation(tmp_path):
    directory = tmp_path / "segments"
    directory.mkdir()
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    child_path = directory / "version" / "child.mp4"
    child_path.parent.mkdir()
    child_path.write_bytes(b"child")
    manifest = tmp_path / "captures.json"
    first = CaptureLibrary(manifest, directory)
    first._commit(
        _group(
            master,
            [_child("segment-1", 0, 0.0, 1.0, child_path)],
            status="generating",
        )
    )

    reopened = CaptureLibrary(manifest, directory)

    assert reopened.problem == ""
    assert reopened.groups["capture-1"]["status"] == "pending"
    assert reopened.groups["capture-1"]["segments"][0]["path"] == str(child_path)


def test_capture_tree_stays_in_library_and_cannot_enter_flat_pool(tmp_path):
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    sidecar_path(master).write_text(json.dumps(_evidence()), encoding="utf-8")
    manifest = tmp_path / "captures.json"
    directory = tmp_path / "segments"
    library = tmp_path / "media-library.json"
    first = MediaService(path=library)
    first.captures = CaptureLibrary(manifest, directory)
    root = first.import_path(str(master))
    first.list_items()
    assert root.metadata.get("capture_group")
    with pytest.raises(ValueError, match="Invalid media type"):
        first.update_media_pool({"source_media_ids": [root.id]})
    assert root.id not in first.media_pool()["source_media_ids"]
    reopened = MediaService(path=library)
    reopened.captures = CaptureLibrary(manifest, directory)
    assert root.id not in reopened.media_pool()["source_media_ids"]
    assert any(i.id == root.id for i in reopened.list_items())


def test_corrupt_derived_manifest_does_not_hide_the_source_recording(tmp_path):
    manifest = tmp_path / "captures.json"
    manifest.write_text("{broken", encoding="utf-8")
    directory = tmp_path / "segments"
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    item = MediaItem(
        id="root",
        path=str(master),
        kind="video",
        metadata={"source": "local_import", "role": "raw_video"},
    )
    inventory = {item.id: item}
    captures = CaptureLibrary(manifest, directory)

    # This index is derived from the irreplaceable source and its capture sidecar. Corrupt
    # derived state may disable child clips, but must never make the master disappear.
    captures.enrich(inventory)

    assert inventory[item.id].path == str(master)
    assert "capture_group" not in inventory[item.id].metadata


def test_legacy_cruise_sidecar_is_not_silently_enrolled_for_transcoding(tmp_path):
    master = tmp_path / "legacy.mp4"
    master.write_bytes(b"master")
    sidecar_path(master).write_text(
        json.dumps(
            {
                "capture_session_id": "legacy-capture",
                "segments": [{"index": 0, "path_name": "旧路线", "goal_id": 1}],
            }
        ),
        encoding="utf-8",
    )
    item = MediaItem(
        id="root",
        path=str(master),
        kind="video",
        metadata={"source": "local_import", "role": "raw_video"},
    )
    captures = CaptureLibrary(tmp_path / "captures.json", tmp_path / "segments")

    captures.enrich({item.id: item})

    assert captures.groups == {}


@pytest.mark.asyncio
async def test_one_bad_pending_capture_does_not_starve_the_next_one(tmp_path, monkeypatch):
    captures = CaptureLibrary(tmp_path / "captures.json", tmp_path / "segments")
    captures.groups = {
        "missing": {"id": "missing", "status": "pending"},
        "healthy": {"id": "healthy", "status": "pending"},
    }
    reached_healthy = asyncio.Event()

    async def prepare(key):
        if key == "missing":
            raise ValueError("missing master")
        captures.groups[key]["status"] = "ready"
        reached_healthy.set()

    monkeypatch.setattr(captures, "prepare", prepare)
    await captures.start(lambda: None)
    try:
        await asyncio.wait_for(reached_healthy.wait(), timeout=0.5)
    finally:
        await captures.close()

    assert captures.groups["healthy"]["status"] == "ready"


@pytest.mark.asyncio
async def test_prepare_persists_a_visible_failure_when_the_master_is_offline(tmp_path):
    missing = tmp_path / "detached-drive" / "master.mp4"
    captures = CaptureLibrary(tmp_path / "captures.json", tmp_path / "segments")
    captures._commit(_group(missing, status="pending", duration=0.0))

    with pytest.raises(ValueError, match="完整录制文件不存在"):
        await captures.prepare("capture-1")

    failed = CaptureLibrary(tmp_path / "captures.json", tmp_path / "segments").groups["capture-1"]
    assert failed["status"] == "failed"
    assert "完整录制文件不存在" in failed["error"]


@pytest.mark.asyncio
async def test_missing_master_can_resolve_one_child_directly_and_join_multiple(tmp_path):
    directory = tmp_path / "segments"
    directory.mkdir()
    first = _video(directory / "first.mp4", color="blue")
    second = _video(directory / "second.mp4", color="red")
    gimbal_sidecar_path(first).write_text(
        json.dumps({"samples": [[0.2, -10.0, 1.0]]}), encoding="utf-8"
    )
    gimbal_sidecar_path(second).write_text(
        json.dumps({"samples": [[0.3, 10.0, 2.0]]}), encoding="utf-8"
    )
    segments = [
        _child("first", 0, 0.0, 0.8, first),
        _child("second", 1, 0.8, 1.6, second),
    ]
    missing = tmp_path / "offline-master.mp4"
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    captures._commit(_group(missing, segments, duration=1.6))
    root = MediaItem(
        id="root",
        path=str(missing),
        kind="video",
        metadata={"role": "raw_video"},
    )

    direct = await captures.resolve(
        root,
        {"capture_id": "capture-1", "include_full": False, "segment_ids": ["first"]},
    )
    joined = await captures.resolve(
        root,
        {
            "capture_id": "capture-1",
            "include_full": False,
            "segment_ids": ["first", "second"],
        },
    )

    assert direct.path == str(first)
    assert direct.metadata["capture_input"] is True
    assert Path(joined.path).is_file()
    assert joined.path != str(first) and joined.path != str(second)
    assert RenderService().probe_duration(joined.path) == pytest.approx(1.6, abs=0.15)
    capture_payload = json.loads(sidecar_path(joined.path).read_text(encoding="utf-8"))
    assert capture_payload["source_ranges"] == [[0.0, 1.6]]
    assert capture_payload["recording_timeline"][0]["start"] == 0.0
    samples = json.loads(gimbal_sidecar_path(joined.path).read_text(encoding="utf-8"))["samples"]
    assert samples == [[0.2, -10.0, 1.0], [1.1, 10.0, 2.0]]

    with pytest.raises(ValueError, match="完整录制已移走或删除"):
        await captures.resolve(
            root,
            {"capture_id": "capture-1", "include_full": True, "segment_ids": []},
        )


@pytest.mark.asyncio
async def test_present_master_materializes_noncontiguous_ranges_with_real_ffmpeg(tmp_path):
    directory = tmp_path / "segments"
    directory.mkdir()
    master = _video(tmp_path / "master.mp4", seconds=1.6, color="green")
    gimbal_sidecar_path(master).write_text(
        json.dumps(
            {
                "samples": [
                    [0.2, -15.0, 1.0],
                    [0.75, 0.0, 2.0],
                    [1.2, 15.0, 3.0],
                ]
            }
        ),
        encoding="utf-8",
    )
    segments = [
        _child("opening", 0, 0.0, 0.5, directory / "opening.mp4"),
        _child("middle", 1, 0.5, 1.0, directory / "middle.mp4"),
        _child("closing", 2, 1.0, 1.5, directory / "closing.mp4"),
    ]
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    captures._commit(_group(master, segments, duration=1.6))
    root = MediaItem(id="root", path=str(master), kind="video", metadata={"role": "raw_video"})

    resolved = await captures.resolve(
        root,
        {
            "capture_id": "capture-1",
            "include_full": False,
            "segment_ids": ["closing", "opening"],
        },
    )

    assert RenderService().probe_duration(resolved.path) == pytest.approx(1.0, abs=0.15)
    evidence = json.loads(sidecar_path(resolved.path).read_text(encoding="utf-8"))
    assert evidence["source_ranges"] == [[0.0, 0.5], [1.0, 1.5]]
    assert [(part["start"], part["end"]) for part in evidence["recording_timeline"]] == [
        (0.0, 0.5),
        (0.5, 1.0),
    ]
    samples = json.loads(gimbal_sidecar_path(resolved.path).read_text(encoding="utf-8"))["samples"]
    assert samples == [[0.2, -15.0, 1.0], [0.7, 15.0, 3.0]]


@pytest.mark.asyncio
async def test_encode_and_child_join_preserve_real_audio(tmp_path):
    directory = tmp_path / "segments"
    directory.mkdir()
    master = _video_with_audio(tmp_path / "master.mp4", seconds=1.6)
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    ranged = directory / "ranged.mp4"

    await captures._encode(master, [(0.0, 0.5), (1.0, 1.5)], ranged)

    assert captures.renderer.has_audio_stream(str(ranged)) is True
    assert captures.renderer.probe_duration(str(ranged)) == pytest.approx(1.0, abs=0.15)

    first = _video_with_audio(directory / "first-audio.mp4", color="blue")
    second = _video_with_audio(directory / "second-audio.mp4", color="red")
    joined = directory / "joined-audio.mp4"
    await captures._concat_children([first, second], joined)

    assert captures.renderer.has_audio_stream(str(joined)) is True
    assert captures.renderer.probe_duration(str(joined)) == pytest.approx(1.6, abs=0.15)


@pytest.mark.asyncio
async def test_unknown_or_inconsistent_audio_probe_never_publishes_a_child(tmp_path, monkeypatch):
    directory = tmp_path / "segments"
    directory.mkdir()
    source = directory / "source.mp4"
    second = directory / "second.mp4"
    source.write_bytes(b"source")
    second.write_bytes(b"second")
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    encoded = directory / "encoded.mp4"
    joined = directory / "joined.mp4"
    monkeypatch.setattr(captures.renderer, "has_audio_stream", lambda _path: None)

    with pytest.raises(ValueError, match="无法确认源视频的声音轨"):
        await captures._encode(source, [(0.0, 1.0)], encoded)
    with pytest.raises(ValueError, match="无法确认所选子片段的声音轨"):
        await captures._concat_children([source, second], joined)

    assert not encoded.exists()
    assert not joined.exists()

    monkeypatch.setattr(
        captures.renderer,
        "has_audio_stream",
        lambda path: Path(path).name == source.name,
    )
    with pytest.raises(ValueError, match="声音轨不一致"):
        await captures._concat_children([source, second], joined)
    assert not joined.exists()


def test_manual_timeline_authorizes_only_ready_manifest_children(tmp_path):
    directory = tmp_path / "segments"
    directory.mkdir()
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    child = directory / "child.mp4"
    child.write_bytes(b"child")
    forged = directory / "forged.mp4"
    forged.write_bytes(b"forged")
    media = MediaService(path=tmp_path / "media-library.json")
    media.captures = CaptureLibrary(tmp_path / "captures.json", directory)
    root = media.import_path(str(master))
    media.captures._commit(_group(master, [_child("child", 0, 0.0, 1.0, child)]))
    media.list_items()
    jobs = JobService(EventHub(), media, None, None, RenderService())

    authorized = jobs._manual_clip_items(
        TimelineClip(
            media_id=root.id,
            source_path=str(child),
            start=0.0,
            duration=0.5,
            timeline_start=0.0,
        ),
        {},
    )

    assert authorized[0].metadata["capture_child"] is True
    assert authorized[0].metadata["capture_root_media_id"] == root.id
    with pytest.raises(ValueError, match="media_id 与文件路径不一致"):
        jobs._manual_clip_items(
            TimelineClip(
                media_id=root.id,
                source_path=str(forged),
                start=0.0,
                duration=0.5,
                timeline_start=0.0,
            ),
            {},
        )

    blocked = deepcopy(media.captures.groups["capture-1"])
    blocked["segments"][0]["status"] = "failed"
    media.captures._commit(blocked)
    assert media.capture_child_item(root.id, str(child)) is None


def test_capture_input_authorization_requires_exact_internal_id_path_and_file(tmp_path):
    directory = tmp_path / "segments"
    input_directory = directory / "capture" / "inputs"
    input_directory.mkdir(parents=True)
    valid = input_directory / "valid.mp4"
    valid.write_bytes(b"valid")
    other = input_directory / "other.mp4"
    other.write_bytes(b"other")
    media = MediaService(path=tmp_path / "media-library.json")
    media.captures = CaptureLibrary(tmp_path / "captures.json", directory)
    internal = MediaItem(
        id="capture-input-trusted",
        path=str(valid),
        kind="video",
        metadata={"source": "capture_input", "capture_input": True},
    )
    media._edit_inputs[internal.id] = internal
    jobs = JobService(EventHub(), media, None, None, RenderService())

    assert jobs._manual_clip_items(
        TimelineClip(
            media_id=internal.id,
            source_path=str(valid),
            start=0.0,
            duration=0.5,
            timeline_start=0.0,
        ),
        {},
    ) == [internal]
    with pytest.raises(ValueError, match="media_id 与文件路径不一致"):
        jobs._manual_clip_items(
            TimelineClip(
                media_id=internal.id,
                source_path=str(other),
                start=0.0,
                duration=0.5,
                timeline_start=0.0,
            ),
            {},
        )

    valid.unlink()
    with pytest.raises(ValueError, match="不在媒体库"):
        jobs._manual_clip_items(
            TimelineClip(
                media_id=internal.id,
                source_path=str(valid),
                start=0.0,
                duration=0.5,
                timeline_start=0.0,
            ),
            {},
        )


def test_deleted_child_stays_missing_until_the_operator_explicitly_retries(tmp_path):
    directory = tmp_path / "segments"
    directory.mkdir()
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    removed = directory / "removed.mp4"
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    captures._commit(_group(master, [_child("removed", 0, 0.0, 1.0, removed)]))
    root = MediaItem(
        id="root",
        path=str(master),
        kind="video",
        metadata={"source": "local_import", "role": "raw_video"},
    )

    captures.enrich({root.id: root})
    assert root.metadata["capture_group"]["segments"][0]["status"] == "missing"
    assert captures.groups["capture-1"]["segments"][0]["status"] == "ready"

    reopened = CaptureLibrary(tmp_path / "captures.json", directory)
    restarted_root = root.model_copy(deep=True)
    restarted_root.metadata.pop("capture_group", None)
    reopened.enrich({restarted_root.id: restarted_root})
    assert restarted_root.metadata["capture_group"]["segments"][0]["status"] == "missing"
    assert reopened.groups["capture-1"]["status"] == "ready"

    reopened.retry("capture-1")
    assert reopened.groups["capture-1"]["status"] == "pending"
    assert reopened.groups["capture-1"]["segments"][0]["status"] == "pending"


def test_cleanup_inputs_deletes_only_disposable_composites_and_their_sidecars(tmp_path):
    directory = tmp_path / "segments"
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    published = captures._group_directory("capture-1") / "timeline-1" / "child.mp4"
    published.parent.mkdir(parents=True)
    published.write_bytes(b"published-child")
    captures._commit(_group(master, [_child("child", 0, 0.0, 1.0, published)]))
    inputs = captures._group_directory("capture-1") / "inputs"
    inputs.mkdir()
    disposable = inputs / "selection.mp4"
    disposable.write_bytes(b"selection-video")
    sidecar_path(disposable).write_bytes(b"capture-evidence")
    gimbal_sidecar_path(disposable).write_bytes(b"gimbal-evidence")
    partial = inputs / "interrupted.part.mp4"
    partial.write_bytes(b"partial")
    filters = inputs / "interrupted.filters.txt"
    filters.write_bytes(b"filter")
    expected_bytes = sum(
        path.stat().st_size
        for path in (
            disposable,
            sidecar_path(disposable),
            gimbal_sidecar_path(disposable),
            partial,
            filters,
        )
    )

    deleted, freed, skipped = captures.cleanup_inputs()

    assert (deleted, freed, skipped) == (5, expected_bytes, 0)
    assert not inputs.exists()
    assert published.read_bytes() == b"published-child"
    assert captures.groups["capture-1"]["segments"][0]["path"] == str(published)


def test_cleanup_inputs_defers_active_and_external_render_inputs(tmp_path):
    directory = tmp_path / "segments"
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    captures._commit(_group(master))
    inputs = captures._group_directory("capture-1") / "inputs"
    inputs.mkdir(parents=True)
    active = inputs / "active.mp4"
    active.write_bytes(b"active")
    sidecar_path(active).write_bytes(b"active-sidecar")
    captures._active.add(str(active.resolve()))

    first = captures.cleanup_inputs()

    assert first == (0, 0, 1)
    assert active.is_file()
    assert sidecar_path(active).is_file()

    captures._active.clear()
    external = inputs / "external.mp4"
    external.write_bytes(b"external")
    sidecar_path(external).write_bytes(b"external-sidecar")
    free = inputs / "free.mp4"
    free.write_bytes(b"free")
    sidecar_path(free).write_bytes(b"free-sidecar")
    captures.external_path_in_use = lambda path: Path(path) == external

    deleted, freed, skipped = captures.cleanup_inputs()

    assert deleted == 4  # active video + sidecar, then free video + sidecar
    assert freed == len(b"active") + len(b"active-sidecar") + len(b"free") + len(b"free-sidecar")
    assert skipped == 1
    assert external.is_file()
    assert sidecar_path(external).is_file()
    assert not active.exists() and not sidecar_path(active).exists()
    assert not free.exists() and not sidecar_path(free).exists()


def test_enrich_purges_group_directory_and_manifest_after_every_visible_member_is_gone(
    tmp_path,
):
    directory = tmp_path / "segments"
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    missing_master = tmp_path / "deleted-master.mp4"
    missing_child = captures._group_directory("capture-1") / "timeline-1" / "deleted.mp4"
    captures._commit(
        _group(
            missing_master,
            [_child("deleted", 0, 0.0, 1.0, missing_child)],
        )
    )
    disposable = captures._group_directory("capture-1") / "inputs" / "selection.mp4"
    disposable.parent.mkdir(parents=True)
    disposable.write_bytes(b"derived")
    sidecar_path(disposable).write_bytes(b"sidecar")

    captures.enrich({})

    assert captures.groups == {}
    assert not captures._group_directory("capture-1").exists()
    reopened = CaptureLibrary(tmp_path / "captures.json", directory)
    assert reopened.problem == ""
    assert reopened.groups == {}


def test_enrich_defers_empty_group_purge_until_render_releases_a_derived_path(tmp_path):
    directory = tmp_path / "segments"
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    missing_master = tmp_path / "deleted-master.mp4"
    missing_child = captures._group_directory("capture-1") / "timeline-1" / "deleted.mp4"
    captures._commit(
        _group(
            missing_master,
            [_child("deleted", 0, 0.0, 1.0, missing_child)],
        )
    )
    rendered_input = captures._group_directory("capture-1") / "inputs" / "selection.mp4"
    rendered_input.parent.mkdir(parents=True)
    rendered_input.write_bytes(b"in use")
    captures.external_path_in_use = lambda path: Path(path) == rendered_input

    captures.enrich({})

    assert "capture-1" in captures.groups
    assert captures._group_directory("capture-1").is_dir()
    assert rendered_input.is_file()
    assert "capture-1" in CaptureLibrary(tmp_path / "captures.json", directory).groups

    captures.external_path_in_use = lambda _path: False
    captures.enrich({})

    assert captures.groups == {}
    assert not captures._group_directory("capture-1").exists()


@pytest.mark.asyncio
async def test_calibration_is_versioned_guarded_and_cleans_superseded_children(
    tmp_path, monkeypatch
):
    directory = tmp_path / "segments"
    old = directory / "old-version" / "old.mp4"
    old.parent.mkdir(parents=True)
    old.write_bytes(b"old child")
    sidecar_path(old).write_text("{}", encoding="utf-8")
    gimbal_sidecar_path(old).write_text('{"samples": []}', encoding="utf-8")
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    captures = CaptureLibrary(tmp_path / "captures.json", directory)
    captures._commit(_group(master, [_child("old", 0, 0.0, 2.0, old)]))
    captures.external_path_in_use = lambda path: Path(path) == old

    with pytest.raises(ValueError, match="正在生成视频"):
        captures.retry("capture-1", offset=0.5)
    assert captures.groups["capture-1"]["offset_seconds"] == 0.0
    assert old.is_file()

    captures.external_path_in_use = lambda _path: False
    captures.retry("capture-1", offset=0.5)
    assert captures.groups["capture-1"]["obsolete_paths"] == [str(old)]
    assert captures.groups["capture-1"]["segments"] == []

    monkeypatch.setattr(captures.renderer, "probe_duration", lambda _path: 2.0)

    async def encode(_source, _ranges, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"new child")

    monkeypatch.setattr(captures, "_encode", encode)
    prepared = await captures.prepare("capture-1")

    assert prepared["status"] == "ready"
    assert all(
        Path(segment["path"]).parent.name == prepared["timeline_version"]
        for segment in prepared["segments"]
    )
    assert all("old-version" not in segment["path"] for segment in prepared["segments"])
    assert not old.exists()
    assert not sidecar_path(old).exists()
    assert not gimbal_sidecar_path(old).exists()
    assert "obsolete_paths" not in captures.groups["capture-1"]


def test_derived_capture_document_is_not_published_if_gimbal_write_fails(tmp_path, monkeypatch):
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    gimbal_sidecar_path(master).write_text(
        json.dumps({"samples": [[0.1, 1.0, 2.0]]}), encoding="utf-8"
    )
    target = tmp_path / "segments" / "child.mp4"
    target.parent.mkdir()
    target.write_bytes(b"child")
    captures = CaptureLibrary(tmp_path / "captures.json", target.parent)
    group = _group(
        master,
        [_child("child", 0, 0.0, 1.0, target)],
        duration=1.0,
    )
    real_write_json = capture_library_module.write_json

    def fail_target_gimbal(path, payload):
        if Path(path) == gimbal_sidecar_path(target):
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(capture_library_module, "write_json", fail_target_gimbal)

    with pytest.raises(OSError, match="云台轨迹"):
        captures._write_evidence(group, target, [(0.0, 1.0)])

    assert not sidecar_path(target).exists()
