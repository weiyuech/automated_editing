from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
import re
import math
import json
from fractions import Fraction
import subprocess
from typing import TypeVar

import httpx

T = TypeVar("T")

# A camera can acknowledge "stop recording" before its HTTP file server has finished
# publishing the new file. Keep this retry budget short and bounded: it covers that hand-off
# without turning an invalid URL or a disconnected camera into an unbounded backend request.
CAMERA_MEDIA_INITIAL_DELAY_SECONDS = 0.5
CAMERA_MEDIA_RETRY_DELAYS_SECONDS = (0.5, 1.0, 2.0, 4.0)
CAMERA_MEDIA_OVERALL_TIMEOUT_SECONDS = 15 * 60.0
RETRYABLE_HTTP_STATUSES = {404, 408, 409, 423, 425, 429, 500, 502, 503, 504}
DOWNLOAD_PART_NAME = re.compile(r"^\..+\.[0-9a-f]{32}\.part$")


class MediaDownloadNotReadyError(ValueError):
    """The server answered, but the requested media is not complete enough to consume yet."""


def cleanup_abandoned_download_parts(folder: str | Path) -> int:
    """Remove only this app's UUID-named transfer remnants during backend startup.

    Electron permits one app instance, and this runs before MediaService can start a transfer.
    The exact hidden-name pattern leaves complete media and unrelated user `.part` files alone.
    """
    root = Path(folder)
    try:
        entries = list(root.iterdir())
    except OSError:
        return 0

    removed = 0
    for path in entries:
        if not DOWNLOAD_PART_NAME.fullmatch(path.name):
            continue
        try:
            if path.is_file() or path.is_symlink():
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


def validate_downloaded_video(path: str | Path) -> None:
    """Reject a camera's HTTP snapshot until it is a decodable, seekable video container.

    Content-Length only describes the bytes returned by this request. Some camera servers expose
    the file while it is still being written, so a truncated snapshot can legitimately match its
    own HTTP length. Opening the container, decoding real frames, and probing near the declared
    end prevents that snapshot from closing the capture recovery session as if it were final.
    """
    from automated_video_editing_backend.services.render import RenderService

    renderer = RenderService()
    duration = renderer.probe_duration(str(path))
    if duration is None or not math.isfinite(duration) or duration <= 0:
        raise MediaDownloadNotReadyError("摄像头视频容器尚未写入完整")
    probe = subprocess.run(
        [
            renderer.ffprobe_binary(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    try:
        fps = float(Fraction(json.loads(probe.stdout)["streams"][0]["avg_frame_rate"]))
        margin = 2 / fps if fps > 0 else 1.0
    except (ValueError, KeyError, IndexError, ZeroDivisionError):
        margin = 1.0
    tail = max(0, min(duration * 0.95, duration - margin))
    for position, frames in ((0, 2), (tail, 1)):
        try:
            result = subprocess.run(
                [
                    renderer.ffmpeg_binary(),
                    "-v",
                    "error",
                    "-xerror",
                    "-err_detect",
                    "explode",
                    "-ss",
                    str(position),
                    "-i",
                    str(path),
                    "-map",
                    "0:v:0",
                    "-frames:v",
                    str(frames),
                    "-vf",
                    "scale=16:16",
                    "-pix_fmt",
                    "gray",
                    "-f",
                    "rawvideo",
                    "pipe:1",
                ],
                capture_output=True,
                timeout=30,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MediaDownloadNotReadyError("摄像头视频画面校验超时") from exc
        if result.returncode or len(result.stdout) < frames * 256:
            raise MediaDownloadNotReadyError("摄像头视频画面或尾部尚未写入完整")


def exception_detail(exc: BaseException) -> str:
    """Never emit an empty diagnostic for exceptions such as asyncio/httpx timeouts."""
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def is_retryable_media_download_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            ConnectionError,
            TimeoutError,
            MediaDownloadNotReadyError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in RETRYABLE_HTTP_STATUSES
    return False


def camera_media_error_message(exc: BaseException) -> str:
    """Give operators a useful camera-side diagnosis while logs retain the exact exception."""
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in RETRYABLE_HTTP_STATUSES:
            return f"摄像头文件暂不可用（HTTP {status}），自动重试后仍未成功"
        return f"摄像头文件服务拒绝下载（HTTP {status}）"
    if isinstance(exc, MediaDownloadNotReadyError):
        return f"{exc}，自动重试后仍未成功"
    if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
        return "连接摄像头文件服务超时，自动重试后仍未成功"
    if isinstance(
        exc,
        (httpx.NetworkError, httpx.RemoteProtocolError, ConnectionError),
    ):
        return "摄像头文件传输连接中断，自动重试后仍未成功"
    return str(exc).strip() or exception_detail(exc)


async def retry_camera_media_download(
    operation: Callable[[], Awaitable[T]],
    *,
    initial_delay_seconds: float = CAMERA_MEDIA_INITIAL_DELAY_SECONDS,
    retry_delays_seconds: tuple[float, ...] = CAMERA_MEDIA_RETRY_DELAYS_SECONDS,
    overall_timeout_seconds: float = CAMERA_MEDIA_OVERALL_TIMEOUT_SECONDS,
    on_retry: Callable[[int, int, float, BaseException], None] | None = None,
) -> T:
    """Retry only transient camera-transfer failures, preserving cancellation and hard errors."""

    async def run_attempts() -> T:
        if initial_delay_seconds > 0:
            await asyncio.sleep(initial_delay_seconds)

        total_attempts = len(retry_delays_seconds) + 1
        for attempt in range(1, total_attempts + 1):
            try:
                return await operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= total_attempts or not is_retryable_media_download_error(exc):
                    raise
                delay = retry_delays_seconds[attempt - 1]
                if on_retry is not None:
                    on_retry(attempt, total_attempts, delay, exc)
                await asyncio.sleep(delay)

        raise AssertionError("unreachable")

    timeout = asyncio.timeout(overall_timeout_seconds)
    try:
        async with timeout:
            return await run_attempts()
    except TimeoutError as exc:
        if timeout.expired():
            raise TimeoutError(f"摄像头媒体传输超过 {overall_timeout_seconds:g} 秒") from exc
        raise
