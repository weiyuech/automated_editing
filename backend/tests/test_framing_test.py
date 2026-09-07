from pathlib import Path

import httpx
import pytest

from automated_video_editing_backend.core.models import RobotState
from automated_video_editing_backend.services import framing_test as framing_test_module
from automated_video_editing_backend.services.framing_test import FramingTestService
from automated_video_editing_backend.services.media_download import (
    MediaDownloadNotReadyError,
    retry_camera_media_download,
)


class FakeRobot:
    def __init__(self, media_url: str = "https://robot.local/test.mp4") -> None:
        self.state = RobotState(connected=True, yaw=12, recording=False)
        self.media_url = media_url
        self.sweeps: list[tuple[float, float]] = []
        self.stop_sync_flags: list[bool] = []

    async def status(self):
        return self.state

    def heartbeat_yaw(self):
        return self.state.yaw

    async def sweep_camera(self, target_yaw, yaw_speed):
        self.sweeps.append((target_yaw, yaw_speed))
        self.state.yaw = target_yaw
        return self.state

    async def start_recording(self):
        self.state.recording = True
        return self.state

    async def stop_recording(self, sync_media=True):
        self.stop_sync_flags.append(sync_media)
        self.state.recording = False
        self.state.media_url = self.media_url
        return self.state

    async def stop_recording_with_download(self, downloader):
        state = await self.stop_recording(sync_media=False)
        if not state.media_url:
            raise RuntimeError("机器人没有返回测试视频地址")
        return state, await downloader(state.media_url)


@pytest.mark.asyncio
async def test_framing_test_is_disposable_and_never_syncs_to_media_library(tmp_path):
    robot = FakeRobot()
    service = FramingTestService(robot)
    service.RECORD_SECONDS = 0

    async def fake_download(_url):
        assert robot.sweeps[-1][0] == 12
        path = service.root / "preview.mp4"
        path.write_bytes(b"temporary video")
        return path

    service._download = fake_download
    try:
        status = await service.start()
        preview = service.preview_path

        assert status["ready"] is True
        assert status["running"] is False
        assert preview and preview.parent == service.root
        assert robot.sweeps[0][0] == -45
        assert robot.sweeps[1][0] == 45
        assert robot.sweeps[-1][0] == 12
        assert robot.stop_sync_flags == [False]

        await service.discard()
        assert not preview.exists()
        assert service.status()["ready"] is False
    finally:
        root = service.root
        await service.close()
        assert not root.exists()


@pytest.mark.asyncio
async def test_failed_framing_test_leaves_no_preview():
    robot = FakeRobot(media_url="")
    service = FramingTestService(robot)
    service.RECORD_SECONDS = 0
    try:
        with pytest.raises(RuntimeError, match="没有返回"):
            await service.start()

        assert robot.sweeps[-1][0] == 12
        assert service.status() == {
            "running": False,
            "ready": False,
            "preview_id": "",
            "record_seconds": 0,
            "expected_wait_seconds": 15,
        }
        assert list(Path(service.root).iterdir()) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_framing_preview_is_not_downloaded_before_the_camera_restores():
    class RestoreFailureRobot(FakeRobot):
        def __init__(self):
            super().__init__()
            self.restore_attempts = 0

        async def sweep_camera(self, target_yaw, yaw_speed):
            if target_yaw == 12:
                self.restore_attempts += 1
                raise ConnectionError("gimbal restore failed")
            return await super().sweep_camera(target_yaw, yaw_speed)

    robot = RestoreFailureRobot()
    service = FramingTestService(robot)
    service.RECORD_SECONDS = 0
    downloaded = False

    async def should_not_download(_url):
        nonlocal downloaded
        downloaded = True
        return service.root / "must-not-exist.mp4"

    service._download = should_not_download
    try:
        with pytest.raises(ConnectionError, match="restore failed"):
            await service.start()

        assert downloaded is False
        assert robot.restore_attempts == 2  # required restore, then final best effort
        assert service.status()["ready"] is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_framing_download_retries_a_transient_camera_connection(monkeypatch):
    service = FramingTestService(FakeRobot())
    attempts = 0

    async def download_once(_url):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("All connection attempts failed")
        path = service.root / "preview-retried.mp4"
        path.write_bytes(b"video")
        return path

    async def retry_without_wait(operation, **kwargs):
        return await retry_camera_media_download(
            operation,
            initial_delay_seconds=0,
            retry_delays_seconds=(0,),
            on_retry=kwargs.get("on_retry"),
        )

    monkeypatch.setattr(service, "_download_once", download_once)
    monkeypatch.setattr(framing_test_module, "retry_camera_media_download", retry_without_wait)
    try:
        path = await service._download("https://robot.local/test.mp4")
        assert attempts == 2
        assert path.read_bytes() == b"video"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_framing_download_exhaustion_has_a_nonempty_operator_error(monkeypatch):
    service = FramingTestService(FakeRobot())
    attempts = 0

    async def download_once(_url):
        nonlocal attempts
        attempts += 1
        raise httpx.ReadTimeout("")

    async def retry_without_wait(operation, **kwargs):
        return await retry_camera_media_download(
            operation,
            initial_delay_seconds=0,
            retry_delays_seconds=(0, 0),
            on_retry=kwargs.get("on_retry"),
        )

    monkeypatch.setattr(service, "_download_once", download_once)
    monkeypatch.setattr(framing_test_module, "retry_camera_media_download", retry_without_wait)
    try:
        with pytest.raises(RuntimeError, match="连接摄像头文件服务超时"):
            await service._download("https://robot.local/test.mp4")
        assert attempts == 3
        assert list(service.root.iterdir()) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_framing_download_rejects_an_unfinished_video_container(monkeypatch):
    service = FramingTestService(FakeRobot())
    real_async_client = httpx.AsyncClient
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=b"not-a-finished-video-container",
        )
    )

    def build_client(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(framing_test_module.httpx, "AsyncClient", build_client)
    try:
        with pytest.raises(MediaDownloadNotReadyError, match="容器尚未写入完整"):
            await service._download_once("https://robot.local/test.mp4")
        assert list(service.root.iterdir()) == []
    finally:
        await service.close()
