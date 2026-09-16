from __future__ import annotations

import asyncio
import shutil
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlparse
from uuid import uuid4

import httpx

from automated_video_editing_backend.core.diagnostics import log_event
from automated_video_editing_backend.services.media_download import (
    MediaDownloadNotReadyError,
    camera_media_error_message,
    exception_detail,
    is_retryable_media_download_error,
    retry_camera_media_download,
    validate_downloaded_video,
)
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
    MAX_DOWNLOAD_SECONDS = 30.0

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

        original_yaw = self.robot.heartbeat_yaw()
        if original_yaw is None:
            original_yaw = state.yaw

        recording_started = False
        returned_to_start = False
        media_url = ""
        preview_path: Path | None = None

        async def download_after_return(url: str) -> Path:
            nonlocal returned_to_start
            if original_yaw is not None:
                # Do not begin a potentially long transfer while the camera is still facing
                # the test endpoint. If this restore fails, the outer finally makes one last
                # best-effort restore and the disposable preview is not accepted.
                await self.robot.sweep_camera(
                    float(original_yaw), self.PREPOSITION_SPEED_DEG_S,
                    context="framing_test",
                )
                returned_to_start = True
            return await self._download(url)

        try:
            await self.robot.sweep_camera(
                self.LEFT_YAW, self.PREPOSITION_SPEED_DEG_S,
                context="framing_test",
            )
            await self._await_yaw(self.LEFT_YAW, timeout_s=3.5)

            recording = await self.robot.start_recording()
            if not recording.recording:
                raise RuntimeError(recording.error or "机器人未确认开始录制")
            recording_started = True

            await self.robot.sweep_camera(
                self.RIGHT_YAW, self.SWEEP_SPEED_DEG_S,
                context="framing_test",
            )
            await asyncio.sleep(self.RECORD_SECONDS)

            stopped, preview_path = await self.robot.stop_recording_with_download(
                download_after_return
            )
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
                    current = await self.robot.status()
                    if current.recording:
                        await self.robot.stop_recording(sync_media=False)
            # This test deliberately pans, but the protocol does not define which yaw means
            # physical front. Return to the operator's actual starting angle instead of 0°.
            if original_yaw is not None and not returned_to_start:
                with suppress(Exception):
                    await self.robot.sweep_camera(
                        float(original_yaw), self.PREPOSITION_SPEED_DEG_S,
                        context="framing_test",
                    )

        if preview_path is None:
            raise RuntimeError("取景测试视频没有成功保存")
        self._preview_path = preview_path
        self._preview_id = uuid4().hex

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
        def log_retry(
            attempt: int,
            total_attempts: int,
            delay: float,
            exc: BaseException,
        ) -> None:
            log_event(
                "warning",
                "framing_test.media.download.retry",
                robot_url=url,
                failed_attempt=attempt,
                total_attempts=total_attempts,
                retry_in_seconds=delay,
                exception=exception_detail(exc),
            )

        try:
            return await retry_camera_media_download(
                lambda: self._download_once(url),
                overall_timeout_seconds=self.MAX_DOWNLOAD_SECONDS,
                on_retry=log_retry,
            )
        except Exception as exc:
            error = camera_media_error_message(exc)
            log_event(
                "error",
                "framing_test.media.download.failed",
                robot_url=url,
                error=error,
                exception=exception_detail(exc),
            )
            if is_retryable_media_download_error(exc) or isinstance(exc, httpx.HTTPError):
                raise RuntimeError(error) from exc
            raise

    async def _download_once(self, url: str) -> Path:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("测试视频必须由机器人返回可直接下载的 HTTP(S) 地址")

        suffix = Path(unquote(parsed.path)).suffix.lower()
        if suffix not in {".mp4", ".mov", ".m4v", ".webm", ".mkv"}:
            suffix = ".mp4"
        target = self.root / f"preview-{uuid4().hex}{suffix}"
        partial = target.with_name(f".{target.name}.{uuid4().hex}.part")
        total = 0
        committed = False
        try:
            async with (
                httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=httpx.Timeout(60.0, connect=3.0, pool=3.0),
                ) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                content_type = str(response.headers.get("content-type") or "").lower()
                if content_type and not content_type.startswith("video/"):
                    raise MediaDownloadNotReadyError("摄像头返回的测试文件暂时不是视频")
                content_length = response.headers.get("content-length")
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > self.MAX_DOWNLOAD_BYTES:
                            raise ValueError("机器人返回的测试视频异常过大")
                        handle.write(chunk)
            if total == 0:
                raise MediaDownloadNotReadyError("摄像头返回的测试视频暂时为空")
            if (
                content_length
                and content_length.isdigit()
                and not response.headers.get("content-encoding")
                and total != int(content_length)
            ):
                raise MediaDownloadNotReadyError("摄像头返回的测试视频尚未传输完整")
            validate_downloaded_video(partial)
            partial.replace(target)
            committed = True
            return target
        finally:
            partial.unlink(missing_ok=True)
            if not committed:
                target.unlink(missing_ok=True)

    async def discard(self) -> None:
        path = self._preview_path
        self._preview_path = None
        self._preview_id = ""
        if path:
            path.unlink(missing_ok=True)

    async def close(self) -> None:
        await self.discard()
        shutil.rmtree(self.root, ignore_errors=True)
