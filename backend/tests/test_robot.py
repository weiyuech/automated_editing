import asyncio
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
from automated_video_editing_backend.services import robot as robot_module
from automated_video_editing_backend.services.capture import CaptureService
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
async def test_video_url_survives_an_in_recording_photo_when_stop_omits_its_url(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_request(payload, _response_key, timeout_s=5.0):
        del timeout_s
        if "video_record" in payload and "start" in payload["video_record"]:
            return {"status": "ok", "url": "http://camera.local/REC_VIDEO.mp4"}
        if "take_photo" in payload:
            return {"status": "ok", "url": "http://camera.local/PHOTO.jpg"}
        return {"status": "ok"}

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.urls.append(url)
            suffix = "PHOTO.jpg" if url.endswith(".jpg") else "REC_VIDEO.mp4"
            kind = "image" if url.endswith(".jpg") else "video"
            return MediaItem(path=f"C:/downloads/{suffix}", kind=kind, metadata=metadata or {})

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)

    await robot.start_recording()
    photo = await robot.capture_photo()
    stopped = await robot.stop_recording()

    assert photo["local_media_item"]["path"].endswith("PHOTO.jpg")
    assert media.urls == [
        "http://camera.local/PHOTO.jpg",
        "http://camera.local/REC_VIDEO.mp4",
    ]
    assert stopped.media_url == "http://camera.local/REC_VIDEO.mp4"
    assert stopped.media_local_path.endswith("REC_VIDEO.mp4")


@pytest.mark.asyncio
async def test_late_stop_recovery_never_uses_an_in_recording_photo_as_video(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_request(payload, _response_key, timeout_s=5.0):
        del timeout_s
        if "video_record" in payload and "start" in payload["video_record"]:
            return {"status": "ok", "url": "http://camera.local/REC_VIDEO.mp4"}
        if "take_photo" in payload:
            return {"status": "ok", "url": "http://camera.local/PHOTO.jpg"}
        # The camera applies Stop, but its acknowledgement misses the caller's deadline.
        adapter.state.recording = False
        raise TimeoutError("stop acknowledgement arrived late")

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path=f"C:/downloads/{url.rsplit('/', 1)[-1]}",
                kind="image" if url.endswith(".jpg") else "video",
                metadata=metadata or {},
            )

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)

    await robot.start_recording()
    await robot.capture_photo()
    with pytest.raises(TimeoutError, match="arrived late"):
        await robot.stop_recording()
    recovered = await robot.finalize_capture_recording()

    assert media.urls == [
        "http://camera.local/PHOTO.jpg",
        "http://camera.local/REC_VIDEO.mp4",
    ]
    assert recovered.media_url.endswith("REC_VIDEO.mp4")
    assert recovered.media_local_path.endswith("REC_VIDEO.mp4")


@pytest.mark.asyncio
async def test_disconnected_recording_still_blocks_endpoint_changes():
    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://old.local:8765"
            self.state = RobotState(connected=False, recording=True)
            self.configured = []

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.configured.append(url)
            self.websocket_url = url
            return self.state

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="录制进行中"):
        await robot.configure_websocket_url("ws://new.local:8765")
    with pytest.raises(ValueError, match="录制进行中"):
        await robot.configure_websocket_url_and_commit(
            "ws://new.local:8765", lambda: None
        )

    assert adapter.configured == []
    assert adapter.websocket_url == "ws://old.local:8765"


@pytest.mark.asyncio
async def test_failed_settings_commit_rolls_robot_endpoint_back():
    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://old.local:8765"
            self.state = RobotState(connected=False, recording=False)
            self.configured = []

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.configured.append(url)
            self.websocket_url = url
            return self.state

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    def fail_commit():
        raise OSError("settings disk is read-only")

    with pytest.raises(OSError, match="read-only"):
        await robot.configure_websocket_url_and_commit(
            "ws://new.local:8765", fail_commit
        )

    assert adapter.configured == ["ws://new.local:8765", "ws://old.local:8765"]
    assert adapter.websocket_url == "ws://old.local:8765"


@pytest.mark.asyncio
async def test_endpoint_repair_preserves_a_restored_capture_url(monkeypatch):
    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://wrong.local:8765"
            self.state = RobotState(connected=True, recording=False)

        async def status(self):
            return self.state

        def recording_status_known(self):
            return True

        async def configure_websocket_url(self, url):
            self.websocket_url = url
            # The real hardware adapter clears transient media state when its endpoint changes.
            self.state.media_url = None
            self.state.media_local_path = None
            self.state.media_sync_error = None
            return self.state

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path="C:/downloads/REC_REPAIRED.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = ConfigAdapter()
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)
    robot.restore_recoverable_video_url("http://camera.local/REC_REPAIRED.mp4")

    committed = False

    def commit():
        nonlocal committed
        committed = True

    await robot.configure_websocket_url_and_commit(
        "ws://correct.local:8765",
        commit,
        preserve_capture_recovery=True,
    )
    recovered = await robot.finalize_capture_recording()

    assert committed is True
    assert adapter.websocket_url == "ws://correct.local:8765"
    assert media.urls == ["http://camera.local/REC_REPAIRED.mp4"]
    assert recovered.media_local_path.endswith("REC_REPAIRED.mp4")


@pytest.mark.asyncio
async def test_endpoint_repair_preserves_an_already_downloaded_capture(tmp_path):
    saved_video = tmp_path / "REC_ALREADY_SAVED.mp4"
    saved_video.write_bytes(b"video")

    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://wrong.local:8765"
            self.state = RobotState(connected=False, recording=False)

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.websocket_url = url
            self.state.media_url = None
            self.state.media_local_path = None
            self.state.media_sync_error = None
            return self.state

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_ALREADY_SAVED.mp4",
        str(saved_video),
        "sidecar write failed",
    )

    await robot.configure_websocket_url_and_commit(
        "ws://correct.local:8765",
        lambda: None,
        preserve_capture_recovery=True,
    )
    recovered = await robot.finalize_capture_recording()

    assert recovered.media_url == "http://camera.local/REC_ALREADY_SAVED.mp4"
    assert recovered.media_local_path == str(saved_video)
    assert recovered.media_sync_error == "sidecar write failed"


@pytest.mark.asyncio
async def test_failed_endpoint_repair_restores_capture_recovery_state(tmp_path):
    saved_video = tmp_path / "REC_ROLLBACK.mp4"
    saved_video.write_bytes(b"video")

    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://wrong.local:8765"
            self.state = RobotState(connected=False, recording=False)

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.websocket_url = url
            self.state.media_url = None
            self.state.media_local_path = None
            self.state.media_sync_error = None
            return self.state

    def fail_commit():
        raise OSError("settings store unavailable")

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_ROLLBACK.mp4",
        str(saved_video),
        "sidecar write failed",
    )

    with pytest.raises(OSError, match="settings store unavailable"):
        await robot.configure_websocket_url_and_commit(
            "ws://correct.local:8765",
            fail_commit,
            preserve_capture_recovery=True,
        )

    recovered = await robot.finalize_capture_recording()
    assert adapter.websocket_url == "ws://wrong.local:8765"
    assert recovered.media_url == "http://camera.local/REC_ROLLBACK.mp4"
    assert recovered.media_local_path == str(saved_video)


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

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
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
        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
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
async def test_download_failure_does_not_turn_successful_recording_into_capture_failure(
    monkeypatch,
):
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://192.168.1.201:82/old.mp4",
                media_local_path="C:/downloads/old.mp4",
            )

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

    async def retry_without_wait(operation, **_kwargs):
        last_error = None
        for _ in range(5):
            try:
                return await operation()
            except ConnectionError as exc:
                last_error = exc
        raise last_error

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    state = await RobotService(EventHub(), adapter=adapter, media=FailingMedia()).stop_recording()

    assert state.recording is False
    assert state.media_url == "http://192.168.1.201:82/video.mp4"
    assert state.media_local_path is None
    assert state.media_sync_error == "摄像头文件传输连接中断，自动重试后仍未成功"


@pytest.mark.asyncio
async def test_next_recording_waits_for_previous_media_and_cannot_corrupt_its_result(
    monkeypatch,
):
    download_started = asyncio.Event()
    release_download = asyncio.Event()

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(connected=True, recording=True)
            self.start_calls = 0

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://192.168.1.201:82/REC_A.mp4"
            return self.state

        async def start_recording(self):
            self.start_calls += 1
            self.state.recording = True
            self.state.media_url = "http://192.168.1.201:82/REC_B.mp4"
            return self.state

    class BlockingMedia:
        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            download_started.set()
            await release_download.wait()
            return MediaItem(
                path="C:/downloads/robot-REC_A.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=BlockingMedia())

    stopping = asyncio.create_task(robot.stop_recording())
    await download_started.wait()
    starting = asyncio.create_task(robot.start_recording())
    await asyncio.sleep(0)
    assert adapter.start_calls == 0

    # Heartbeats continue while a large file downloads. The stop response must keep the
    # operation's URL/path while returning the newest unrelated live telemetry.
    adapter.state.recording = True
    adapter.state.connected = False
    adapter.state.yaw = 77
    adapter.state.battery = 13
    adapter.state.media_url = "http://192.168.1.201:82/STALE_HEARTBEAT.mp4"
    release_download.set()
    stopped = await stopping
    started = await starting

    assert stopped.recording is False
    assert stopped.connected is False
    assert stopped.yaw == 77
    assert stopped.battery == 13
    assert stopped.media_url.endswith("REC_A.mp4")
    assert stopped.media_local_path.endswith("robot-REC_A.mp4")
    assert stopped.media_sync_error is None
    assert started.media_url.endswith("REC_B.mp4")
    assert started.media_local_path is None
    assert adapter.state.media_url.endswith("REC_B.mp4")
    assert adapter.state.media_local_path is None


@pytest.mark.asyncio
async def test_final_stop_url_is_durable_before_video_download_finishes(
    monkeypatch,
    tmp_path,
):
    download_started = asyncio.Event()
    release_download = asyncio.Event()

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(connected=True, recording=True)

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_FINAL.mp4"
            return self.state

    class BlockingMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            download_started.set()
            await release_download.wait()
            return MediaItem(
                path="C:/downloads/REC_FINAL.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    sessions_path = tmp_path / "capture-sessions.json"
    capture = CaptureService(EventHub(), path=sessions_path)
    session = await capture.start("崩溃恢复验证")
    robot = RobotService(EventHub(), adapter=RecordingAdapter(), media=BlockingMedia())

    stopping = asyncio.create_task(
        robot.finalize_capture_recording(
            on_media_url=lambda url: capture.remember_pending_media(session, url),
        )
    )
    try:
        await download_started.wait()
        restored = CaptureService(EventHub(), path=sessions_path).active_session()
        assert restored is not None
        assert restored.pending_media_url == "http://camera.local/REC_FINAL.mp4"
        assert not stopping.done()
    finally:
        release_download.set()
        await stopping


@pytest.mark.asyncio
async def test_media_url_persistence_failure_does_not_skip_video_download(monkeypatch):
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(connected=True, recording=True)

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_FINAL.mp4"
            return self.state

    class RecordingMedia:
        def __init__(self):
            self.downloads = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.downloads.append(url)
            return MediaItem(
                path="C:/downloads/REC_FINAL.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    def fail_persist(_url):
        raise OSError("capture store unavailable")

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = RecordingMedia()
    state = await RobotService(
        EventHub(),
        adapter=RecordingAdapter(),
        media=media,
    ).finalize_capture_recording(on_media_url=fail_persist)

    assert media.downloads == ["http://camera.local/REC_FINAL.mp4"]
    assert state.media_local_path == "C:/downloads/REC_FINAL.mp4"


@pytest.mark.asyncio
async def test_stuck_photo_transfer_does_not_delay_stop_or_overwrite_video(
    monkeypatch,
):
    photo_download_started = asyncio.Event()
    release_photo = asyncio.Event()
    stop_called = asyncio.Event()

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://camera.local/REC_ACTIVE.mp4",
            )

        async def status(self):
            return self.state

        async def capture_photo(self):
            self.state.media_url = "http://camera.local/PHOTO.jpg"
            return {"status": "ok", "url": self.state.media_url}

        async def stop_recording(self):
            stop_called.set()
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_DONE.mp4"
            return self.state

    class BlockingPhotoMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            if url.endswith("PHOTO.jpg"):
                photo_download_started.set()
                await release_photo.wait()
                return MediaItem(
                    path="C:/downloads/PHOTO.jpg",
                    kind="image",
                    metadata=metadata or {},
                )
            return MediaItem(
                path="C:/downloads/REC_DONE.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=BlockingPhotoMedia())
    photo_task = asyncio.create_task(robot.capture_photo())
    try:
        await photo_download_started.wait()
        stopped = await asyncio.wait_for(robot.stop_recording(), timeout=1.0)
        assert stop_called.is_set()
        assert not photo_task.done()
        assert stopped.media_url.endswith("REC_DONE.mp4")
        assert stopped.media_local_path.endswith("REC_DONE.mp4")
    finally:
        release_photo.set()
        await photo_task

    live = await robot.status()
    assert live.media_url.endswith("REC_DONE.mp4")
    assert live.media_local_path.endswith("REC_DONE.mp4")


@pytest.mark.asyncio
async def test_restored_url_waits_for_observed_idle_before_download(monkeypatch):
    class RecoveringAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(connected=True, recording=False)
            self.known = False

        async def status(self):
            return self.state

        def recording_status_known(self):
            return self.known

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path="C:/downloads/REC_RESTORED.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecoveringAdapter()
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)
    robot.restore_recoverable_video_url("http://camera.local/REC_RESTORED.mp4")

    with pytest.raises(ConnectionError, match="等待机器人同步录制状态"):
        await robot.finalize_capture_recording()
    assert media.urls == []

    adapter.known = True
    recovered = await robot.finalize_capture_recording()
    assert media.urls == ["http://camera.local/REC_RESTORED.mp4"]
    assert recovered.media_local_path.endswith("REC_RESTORED.mp4")


@pytest.mark.asyncio
async def test_new_start_timeout_cannot_recover_previous_recording(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub())
    adapter.state.connected = True
    adapter._recording_media_url = "http://camera.local/REC_OLD.mp4"

    async def timeout_start(*_args, **_kwargs):
        raise TimeoutError("start acknowledgement timed out")

    monkeypatch.setattr(adapter, "_request", timeout_start)
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url("http://camera.local/REC_OLD.mp4")

    with pytest.raises(TimeoutError, match="timed out"):
        await robot.start_recording()

    adapter._apply_protocol_state({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0},
    })
    with pytest.raises(ValueError, match="没有可恢复的视频地址"):
        await robot.finalize_capture_recording()
    assert adapter.pending_recording_media_url() is None


@pytest.mark.asyncio
async def test_verified_local_recovery_finishes_before_first_post_restart_heartbeat(tmp_path):
    video = tmp_path / "already-downloaded.mp4"
    video.write_bytes(b"video")

    class RecoveringAdapter:
        def __init__(self):
            self.state = RobotState(connected=False, recording=False)

        async def status(self):
            return self.state

        def recording_status_known(self):
            return False

    robot = RobotService(EventHub(), adapter=RecoveringAdapter())
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_DONE.mp4",
        str(video),
        "old transfer warning",
    )

    recovered = await robot.finalize_capture_recording()
    assert recovered.media_local_path == str(video)


@pytest.mark.asyncio
async def test_discard_requires_observed_idle_and_forgets_recovery_only_after_commit():
    class RecoveringAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=False)
            self.known = False

        async def status(self):
            return self.state

        def recording_status_known(self):
            return self.known

    adapter = RecoveringAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url("http://camera.local/REC_LOST.mp4")
    committed = False

    async def commit():
        nonlocal committed
        committed = True
        return "discarded"

    with pytest.raises(ConnectionError, match="等待机器人同步录制状态"):
        await robot.discard_capture_recovery(commit)
    assert committed is False

    adapter.known = True
    assert await robot.discard_capture_recovery(commit) == "discarded"
    assert committed is True
    with pytest.raises(ValueError, match="没有可恢复的视频地址"):
        await robot.finalize_capture_recording()


@pytest.mark.asyncio
async def test_repeated_stop_is_refused_without_erasing_the_saved_result():
    class IdleAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                recording=False,
                media_url="http://camera.local/REC_DONE.mp4",
                media_local_path="C:/downloads/REC_DONE.mp4",
            )
            self.stop_calls = 0

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.stop_calls += 1
            return self.state

    adapter = IdleAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="当前未在录制"):
        await robot.stop_recording()

    assert adapter.stop_calls == 0
    assert adapter.state.media_url.endswith("REC_DONE.mp4")
    assert adapter.state.media_local_path.endswith("REC_DONE.mp4")


@pytest.mark.asyncio
async def test_stop_preserves_a_recording_url_returned_at_start(monkeypatch):
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://192.168.1.201:82/REC_FROM_START.mp4",
            )

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            return self.state

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path="C:/downloads/robot-REC_FROM_START.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecordingAdapter()
    media = RecordingMedia()
    stopped = await RobotService(EventHub(), adapter=adapter, media=media).stop_recording()

    assert media.urls == ["http://192.168.1.201:82/REC_FROM_START.mp4"]
    assert stopped.media_url.endswith("REC_FROM_START.mp4")
    assert stopped.media_local_path.endswith("robot-REC_FROM_START.mp4")


@pytest.mark.asyncio
async def test_disposable_preview_download_holds_capture_lock_and_resolves_relative_url():
    preview_started = asyncio.Event()
    release_preview = asyncio.Event()
    downloaded_urls = []

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765/ws"
            self.state = RobotState(connected=True, recording=True)
            self.start_calls = 0

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "/mp4/REC_PREVIEW.mp4"
            return self.state

        async def start_recording(self):
            self.start_calls += 1
            self.state.recording = True
            return self.state

    async def download_preview(url):
        downloaded_urls.append(url)
        preview_started.set()
        await release_preview.wait()
        return "temporary-preview"

    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    stopping = asyncio.create_task(robot.stop_recording_with_download(download_preview))
    await preview_started.wait()
    starting = asyncio.create_task(robot.start_recording())
    await asyncio.sleep(0)
    assert adapter.start_calls == 0

    release_preview.set()
    stopped, preview = await stopping
    await starting

    assert stopped.media_url == "/mp4/REC_PREVIEW.mp4"
    assert preview == "temporary-preview"
    assert downloaded_urls == ["http://10.73.2.199:8765/mp4/REC_PREVIEW.mp4"]
    assert adapter.start_calls == 1


@pytest.mark.asyncio
async def test_start_recording_refuses_an_already_active_recording():
    class RecordingAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=True)
            self.start_calls = 0

        async def status(self):
            return self.state

        async def start_recording(self):
            self.start_calls += 1
            return self.state

    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="已在录制"):
        await robot.start_recording()
    assert adapter.start_calls == 0


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
