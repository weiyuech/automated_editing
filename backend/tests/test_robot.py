import json

import pytest

from automated_video_editing_backend.core.diagnostics import safe_url
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraAngle,
    MediaItem,
    MoveCommand,
    RobotGoalCommand,
    RobotMode,
    RobotState,
)
from automated_video_editing_backend.services.media import resolve_robot_media_url
from automated_video_editing_backend.services.robot import (
    HardwareRobotAdapter,
    RobotService,
    refuse_if_unfit_to_drive,
)


@pytest.mark.asyncio
async def test_robot_without_hardware_url_starts_disconnected():
    robot = RobotService(EventHub())
    state = await robot.status()

    assert state.adapter == RobotMode.REAL
    assert state.connected is False
    assert state.connection_status == "disconnected"
    assert state.error == "Robot websocket URL is not configured"

    with pytest.raises(ValueError):
        await robot.move(MoveCommand(direction="forward"))


@pytest.mark.asyncio
async def test_hardware_adapter_uses_documented_websocket_protocol():
    received: list[dict] = []

    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_connect():
        return adapter.state

    adapter.connect = fake_connect

    class FakeRobotSocket:
        async def send(self, message):
            payload = json.loads(message)
            received.append(payload)
            if "get_map_list" in payload:
                await adapter._handle_message(json.dumps({"robot_map_list": ["map1"]}))
            elif "set_switch_map" in payload:
                await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))
            elif "get_path_list" in payload:
                await adapter._handle_message(json.dumps({"robot_path_list": ["path1"]}))
            elif "set_goal" in payload:
                await adapter._handle_message(json.dumps({
                    "robot_goal": {
                        "path_file": "path1",
                        "goal_id": 3,
                        "goal_object": "car",
                        "goal_check": "true",
                    }
                }))
            elif "video_record" in payload:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        **payload["video_record"],
                        "status": " OK ",
                        "url": "robot://video.mp4",
                    }
                }))
            elif "take_photo" in payload:
                await adapter._handle_message(json.dumps({
                    "robot_take_photo": {"status": "Ok", "url": "robot://photo.jpg"}
                }))

        async def close(self):
            return None

    adapter._socket = FakeRobotSocket()
    await adapter._handle_message(json.dumps({
        "system": {"status": "ready", "battery": 85},
        "map": {"mode": "localization", "name": "map1", "status": "ready"},
        "naviagtion": {"status": "ready", "goal_status": "going"},
            "task": {
                "path_file": "path1",
                "goal_id": 1,
                "goal_object": "car",
                "goal_status": "done",
                "object_status": "faild",
            },
            "gimbal": {"record_status": "idle", "yaw": 45, "pitch": 10, "mode": 1},
    }))

    try:
        assert await adapter.map_list() == ["map1"]
        assert await adapter.switch_map("map1") == {
            "map_name": "map1",
            "ok": True,
            "raw": "true",
        }
        assert await adapter.path_list("map1") == ["path1"]
        goal = await adapter.set_goal(
            RobotGoalCommand(path_name="path1", goal_id=3, goal_object="car")
        )
        assert goal["goal_check"] == "true"
        state = await adapter.start_recording()
        assert state.recording is True
        photo = await adapter.capture_photo()
        assert photo["url"] == "robot://photo.jpg"
        state = await adapter.stop_recording()
        assert state.recording is False

        assert {"get_map_list": "all"} in received
        assert {"set_switch_map": "map1"} in received
        assert {"get_path_list": "map1"} in received
        assert {
            "set_goal": {"path_name": "path1", "goal_id": 3, "goal_object": "car"}
        } in received
        assert {"video_record": {"start": 0, "resolution": 4}} in received
        assert {"video_record": {"stop": 0}} in received
        assert {"take_photo": {"counter": 1, "gap": 0}} in received
        assert adapter.state.object_status == "failed"
        assert adapter.state.battery == 85
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_robot_photo_url_syncs_through_media_service():
    class FakeAdapter:
        def __init__(self):
            self.state = RobotState(connected=True)
            self.websocket_url = "ws://robot.local:8765"

        async def status(self):
            return self.state

        def heartbeat_yaw(self):
            return self.state.yaw

        async def set_camera_angle(self, angle):
            self.state.yaw = angle.angle
            return self.state

        async def capture_photo(self):
            self.state.media_url = "http://robot.local/photo.png"
            return {"status": "ok", "url": self.state.media_url}

    class FakeMedia:
        def __init__(self):
            self.downloads = []

        async def download_url(self, url, metadata=None, filename_prefix=""):
            self.downloads.append((url, metadata, filename_prefix))
            return MediaItem(
                path="/Users/user/Desktop/automated_video_editing/data/downloads/robot-photo.png",
                kind="image",
                metadata=metadata or {},
            )

    events = EventHub()
    media = FakeMedia()
    robot = RobotService(events, adapter=FakeAdapter(), media=media)

    result = await robot.capture_photo()

    assert result["local_media_item"]["kind"] == "image"
    assert media.downloads == [(
        "http://robot.local/photo.png",
        {
            "source": "data/downloads",
            "origin": "robot_hardware",
            "robot_url": "http://robot.local/photo.png",
            "kind_hint": "image",
        },
        "robot-",
    )]
    assert (await robot.status()).media_local_path.endswith("robot-photo.png")


def test_robot_relative_media_paths_resolve_against_robot_host():
    assert resolve_robot_media_url(
        "/media/capture/video.mp4", "ws://10.73.2.199:8765/ws"
    ) == "http://10.73.2.199:8765/media/capture/video.mp4"
    assert resolve_robot_media_url(
        "10.73.2.199:9000/photo.jpg", "ws://10.73.2.199:8765"
    ) == "http://10.73.2.199:9000/photo.jpg"
    assert resolve_robot_media_url(
        "10.73.2.199/photo.jpg", "ws://10.73.2.199:8765"
    ) == "http://10.73.2.199/photo.jpg"
    assert resolve_robot_media_url(
        "https://cdn.example/video.mp4?signature=abc", "ws://10.73.2.199:8765"
    ) == "https://cdn.example/video.mp4?signature=abc"


@pytest.mark.parametrize(
    "value", ["file:///home/robot/video.mp4", "/home/robot/video.mp4", r"C:\\media\\video.mp4"]
)
def test_robot_filesystem_paths_are_rejected(value):
    with pytest.raises(ValueError, match="本机文件路径"):
        resolve_robot_media_url(value, "ws://10.73.2.199:8765")


def test_diagnostic_urls_redact_credentials_and_signed_queries():
    assert safe_url(
        "https://user:password@robot.local/media/video.mp4?signature=secret"
    ) == "https://robot.local/media/video.mp4?<redacted>"


@pytest.mark.asyncio
async def test_zero_yaw_is_a_real_gimbal_start_angle():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.yaw = 0.0
    adapter.state.camera_angle = -90.0
    sent = []

    async def fake_send(payload):
        sent.append(payload)

    adapter._send = fake_send
    await adapter.set_camera_angle(CameraAngle(angle=15))

    assert sent[0]["gimbal_control"]["yaw_start"] == 0.0


@pytest.mark.asyncio
async def test_photo_keeps_current_angle_and_clears_stale_media():
    class AngleTrackingAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                yaw=-90,
                media_url="http://robot.local/old.mp4",
                media_local_path="C:/old.mp4",
            )
            self.websocket_url = "ws://robot.local:8765"
            self.calls = []

        async def status(self):
            return self.state

        def heartbeat_yaw(self):
            return self.state.yaw

        async def set_camera_angle(self, angle):
            self.calls.append(("angle", angle.angle))
            self.state.yaw = angle.angle
            return self.state

        async def capture_photo(self):
            self.calls.append(("photo", self.state.yaw))
            return {"status": "ok", "url": "http://robot.local/new.jpg"}

    class FakeMedia:
        async def download_url(self, url, metadata=None, filename_prefix=""):
            return MediaItem(path="C:/downloads/new.jpg", kind="image", metadata=metadata or {})

    adapter = AngleTrackingAdapter()
    result = await RobotService(EventHub(), adapter=adapter, media=FakeMedia()).capture_photo()

    assert adapter.calls == [("photo", -90)]
    assert result["local_media_item"]["path"] == "C:/downloads/new.jpg"
    assert adapter.state.media_url is None
    assert adapter.state.media_local_path == "C:/downloads/new.jpg"


@pytest.mark.asyncio
async def test_normal_recording_keeps_current_gimbal_angle():
    class AngleTrackingAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, yaw=135.0)
            self.calls = []

        async def status(self):
            return self.state

        async def set_camera_angle(self, angle):
            self.calls.append(("angle", angle.angle))
            self.state.yaw = angle.angle
            return self.state

        async def start_recording(self):
            self.calls.append(("record", self.state.yaw))
            self.state.recording = True
            return self.state

    adapter = AngleTrackingAdapter()
    await RobotService(EventHub(), adapter=adapter).start_recording()

    assert adapter.calls == [("record", 135.0)]


@pytest.mark.asyncio
async def test_download_failure_does_not_turn_successful_recording_into_capture_failure():
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(connected=True, recording=True)

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://192.168.1.201:82/video.mp4"
            return self.state

    class FailingMedia:
        async def download_url(self, *_args, **_kwargs):
            raise ConnectionError("无法连接到远程服务器")

    adapter = RecordingAdapter()
    state = await RobotService(EventHub(), adapter=adapter, media=FailingMedia()).stop_recording()

    assert state.recording is False
    assert state.media_url == "http://192.168.1.201:82/video.mp4"
    assert state.media_local_path is None
    assert state.media_sync_error == "无法连接到远程服务器"


def _heartbeat(adapter, **blocks):
    """Feed one heartbeat straight into the state parser."""
    adapter._apply_protocol_state(blocks)


def test_heartbeat_records_hardware_and_map_mode():
    """system.status and map.mode were read past entirely, so a faulted robot in mapping mode
    looked identical to a healthy one ready to drive."""
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")

    _heartbeat(
        adapter,
        system={"status": "error", "battery": 41},
        map={"mode": "mapping", "name": "map1", "status": "faild"},
    )

    assert adapter.state.system_status == "error"
    assert adapter.state.map_mode == "mapping"
    assert adapter.state.battery == 41
    # Their spelling, normalised, so the UI never has to show "faild".
    assert adapter.state.map_status == "failed"


def test_mapping_mode_refuses_a_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "ready"}, map={"mode": "mapping", "status": "ready"})

    with pytest.raises(ValueError, match="扫图模式"):
        refuse_if_unfit_to_drive(adapter.state)


def test_hardware_fault_refuses_a_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "error"}, map={"mode": "localization", "status": "ready"})

    with pytest.raises(ValueError, match="硬件状态异常"):
        refuse_if_unfit_to_drive(adapter.state)


def test_lost_localisation_refuses_a_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "ready"}, map={"mode": "localization", "status": "faild"})

    with pytest.raises(ValueError, match="定位失败"):
        refuse_if_unfit_to_drive(adapter.state)


def test_a_healthy_robot_is_fit_to_drive():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "ready"}, map={"mode": "localization", "status": "ready"})

    refuse_if_unfit_to_drive(adapter.state)


def test_a_robot_that_has_said_nothing_yet_is_not_blocked():
    """Before the first heartbeat every field is None. Refusing then would make the app
    unusable until a heartbeat happened to arrive."""
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")

    refuse_if_unfit_to_drive(adapter.state)


def test_task_goal_status_is_normalised_too():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")

    _heartbeat(adapter, task={"goal_status": "faild", "object_status": "faild", "goal_id": 3})

    assert adapter.state.goal_status == "failed"
    assert adapter.state.object_status == "failed"


def test_a_heartbeat_without_a_record_field_leaves_the_recording_alone():
    """`.get` on a missing key returns None, which is not "recording" — so a gimbal block
    carrying only yaw would rewrite a running recording as stopped. Whether to send the stop
    command is decided from this flag and nothing else, so absent has to mean unchanged.
    """
    import asyncio
    import json

    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True

    recording = json.dumps({"gimbal": {"record_status": "recording", "yaw": 0, "pitch": 0}})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        adapter._handle_message(recording)
    )
    assert adapter.state.recording is True

    # The same robot, a beat later, reporting only where the camera is pointed.
    partial = json.dumps({"gimbal": {"yaw": 12, "pitch": 0}})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        adapter._handle_message(partial)
    )
    assert adapter.state.recording is True, "a silent field must not stop a running recording"

    idle = json.dumps({"gimbal": {"record_status": "idle", "yaw": 12}})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        adapter._handle_message(idle)
    )
    assert adapter.state.recording is False


def test_zero_heartbeat_updates_camera_angle_instead_of_preserving_minus_ninety():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.camera_angle = -90.0

    _heartbeat(adapter, gimbal={"yaw": 0, "pitch": 0})

    assert adapter.state.yaw == 0.0
    assert adapter.state.camera_angle == 0.0
