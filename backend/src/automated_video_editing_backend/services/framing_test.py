from __future__ import annotations

import asyncio
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx

from automated_video_editing_backend.services.robot import RobotService


class FramingTestService:
    """Own the disposable camera-sweep clip used to choose an export crop.

    Nothing here is registered with MediaService or written under the app's managed folders.
    There can be one preview at a time; a replacement, confirmation, cancellation, failure or
    backend shutdown deletes it.
    """

    RECORD_SECONDS = 10.0
    LEFT_YAW = -45.0
    RIGHT_YAW = 45.0
    SWEEP_SPEED_DEG_S = 9.0
    PREPOSITION_SPEED_DEG_S = 30.0
    MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024

    def __init__(self, robot: RobotService) -> None:
        self.robot = robot
        self.root = Path(tempfile.mkdtemp(prefix="ave-framing-test-"))
        self._preview_path: Path | None = None
        self._preview_id = ""
        self._running = False
        self._lock = asyncio.Lock()

    @property
    def preview_path(self) -> Path | None:
        path = self._preview_path
        return path if path and path.is_file() else None

    def status(self) -> dict:
        return {
            "running": self._running,
            "ready": self.preview_path is not None,
            "preview_id": self._preview_id if self.preview_path else "",
            "record_seconds": self.RECORD_SECONDS,
            "expected_wait_seconds": 15,
        }

    async def start(self) -> dict:
        if self._lock.locked():
            raise ValueError("取景测试正在进行，请等待约 10–15 秒")

        async with self._lock:
            self._running = True
            try:
                await self._run_test()
            finally:
                self._running = False
            return self.status()

    async def _run_test(self) -> None:
        await self.discard()
        state = await self.robot.status()
        if not state.connected:
            raise ConnectionError(state.error or "机器人尚未连接")
        if state.recording:
            raise ValueError("机器人正在录制，请先停止当前采集")

        recording_started = False
        media_url = ""
        try:
            await self.robot.center_camera()
            await self.robot.sweep_camera(self.LEFT_YAW, self.PREPOSITION_SPEED_DEG_S)
            if not await self._await_yaw(self.LEFT_YAW, timeout_s=3.5):
                raise RuntimeError("云台未能到达取景测试起点 -45°")

            recording = await self.robot.start_recording(center_camera=False)
            if not recording.recording:
                raise RuntimeError(recording.error or "机器人未确认开始录制")
            recording_started = True

            await self.robot.sweep_camera(self.RIGHT_YAW, self.SWEEP_SPEED_DEG_S)
            if not await self._await_yaw(self.RIGHT_YAW, timeout_s=self.RECORD_SECONDS + 3.0):
                raise RuntimeError("云台未能完成 -45° 到 45° 的取景测试")

            stopped = await self.robot.stop_recording(sync_media=False)
            recording_started = False
            media_url = str(stopped.media_url or "")
            if not media_url:
                raise RuntimeError("机器人没有返回测试视频地址")
        except Exception:
            await self.discard()
            raise
        finally:
            if recording_started:
                with suppress(Exception):
                    await self.robot.stop_recording(sync_media=False)
            # The test is the one capture allowed to pan. It must still leave the physical
            # lens at 0°; failure is reported instead of silently stranding it at an edge.
            await self.robot.center_camera()

        try:
            self._preview_path = await self._download(media_url)
            self._preview_id = uuid4().hex
        except Exception:
            await self.discard()
            raise

    async def _await_yaw(self, target: float, timeout_s: float) -> bool:
        """Use heartbeat feedback when available and the same time budget as fallback."""
        deadline = asyncio.get_running_loop().time() + timeout_s
        while asyncio.get_running_loop().time() < deadline:
            yaw = self.robot.heartbeat_yaw()
            if yaw is not None and abs(yaw - target) <= 2.0:
                return True
            await asyncio.sleep(0.2)
        return False

    async def _download(self, url: str) -> Path:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("测试视频必须由机器人返回可直接下载的 HTTP(S) 地址")

        suffix = Path(unquote(parsed.path)).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            suffix = ".mp4"
        target = self.root / f"preview-{uuid4().hex}{suffix}"
        total = 0
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_type = str(response.headers.get("content-type") or "").lower()
                    if content_type and not content_type.startswith("video/"):
                        raise ValueError("机器人返回的测试文件不是视频")
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self.MAX_DOWNLOAD_BYTES:
                                raise ValueError("机器人返回的测试视频异常过大")
                            handle.write(chunk)
            if total == 0:
                raise ValueError("机器人返回了空的测试视频")
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise

    async def discard(self) -> None:
        path = self._preview_path
        self._preview_path = None
        self._preview_id = ""
        if path:
            path.unlink(missing_ok=True)

    async def close(self) -> None:
        await self.discard()
        shutil.rmtree(self.root, ignore_errors=True)
