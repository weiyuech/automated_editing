import asyncio
import json
import re
import tempfile
from contextlib import suppress
from pathlib import Path

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraworkConfig,
    CruisePoint,
    CruiseRequest,
    GimbalScanConfig,
    MediaItem,
    RobotGoalCommand,
    RobotState,
)
from automated_video_editing_backend.services.capture import (
    CaptureService,
    gimbal_sidecar_path,
    sidecar_path,
)
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.robot import HardwareRobotAdapter, RobotService


def heartbeat(goal_status, object_status="done", goal_id=1, yaw=None, pitch=0):
    payload = {
        "system": {"status": "ready", "battery": 90},
        "map": {"mode": "localization", "name": "map1", "status": "ready"},
        "naviagtion": {"status": "ready", "goal_status": goal_status},
        "task": {
            "path_file": "path1",
            "goal_id": goal_id,
            "goal_status": goal_status,
            "object_status": object_status,
        },
    }
    if yaw is not None:
        payload["gimbal"] = {
            "record_status": "recording",
            "yaw": yaw,
            "pitch": pitch,
            "mode": 1,
        }
    return json.dumps(payload)


def armed_adapter():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    return adapter


@pytest.mark.asyncio
async def test_arrival_ignores_the_previous_goal_settled_heartbeat():
    adapter = armed_adapter()
    adapter._arm_goal_tracking(RobotGoalCommand(path_name="path1", goal_id=2))

    # Stale 'done' from the point we just left must not count as arriving at the new one.
    await adapter._handle_message(heartbeat("done"))
    with pytest.raises(TimeoutError):
        await adapter.wait_for_arrival(timeout_s=0.05)

    await adapter._handle_message(heartbeat("going"))
    await adapter._handle_message(heartbeat("done"))
    assert await adapter.wait_for_arrival(timeout_s=0.05) == "done"


@pytest.mark.asyncio
async def test_arrival_waits_for_alignment_only_when_goal_object_is_set():
    with_object = armed_adapter()
    with_object._arm_goal_tracking(
        RobotGoalCommand(path_name="path1", goal_id=1, goal_object="car")
    )
    await with_object._handle_message(heartbeat("going"))
    await with_object._handle_message(heartbeat("done", object_status="going"))
    with pytest.raises(TimeoutError):
        await with_object.wait_for_arrival(timeout_s=0.05)
    await with_object._handle_message(heartbeat("done", object_status="done"))
    assert await with_object.wait_for_arrival(timeout_s=0.05) == "done"

    without_object = armed_adapter()
    without_object._arm_goal_tracking(RobotGoalCommand(path_name="path1", goal_id=1))
    await without_object._handle_message(heartbeat("going"))
    # Same 'still aligning' heartbeat, but this point never asked for alignment.
    await without_object._handle_message(heartbeat("done", object_status="going"))
    assert await without_object.wait_for_arrival(timeout_s=0.05) == "done"


@pytest.mark.asyncio
async def test_lost_connection_resolves_a_pending_arrival():
    adapter = armed_adapter()
    adapter._arm_goal_tracking(RobotGoalCommand(path_name="path1", goal_id=1))
    adapter._fail_pending(ConnectionError("socket closed"))
    assert await adapter.wait_for_arrival(timeout_s=0.05) == "failed"


@pytest.mark.asyncio
async def test_hardware_adapter_keeps_raw_heartbeat_pitch_separate_from_command_tracking():
    adapter = armed_adapter()
    await adapter._handle_message(heartbeat("going", yaw=7, pitch=-3))

    assert adapter.heartbeat_yaw() == 7
    assert adapter.heartbeat_pitch() == -3
    assert adapter.heartbeat_revision() == 1
    await adapter._handle_message(heartbeat("going", yaw=8, pitch=-2))
    assert adapter.heartbeat_revision() == 2


@pytest.mark.asyncio
async def test_reconnect_forgets_stale_recording_status_until_a_new_heartbeat():
    adapter = armed_adapter()
    await adapter._handle_message(heartbeat("going", yaw=7, pitch=-3))
    assert adapter.recording_status_known() is True

    await adapter._stop_connection_loop()
    adapter.state.connected = True  # fresh socket, before its first new gimbal heartbeat
    assert adapter.recording_status_known() is False

    await adapter._handle_message(heartbeat("going", yaw=8, pitch=-2))
    assert adapter.recording_status_known() is True


class FakeAdapter:
    """Duck-typed robot that reports arrival for every goal except those in fail_ids."""

    def __init__(self, fail_ids=()):
        self.state = RobotState(connected=True, yaw=0.0, pitch=0.0)
        self.fail_ids = set(fail_ids)
        self.goals = []
        self.sweeps = []
        self.gimbal_commands = []
        self.recording_calls = []
        self.arrival_timeouts = []
        self.gimbal_revision = 0

    async def status(self):
        return self.state

    async def switch_map(self, map_name):
        self.state.map_name = map_name
        return {"map_name": map_name, "ok": True}

    async def set_goal(self, command):
        self.goals.append(command)
        self._pending = command
        return {"goal_check": "true", "goal_id": command.goal_id}

    async def wait_for_arrival(self, timeout_s=60.0):
        self.arrival_timeouts.append(timeout_s)
        await asyncio.sleep(0)
        return "failed" if self._pending.goal_id in self.fail_ids else "done"

    async def sweep_camera(self, target_yaw, yaw_speed):
        self.sweeps.append((target_yaw, yaw_speed))
        self.state.yaw = target_yaw
        return self.state

    async def set_camera_angle(self, angle):
        self.state.yaw = angle.angle
        return self.state

    def heartbeat_yaw(self):
        return self.state.yaw

    def heartbeat_pitch(self):
        return self.state.pitch

    def heartbeat_revision(self):
        return self.gimbal_revision

    async def set_gimbal(self, command):
        self.gimbal_commands.append(command)
        self.state.yaw = command.yaw_end
        self.state.pitch = command.pitch_end
        self.gimbal_revision += 1
        return self.state

    async def start_recording(self):
        self.recording_calls.append("start")
        self.state.recording = True
        return self.state

    async def stop_recording(self):
        self.recording_calls.append("stop")
        self.state.recording = False
        self.state.media_url = "robot://cruise.mp4"
        return self.state


def build_cruise(fail_ids=(), camerawork_provider=None):
    events = EventHub()
    adapter = FakeAdapter(fail_ids=fail_ids)
    media_root = Path(tempfile.mkdtemp())

    class FakeMedia:
        def __init__(self):
            self.counter = 0

        async def download_url(self, _url, metadata=None, **_kwargs):
            self.counter += 1
            target = media_root / f"cruise-{self.counter}.mp4"
            target.write_bytes(b"video")
            return MediaItem(path=str(target), kind="video", metadata=metadata or {})

    robot = RobotService(events, adapter=adapter, media=FakeMedia())
    robot.resolve_media_url = lambda url: url
    capture = CaptureService(events, path=Path(tempfile.mkdtemp()) / "sessions.json")
    return CruiseService(events, robot, capture, camerawork_provider), adapter, capture


def cruise_request(**overrides):
    request = {
        "points": [CruisePoint(path_name="path1", goal_id=1), CruisePoint(path_name="path1", goal_id=2)],
        "dwell_min_seconds": 0.0,
        "dwell_max_seconds": 0.0,
    }
    request.update(overrides)
    return CruiseRequest(**request)


def test_cruise_arrival_timeout_defaults_to_sixty_seconds():
    assert cruise_request().arrival_timeout_seconds == 60.0


@pytest.mark.asyncio
async def test_legacy_saved_timeout_is_overridden_by_fixed_sixty_seconds():
    cruise, adapter, _ = build_cruise()

    await cruise.start(cruise_request(arrival_timeout_seconds=180.0))
    await cruise._task

    assert adapter.arrival_timeouts == [60.0, 60.0]


@pytest.mark.asyncio
async def test_auto_camerawork_requires_an_explicitly_saved_camera_profile():
    cruise, adapter, _ = build_cruise(
        camerawork_provider=lambda: CameraworkConfig(configured=False)
    )

    with pytest.raises(ValueError, match="镜头设置"):
        await cruise.start(cruise_request(auto_camerawork=True))

    assert adapter.goals == []
    assert adapter.recording_calls == []


@pytest.mark.asyncio
async def test_cruise_records_once_and_marks_each_arrival():
    cruise, adapter, capture = build_cruise()

    run = await cruise.start(cruise_request())
    await cruise._task

    assert run.status == "succeeded"
    assert adapter.recording_calls == ["start", "stop"]
    assert [goal.goal_id for goal in adapter.goals] == [1, 2]
    # Navigation-only points must not ask the robot to align its gimbal.
    assert all(goal.goal_object is None for goal in adapter.goals)
    assert [marker.label for marker in run.markers] == ["path1#1", "path1#2"]
    assert len(capture.list_sessions()[0].markers) == 2
    assert run.media_url == "robot://cruise.mp4"


@pytest.mark.asyncio
async def test_failed_point_is_marked_but_nothing_is_discarded():
    cruise, adapter, _ = build_cruise(fail_ids={1})

    run = await cruise.start(cruise_request())
    await cruise._task

    assert run.status == "succeeded"
    assert [goal.goal_id for goal in adapter.goals] == [1, 2]
    assert run.segments[0].status == "failed"
    assert run.segments[1].status == "arrived"
    # Recording keeps rolling through the failure and no footage is cut: transit and even
    # a failed stretch may be worth keeping, so the failure is only labelled.
    assert adapter.recording_calls == ["start", "stop"]
    assert [marker.label for marker in run.markers] == ["path1#1 失败", "path1#2"]


@pytest.mark.asyncio
async def test_gimbal_scan_is_off_by_default_and_returns_to_centre_when_enabled():
    cruise, adapter, _ = build_cruise()
    await cruise.start(cruise_request())
    await cruise._task
    assert adapter.sweeps == []

    cruise, adapter, _ = build_cruise()
    adapter.state.yaw = 20.0
    scan = GimbalScanConfig(enabled=True, yaw_offset_deg=15.0, yaw_speed_deg_s=30.0)
    run = await cruise.start(cruise_request(gimbal_scan=scan))
    await cruise._task

    assert all(segment.scanned for segment in run.segments)
    # Recording does not move the gimbal. Each point scans from its current angle and returns.
    assert adapter.sweeps == [(35.0, 30.0), (20.0, 30.0), (35.0, 30.0), (20.0, 30.0)]
    assert adapter.state.yaw == 20.0
    samples = json.loads(
        gimbal_sidecar_path(run.media_local_path).read_text(encoding="utf-8")
    )["samples"]
    assert samples


@pytest.mark.asyncio
async def test_plain_cruise_never_inherits_gimbal_samples_from_previous_run():
    cruise, adapter, _ = build_cruise()
    adapter.state.yaw = 20.0
    scan = GimbalScanConfig(enabled=True, yaw_offset_deg=15.0, yaw_speed_deg_s=30.0)
    scanned = await cruise.start(cruise_request(gimbal_scan=scan))
    await cruise._task
    assert gimbal_sidecar_path(scanned.media_local_path).exists()

    plain = await cruise.start(cruise_request())
    await cruise._task
    assert not gimbal_sidecar_path(plain.media_local_path).exists()


@pytest.mark.asyncio
async def test_dwell_is_floored_so_a_scan_is_never_cut_mid_return():
    scan = GimbalScanConfig(enabled=True, yaw_offset_deg=15.0, yaw_speed_deg_s=5.0)
    assert scan.budget_seconds == pytest.approx(7.6)

    request = cruise_request(dwell_min_seconds=1.0, dwell_max_seconds=1.0, gimbal_scan=scan)
    assert request.gimbal_scan.budget_seconds > request.dwell_max_seconds


@pytest.mark.asyncio
async def test_each_run_gets_a_timestamped_session_so_runs_are_distinguishable():
    cruise, _, capture = build_cruise()

    await cruise.start(cruise_request(title="早班清单"))
    await cruise._task
    first = capture.list_sessions()[0].title
    await cruise.start(cruise_request(title="早班清单"))
    await cruise._task
    titles = [session.title for session in capture.list_sessions()]

    # A saved 清单 stores its request, so the stamp must come from run time, not save time.
    assert re.fullmatch(r"早班清单 \d{2}-\d{2} \d{2}:\d{2}", first)
    assert len(titles) == 2
    assert all(title.startswith("早班清单 ") for title in titles)
    # Two runs inside the same minute share a stamp, so the numbering keeps them apart.
    assert len(set(titles)) == 2


@pytest.mark.asyncio
async def test_cruise_refuses_to_hijack_a_running_manual_capture():
    cruise, adapter, capture = build_cruise()
    await capture.start("手动采集")

    with pytest.raises(ValueError):
        await cruise.start(cruise_request())

    # The manual session and its recording must survive untouched.
    assert capture.active_session() is not None
    assert adapter.recording_calls == []

    await capture.stop()
    run = await cruise.start(cruise_request())
    await cruise._task
    assert run.status == "succeeded"
    assert adapter.recording_calls == ["start", "stop"]


@pytest.mark.asyncio
async def test_second_cruise_is_rejected_while_one_is_running():
    cruise, _, _ = build_cruise()
    await cruise.start(cruise_request(dwell_min_seconds=5.0, dwell_max_seconds=5.0))
    try:
        with pytest.raises(ValueError):
            await cruise.start(cruise_request())
    finally:
        run = await cruise.cancel()

    assert run.status == "canceled"
    assert any(segment.status == "skipped" for segment in run.segments)


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)


class RunningCruise:
    is_running = True


class IdleFramingTest:
    def status(self):
        return {"running": False}


@pytest.mark.asyncio
async def test_ws_refuses_manual_capture_while_a_cruise_runs():
    from automated_video_editing_backend.api.ws import _handle_command

    cruise, adapter, capture = build_cruise()
    socket = FakeSocket()

    for command in ("CAPTURE_START", "CAPTURE_STOP"):
        with pytest.raises(ValueError):
            await _handle_command(
                socket,
                command,
                {},
                cruise.robot,
                capture,
                RunningCruise(),
                IdleFramingTest(),
            )

    # The refusal must happen before the robot is touched.
    assert adapter.recording_calls == []
    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_ws_never_starts_robot_when_capture_session_cannot_be_saved(monkeypatch):
    from automated_video_editing_backend.api.ws import _handle_command

    _, _, capture = build_cruise()

    def refuse_save():
        raise OSError("capture index is read-only")

    monkeypatch.setattr(capture, "_save", refuse_save)

    class StartTrackingRobot:
        def __init__(self):
            self.start_calls = 0

        async def start_recording(self):
            self.start_calls += 1
            return RobotState(connected=True, recording=True)

    class IdleCruise:
        is_running = False

    robot = StartTrackingRobot()
    with pytest.raises(OSError, match="read-only"):
        await _handle_command(
            FakeSocket(),
            "CAPTURE_START",
            {"title": "不能保存"},
            robot,
            capture,
            IdleCruise(),
            IdleFramingTest(),
        )

    assert robot.start_calls == 0
    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_ws_can_explicitly_discard_an_observed_idle_pending_capture():
    from automated_video_editing_backend.api.ws import _handle_command

    _, _, capture = build_cruise()
    await capture.start("无法恢复")

    class DiscardRobot:
        async def discard_capture_recovery(self, commit):
            return await commit()

    class IdleCruise:
        is_running = False

    socket = FakeSocket()
    await _handle_command(
        socket,
        "CAPTURE_DISCARD",
        {},
        DiscardRobot(),
        capture,
        IdleCruise(),
        IdleFramingTest(),
    )

    assert capture.active_session() is None
    assert socket.sent[-1]["type"] == "CAPTURE_DISCARDED"


@pytest.mark.asyncio
async def test_ws_reserves_camera_commands_for_the_running_framing_test():
    from automated_video_editing_backend.api.ws import _handle_command

    cruise, adapter, capture = build_cruise()
    socket = FakeSocket()

    class IdleCruise:
        is_running = False

    class RunningFramingTest:
        def status(self):
            return {"running": True}

    for command in (
        "ROBOT_GIMBAL",
        "ROBOT_START_RECORDING",
        "ROBOT_STOP_RECORDING",
        "ROBOT_CAPTURE_PHOTO",
        "CAPTURE_START",
        "CAPTURE_STOP",
        "CRUISE_START",
    ):
        with pytest.raises(ValueError, match="取景测试正在进行"):
            await _handle_command(
                socket,
                command,
                {},
                cruise.robot,
                capture,
                IdleCruise(),
                RunningFramingTest(),
            )

    assert adapter.recording_calls == []


@pytest.mark.asyncio
async def test_ws_keeps_capture_session_when_stop_operation_fails():
    from automated_video_editing_backend.api.ws import _handle_command

    _, _, capture = build_cruise()
    await capture.start("拍摄")
    socket = FakeSocket()

    class SaveFailureRobot:
        async def finalize_capture_recording(self, **_kwargs):
            raise ValueError("机器人拒绝停止录制")

        async def status(self):
            return RobotState(connected=True, recording=True)

    class IdleCruise:
        is_running = False

    with pytest.raises(ValueError, match="拒绝停止录制"):
        await _handle_command(
            socket,
            "CAPTURE_STOP",
            {},
            SaveFailureRobot(),
            capture,
            IdleCruise(),
            IdleFramingTest(),
        )

    assert capture.active_session() is not None


@pytest.mark.asyncio
async def test_ws_reports_recording_success_separately_from_windows_save_failure():
    from automated_video_editing_backend.api.ws import _handle_command

    _, _, capture = build_cruise()
    await capture.start("拍摄")
    socket = FakeSocket()

    class RecordedButNotDownloadedRobot:
        async def finalize_capture_recording(self, **_kwargs):
            return RobotState(
                connected=True,
                recording=False,
                media_url="http://192.168.1.201:82/video.mp4",
                media_sync_error="无法连接到远程服务器",
            )

    class IdleCruise:
        is_running = False

    await _handle_command(
        socket,
        "CAPTURE_STOP",
        {},
        RecordedButNotDownloadedRobot(),
        capture,
        IdleCruise(),
        IdleFramingTest(),
    )

    assert capture.active_session() is not None
    assert socket.sent[-1]["type"] == "CAPTURE_STOPPED"
    assert socket.sent[-1]["data"]["media_url"] == "http://192.168.1.201:82/video.mp4"
    assert socket.sent[-1]["data"]["media_local_path"] is None
    assert socket.sent[-1]["data"]["media_sync_error"] == "无法连接到远程服务器"


@pytest.mark.asyncio
async def test_ws_cancellation_after_confirmed_save_closes_the_capture_session(tmp_path):
    from automated_video_editing_backend.api.ws import _handle_command

    _, _, capture = build_cruise()
    await capture.start("拍摄")
    saved_video = tmp_path / "video.mp4"
    saved_video.write_bytes(b"video")
    stopped = asyncio.Event()
    never_finish = asyncio.Event()

    class CancelledAfterStopRobot:
        def __init__(self):
            self.state = RobotState(connected=True, recording=True)

        async def finalize_capture_recording(self, **_kwargs):
            self.state.recording = False
            self.state.media_local_path = str(saved_video)
            stopped.set()
            await never_finish.wait()

        async def status(self):
            return self.state

    class IdleCruise:
        is_running = False

    task = asyncio.create_task(
        _handle_command(
            FakeSocket(),
            "CAPTURE_STOP",
            {},
            CancelledAfterStopRobot(),
            capture,
            IdleCruise(),
            IdleFramingTest(),
        )
    )
    await stopped.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_a_recording_that_never_started_is_not_stopped():
    """Stopping was driven by what was asked for rather than by what is happening. A start
    that fails leaves nothing to stop, and sending the stop anyway is a command about a state
    the robot is not in."""
    cruise, adapter, _ = build_cruise()

    async def refuse(*_args, **_kwargs):
        raise ConnectionError("recorder unavailable")

    adapter.start_recording = refuse

    run = await cruise.start(cruise_request())
    with suppress(Exception):
        await cruise._task

    assert "stop" not in adapter.recording_calls, adapter.recording_calls
    assert run.status == "failed"


@pytest.mark.asyncio
async def test_failure_before_start_attempt_never_stops_an_unowned_recording():
    cruise, adapter, capture = build_cruise()
    adapter.state.recording = True

    async def fail_map_switch(*_args, **_kwargs):
        raise ConnectionError("map switch failed before capture setup")

    adapter.switch_map = fail_map_switch

    run = await cruise.start(cruise_request(map_name="map1"))
    await cruise._task

    assert run.status == "failed"
    assert adapter.state.recording is True
    assert adapter.recording_calls == []
    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_a_recording_left_running_is_stopped_even_if_the_run_broke_early():
    """The recording is the one thing here that has a stop, so leaving one running is ours to
    prevent — whatever else went wrong."""
    cruise, adapter, _ = build_cruise()

    async def explode(*_args, **_kwargs):
        raise RuntimeError("navigation stack died")

    adapter.set_goal = explode

    await cruise.start(cruise_request())
    await cruise._task

    assert adapter.recording_calls == ["start", "stop"], adapter.recording_calls
    assert adapter.state.recording is False


@pytest.mark.asyncio
async def test_stop_failure_marks_cruise_failed_and_keeps_session_for_manual_recovery():
    cruise, adapter, capture = build_cruise()

    async def refuse_stop(*_args, **_kwargs):
        adapter.recording_calls.append("stop")
        raise ConnectionError("stop command was not confirmed")

    adapter.stop_recording = refuse_stop

    run = await cruise.start(cruise_request())
    await cruise._task

    assert run.status == "failed"
    assert "Stop recording failed" in run.error
    assert adapter.state.recording is True
    assert capture.active_session() is not None


@pytest.mark.asyncio
async def test_manual_retry_after_failed_cruise_stop_preserves_point_spans(tmp_path):
    from automated_video_editing_backend.api.ws import _handle_command

    cruise, adapter, capture = build_cruise()

    async def refuse_stop(*_args, **_kwargs):
        adapter.recording_calls.append("stop")
        raise ConnectionError("stop command was not confirmed")

    adapter.stop_recording = refuse_stop
    run = await cruise.start(cruise_request())
    await cruise._task
    capture.remember_gimbal_samples(
        capture.active_session(),
        [(0.0, -3.0, 1.0), (0.3, -1.0, 1.0)],
    )

    recording = tmp_path / "recovered-cruise.mp4"
    recording.write_bytes(b"video")

    async def recover_stop(*_args, **_kwargs):
        adapter.recording_calls.append("stop")
        adapter.state.recording = False
        adapter.state.media_local_path = str(recording)
        return adapter.state

    adapter.stop_recording = recover_stop
    socket = FakeSocket()
    await _handle_command(
        socket,
        "CAPTURE_STOP",
        {},
        cruise.robot,
        capture,
        cruise,
        IdleFramingTest(),
    )

    payload = json.loads(sidecar_path(recording).read_text(encoding="utf-8"))
    assert capture.active_session() is None
    assert payload["capture_session_id"] == run.capture_session_id
    assert len(payload["segments"]) == len(run.segments)
    assert payload["segments"][0]["path_name"] == "path1"
    assert json.loads(gimbal_sidecar_path(recording).read_text(encoding="utf-8"))["samples"] == [
        [0.0, -3.0, 1.0],
        [0.3, -1.0, 1.0],
    ]


@pytest.mark.asyncio
async def test_explicit_recording_start_rejection_does_not_leave_a_retry_session():
    cruise, adapter, capture = build_cruise()

    async def reject(*_args, **_kwargs):
        raise ValueError("机器人拒绝开始录制")

    adapter.start_recording = reject
    run = await cruise.start(cruise_request())
    await cruise._task

    assert run.status == "failed"
    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_losing_the_point_spans_is_reported_rather_than_passed_over():
    """Footage without spans is edited as ordinary video, which quietly removes most of the
    editing choices available to it. The recording still looks perfectly fine."""
    cruise, adapter, capture = build_cruise()
    cruise.robot.media = None

    async def stop_without_syncing(*_args, **_kwargs):
        adapter.recording_calls.append("stop")
        adapter.state.recording = False
        adapter.state.media_local_path = None      # robot kept the file
        return adapter.state

    adapter.stop_recording = stop_without_syncing

    run = await cruise.start(cruise_request())
    await cruise._task

    assert run.status == "failed"
    assert capture.active_session() is not None
    assert any("点位信息未能写入" in note for note in run.warnings), run.warnings


@pytest.mark.asyncio
async def test_a_healthy_run_reports_no_warnings(tmp_path):
    """The counterpart: warnings must mean something, so a run that wrote its spans has none."""
    cruise, adapter, _ = build_cruise()
    recording = tmp_path / "cruise.mp4"
    recording.write_bytes(b"fake")

    async def stop_and_sync(*_args, **_kwargs):
        adapter.recording_calls.append("stop")
        adapter.state.recording = False
        adapter.state.media_local_path = str(recording)
        return adapter.state

    adapter.stop_recording = stop_and_sync

    run = await cruise.start(cruise_request())
    await cruise._task

    assert run.warnings == []
    assert sidecar_path(recording).exists()
