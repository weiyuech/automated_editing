import json

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import CruiseSegment
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
    # The capture document is the splitter's enrollment marker. Publishing it before the
    # associated physical-motion evidence would let background segmentation race ahead with
    # an incomplete recording, so a telemetry failure must leave it absent.
    assert not sidecar_path(video).exists()


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
