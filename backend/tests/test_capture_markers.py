import json

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import CruiseSegment
from automated_video_editing_backend.services.analysis import AnalysisService
from automated_video_editing_backend.services import capture as capture_module
from automated_video_editing_backend.services.capture import (
    CaptureService,
    gimbal_sidecar_path,
    read_sidecar,
    sidecar_path,
)


@pytest.mark.asyncio
async def test_sessions_notes_and_markers_survive_a_restart(tmp_path):
    path = tmp_path / "capture-sessions.json"

    capture = CaptureService(EventHub(), path=path)
    await capture.start("产品晨拍")
    await capture.add_note("这款鞋卖得最好")
    await capture.add_marker(12.4, "path1#1")
    await capture.stop()

    reloaded = CaptureService(EventHub(), path=path)
    sessions = reloaded.list_sessions()

    assert len(sessions) == 1
    assert sessions[0].notes == ["这款鞋卖得最好"]
    assert [marker.label for marker in sessions[0].markers] == ["path1#1"]
    # Nothing can still be recording after a restart.
    assert sessions[0].active is False
    assert reloaded.active_session() is None


@pytest.mark.asyncio
async def test_unfinished_capture_and_retry_metadata_survive_a_restart(tmp_path):
    path = tmp_path / "capture-sessions.json"
    capture = CaptureService(EventHub(), path=path)
    session = await capture.start("待恢复巡游")
    capture.remember_pending_media(
        session,
        "http://camera.local/REC_PENDING.mp4",
        "摄像头文件传输连接中断",
    )
    capture.remember_gimbal_samples(session, [(0.0, -10.0, 2.0), (0.3, -8.0, 2.0)])

    reloaded = CaptureService(EventHub(), path=path)
    pending = reloaded.active_session()
    assert pending is not None
    assert pending.id == session.id
    assert pending.pending_media_url.endswith("REC_PENDING.mp4")
    assert pending.pending_media_sync_error == "摄像头文件传输连接中断"
    assert pending.gimbal_samples == [(0.0, -10.0, 2.0), (0.3, -8.0, 2.0)]

    video = tmp_path / "recovered.mp4"
    video.write_bytes(b"video")
    stopped = await reloaded.complete_with_recording(str(video))
    assert stopped is not None
    assert sidecar_path(video).exists()
    assert json.loads(gimbal_sidecar_path(video).read_text(encoding="utf-8"))["samples"] == [
        [0.0, -10.0, 2.0],
        [0.3, -8.0, 2.0],
    ]

    finalized = CaptureService(EventHub(), path=path).list_sessions()[0]
    assert finalized.active is False
    assert finalized.gimbal_samples == []
    assert finalized.pending_media_url is None


@pytest.mark.asyncio
async def test_failed_stop_persistence_does_not_resurrect_a_false_completion(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "capture-sessions.json"
    capture = CaptureService(EventHub(), path=path)
    session = await capture.start("磁盘写入失败")
    monkeypatch.setattr(capture_module, "write_json", lambda *_args, **_kwargs: False)

    with pytest.raises(OSError, match="无法保存采集会话"):
        await capture.stop()

    assert capture.active_session() is session
    # The last durable state is still active, matching memory/UI rather than pretending the
    # capture closed and then unexpectedly resurrecting only after the next process start.
    assert CaptureService(EventHub(), path=path).active_session() is not None


@pytest.mark.asyncio
async def test_explicit_discard_closes_only_the_session_and_clears_large_retry_state(tmp_path):
    path = tmp_path / "capture-sessions.json"
    capture = CaptureService(EventHub(), path=path)
    session = await capture.start("无法恢复")
    capture.remember_pending_media(session, "http://camera.local/REC_LOST.mp4", "连接失败")
    capture.remember_gimbal_samples(session, [(0.0, 1.0, 2.0)])

    discarded = await capture.discard()

    assert discarded is not None
    assert capture.active_session() is None
    reloaded = CaptureService(EventHub(), path=path).list_sessions()[0]
    assert reloaded.active is False
    assert reloaded.pending_media_url is None
    assert reloaded.gimbal_samples == []


@pytest.mark.asyncio
async def test_a_corrupt_session_file_is_not_fatal(tmp_path):
    path = tmp_path / "capture-sessions.json"
    path.write_text("{not json", encoding="utf-8")
    assert CaptureService(EventHub(), path=path).list_sessions() == []

    path.write_text(json.dumps([{"nonsense": True}]), encoding="utf-8")
    assert CaptureService(EventHub(), path=path).list_sessions() == []


@pytest.mark.asyncio
async def test_transient_session_read_failure_cannot_overwrite_the_existing_store(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "capture-sessions.json"
    original = b'[{"id":"owned-recording","title":"must survive"}]'
    path.write_bytes(original)
    monkeypatch.setattr(
        capture_module,
        "read_json",
        lambda _path: (None, "capture-sessions.json 无法读取：文件暂时被占用"),
    )
    capture = CaptureService(EventHub(), path=path)

    with pytest.raises(OSError, match="为避免覆盖原有采集记录"):
        await capture.start("不能覆盖")

    assert capture.active_session() is None
    assert path.read_bytes() == original


@pytest.mark.asyncio
async def test_notes_and_markers_are_written_beside_the_recording(tmp_path):
    capture = CaptureService(EventHub(), path=tmp_path / "sessions.json")
    video = tmp_path / "robot-cruise.mp4"
    video.write_bytes(b"fake-video")

    session = await capture.start("早班清单")
    await capture.add_note("第三排是新款")
    await capture.add_marker(12.4, "path1#1")
    await capture.add_marker(48.2, "path1#3 失败")
    await capture.stop()

    written = capture.attach_to_recording(session, str(video))
    assert written == sidecar_path(video)

    payload = read_sidecar(video)
    assert payload["notes"] == ["第三排是新款"]
    assert [marker["label"] for marker in payload["markers"]] == ["path1#1", "path1#3 失败"]
    assert payload["capture_session_id"] == session.id


@pytest.mark.asyncio
async def test_no_sidecar_is_written_when_the_recording_produced_no_file(tmp_path):
    capture = CaptureService(EventHub(), path=tmp_path / "sessions.json")
    session = await capture.start("失败的巡游")
    await capture.add_marker(1.0, "path1#1")
    await capture.stop()

    missing = tmp_path / "never-created.mp4"
    assert capture.attach_to_recording(session, str(missing)) is None
    assert not sidecar_path(missing).exists()


@pytest.mark.asyncio
async def test_gimbal_sidecar_failure_is_reported_and_samples_remain_retryable(
    tmp_path,
    monkeypatch,
):
    capture = CaptureService(EventHub(), path=tmp_path / "sessions.json")
    video = tmp_path / "cruise.mp4"
    video.write_bytes(b"video")
    session = await capture.start("巡游")
    capture.remember_gimbal_samples(session, [(0.0, 1.0, 2.0)])
    real_write_json = capture_module.write_json

    def fail_only_gimbal(path, payload):
        if str(path).endswith(".gimbal.json"):
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(capture_module, "write_json", fail_only_gimbal)
    with pytest.raises(OSError, match="拍摄信息写入失败"):
        await capture.complete_with_recording(str(video))
    assert capture.active_session() is session
    assert session.gimbal_samples == [(0.0, 1.0, 2.0)]


def test_markers_and_detected_cuts_are_merged(tmp_path):
    video = tmp_path / "cruise.mp4"
    video.write_bytes(b"fake")
    sidecar_path(video).write_text(
        json.dumps({"markers": [{"timestamp": 12.4, "label": "path1#1"},
                                {"timestamp": 48.2, "label": "path1#3 失败"}]}),
        encoding="utf-8",
    )

    detected = [{"start": 0.0, "end": 30.1}, {"start": 30.1, "end": 65.7}, {"start": 65.7, "end": 130.0}]
    warnings: list[str] = []
    merged = AnalysisService()._merge_capture_markers(video, detected, warnings)

    boundaries = [scene["start"] for scene in merged]
    # Every marker becomes a boundary, and the detected cuts survive as subdivisions.
    assert 12.4 in boundaries and 48.2 in boundaries
    assert 30.1 in boundaries and 65.7 in boundaries
    assert merged[0]["start"] == 0.0
    assert merged[-1]["end"] == 130.0
    # Stretches inherit the label of the point they belong to.
    labelled = {scene["start"]: scene.get("label") for scene in merged}
    assert labelled[12.4] == "path1#1"
    assert labelled[30.1] == "path1#1"
    assert labelled[48.2] == "path1#3 失败"
    assert any(scene["from_marker"] for scene in merged)


@pytest.mark.asyncio
async def test_cruise_spans_are_written_beside_the_recording(tmp_path):
    """Markers record arrivals and nothing else. Without the spans there is no departure,
    so nothing downstream can tell a parked shot from a moving one."""
    capture = CaptureService(EventHub(), path=tmp_path / "sessions.json")
    video = tmp_path / "cruise.mp4"
    video.write_bytes(b"fake-video")

    session = await capture.start("巡游")
    await capture.add_marker(20.0, "path1#1")
    await capture.stop()

    capture.attach_to_recording(session, str(video), [
        CruiseSegment(index=0, path_name="path1", goal_id=1, status="arrived",
                      transit_start_seconds=0.0, arrived_at_seconds=20.0,
                      departed_at_seconds=28.0),
    ])

    written = read_sidecar(video)["segments"]
    assert [segment["status"] for segment in written] == ["arrived"]
    assert written[0]["arrived_at_seconds"] == 20.0
    assert written[0]["departed_at_seconds"] == 28.0


@pytest.mark.asyncio
async def test_a_manual_capture_writes_no_spans(tmp_path):
    """Nothing drove the camera, so there is nothing to say about where it was pointed."""
    capture = CaptureService(EventHub(), path=tmp_path / "sessions.json")
    video = tmp_path / "manual.mp4"
    video.write_bytes(b"fake-video")

    session = await capture.start("原地采集")
    await capture.stop()
    capture.attach_to_recording(session, str(video))

    assert read_sidecar(video)["segments"] == []


def test_spans_classify_parked_travelling_and_failed_footage(tmp_path):
    video = tmp_path / "cruise.mp4"
    video.write_bytes(b"fake")
    sidecar_path(video).write_text(
        json.dumps({"segments": [
            {"index": 0, "path_name": "path1", "goal_id": 1, "status": "arrived",
             "transit_start_seconds": 0.0, "arrived_at_seconds": 20.0, "departed_at_seconds": 28.0},
            {"index": 1, "path_name": "path1", "goal_id": 2, "status": "failed",
             "transit_start_seconds": 28.0, "arrived_at_seconds": None, "departed_at_seconds": 60.0},
            {"index": 2, "path_name": "path1", "goal_id": 3, "status": "arrived",
             "transit_start_seconds": 60.0, "arrived_at_seconds": 80.0, "departed_at_seconds": 90.0},
        ]}),
        encoding="utf-8",
    )

    detected = [{"start": 0.0, "end": 45.0}, {"start": 45.0, "end": 100.0}]
    merged = AnalysisService()._merge_capture_markers(video, detected, [])
    kinds = {(scene["start"], scene["end"]): scene["kind"] for scene in merged}

    assert kinds[(0.0, 20.0)] == "transit"
    assert kinds[(20.0, 28.0)] == "dwell"
    # A detected cut subdivides the failed leg without changing what it is.
    assert kinds[(28.0, 45.0)] == "failed"
    assert kinds[(45.0, 60.0)] == "failed"
    assert kinds[(60.0, 80.0)] == "transit"
    assert kinds[(80.0, 90.0)] == "dwell"
    # Footage rolling after the last point ended belongs to no span.
    assert kinds[(90.0, 100.0)] == "unknown"

    labels = {scene["start"]: scene.get("label") for scene in merged}
    assert labels[20.0] == "path1#1"
    assert labels[28.0] == "path1#2"
    assert labels[80.0] == "path1#3"


def test_a_dwell_with_no_recorded_departure_stays_classified(tmp_path):
    """A run that died at a point still knows the robot was parked when it stopped."""
    video = tmp_path / "died.mp4"
    video.write_bytes(b"fake")
    sidecar_path(video).write_text(
        json.dumps({"segments": [
            {"index": 0, "path_name": "path1", "goal_id": 1, "status": "arrived",
             "transit_start_seconds": 0.0, "arrived_at_seconds": 10.0, "departed_at_seconds": None},
        ]}),
        encoding="utf-8",
    )

    merged = AnalysisService()._merge_capture_markers(video, [{"start": 0.0, "end": 40.0}], [])
    kinds = {(scene["start"], scene["end"]): scene["kind"] for scene in merged}

    assert kinds[(0.0, 10.0)] == "transit"
    assert kinds[(10.0, 40.0)] == "dwell"


def test_an_old_sidecar_without_spans_falls_back_to_markers(tmp_path):
    """Footage shot before spans were recorded must keep working."""
    video = tmp_path / "old.mp4"
    video.write_bytes(b"fake")
    sidecar_path(video).write_text(
        json.dumps({"markers": [{"timestamp": 12.4, "label": "path1#1"}]}), encoding="utf-8",
    )

    merged = AnalysisService()._merge_capture_markers(video, [{"start": 0.0, "end": 30.0}], [])

    assert 12.4 in [scene["start"] for scene in merged]
    assert all("kind" not in scene for scene in merged)


def test_a_video_without_a_sidecar_is_left_alone(tmp_path):
    video = tmp_path / "imported.mp4"
    video.write_bytes(b"fake")
    detected = [{"start": 0.0, "end": 6.0, "score": 1.0}]

    assert AnalysisService()._merge_capture_markers(video, detected, []) == detected
