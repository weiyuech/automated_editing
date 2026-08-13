from pathlib import Path

import pytest

from automated_video_editing_backend.core.models import RobotState
from automated_video_editing_backend.services.framing_test import FramingTestService


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

    async def start_recording(self, center_camera=True):
        self.state.recording = True
        return self.state

    async def center_camera(self):
        self.sweeps.append((0.0, "center"))
        self.state.yaw = 0.0
        return self.state

    async def stop_recording(self, sync_media=True):
        self.stop_sync_flags.append(sync_media)
        self.state.recording = False
        self.state.media_url = self.media_url
        return self.state


@pytest.mark.asyncio
async def test_framing_test_is_disposable_and_never_syncs_to_media_library(tmp_path):
    robot = FakeRobot()
    service = FramingTestService(robot)
    service.RECORD_SECONDS = 0

    async def fake_download(_url):
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
        assert robot.sweeps[0][0] == 0
        assert robot.sweeps[1][0] == -45
        assert robot.sweeps[2][0] == 45
        assert robot.sweeps[-1][0] == 0
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
