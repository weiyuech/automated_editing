import asyncio
from pathlib import Path

import subprocess
import httpx
from automated_video_editing_backend.services.render import RenderService
import pytest

from automated_video_editing_backend.services import media as media_module
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.media_download import (
    MediaDownloadNotReadyError,
    camera_media_error_message,
    cleanup_abandoned_download_parts,
    exception_detail,
    retry_camera_media_download,
)


def _tiny_avi_bytes(tmp_path: Path) -> bytes:
    path = tmp_path / "complete.avi"
    subprocess.run([RenderService().ffmpeg_binary(), '-v', 'error', '-y', '-f', 'lavfi',
                    '-i', 'testsrc2=s=64x48:r=10:d=1', '-c:v', 'mjpeg', str(path)], check=True)
    return path.read_bytes()



def test_startup_cleanup_removes_only_app_owned_partial_downloads(tmp_path):
    owned = tmp_path / f".robot-REC_1.mp4.{'a' * 32}.part"
    unrelated = tmp_path / ".operator-notes.part"
    malformed = tmp_path / ".robot-REC_2.mp4.not-a-uuid.part"
    complete = tmp_path / "robot-REC_1.mp4"
    for path in (owned, unrelated, malformed, complete):
        path.write_bytes(b"bytes")

    assert cleanup_abandoned_download_parts(tmp_path) == 1
    assert not owned.exists()
    assert unrelated.read_bytes() == b"bytes"
    assert malformed.read_bytes() == b"bytes"
    assert complete.read_bytes() == b"bytes"


@pytest.mark.asyncio
async def test_camera_download_retries_transient_status_and_network_errors():
    attempts = 0
    request = httpx.Request("GET", "http://camera.local/REC_1.mp4")

    async def operation():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            response = httpx.Response(404, request=request)
            raise httpx.HTTPStatusError("not ready", request=request, response=response)
        if attempts == 2:
            raise httpx.ReadError("connection reset", request=request)
        return "saved"

    result = await retry_camera_media_download(
        operation,
        initial_delay_seconds=0,
        retry_delays_seconds=(0, 0),
    )

    assert result == "saved"
    assert attempts == 3


@pytest.mark.asyncio
async def test_camera_download_does_not_retry_a_permanent_http_error():
    attempts = 0
    request = httpx.Request("GET", "http://camera.local/REC_1.mp4")
    response = httpx.Response(403, request=request)

    async def operation():
        nonlocal attempts
        attempts += 1
        raise httpx.HTTPStatusError("forbidden", request=request, response=response)

    with pytest.raises(httpx.HTTPStatusError):
        await retry_camera_media_download(
            operation,
            initial_delay_seconds=0,
            retry_delays_seconds=(0, 0, 0),
        )
    assert attempts == 1
    error = httpx.HTTPStatusError("forbidden", request=request, response=response)
    assert camera_media_error_message(error) == "摄像头文件服务拒绝下载（HTTP 403）"


def test_empty_timeout_diagnostic_and_operator_message_are_never_blank():
    exc = httpx.ReadTimeout("")
    assert exception_detail(exc) == "ReadTimeout"
    assert camera_media_error_message(exc) == "连接摄像头文件服务超时，自动重试后仍未成功"


def _use_mock_download_transport(monkeypatch, transport):
    real_async_client = httpx.AsyncClient

    def build_client(**kwargs):
        return real_async_client(transport=transport, **kwargs)

    monkeypatch.setattr(media_module.httpx, "AsyncClient", build_client)


def _use_temporary_download_root(monkeypatch, tmp_path):
    root = tmp_path / "downloads"

    def temporary_generated_path(area, *parts):
        assert area == "data"
        return tmp_path.joinpath(*parts)

    monkeypatch.setattr(media_module, "generated_path", temporary_generated_path)
    monkeypatch.setattr(media_module, "ensure_inside_root", lambda path: path.resolve())
    return root


@pytest.mark.asyncio
async def test_media_download_is_hidden_until_atomically_complete(monkeypatch, tmp_path):
    transfer_started = asyncio.Event()
    finish_transfer = asyncio.Event()

    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first-half"
            transfer_started.set()
            await finish_transfer.wait()
            yield b"second-half"

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            stream=PausedStream(),
        )
    )
    _use_mock_download_transport(monkeypatch, transport)
    root = _use_temporary_download_root(monkeypatch, tmp_path)
    media = MediaService(path=tmp_path / "media-library.json")

    downloading = asyncio.create_task(media.download_url("http://camera.local/REC_1.mp4"))
    await transfer_started.wait()

    in_progress = [path for path in root.iterdir() if path.is_file()]
    assert len(in_progress) == 1
    assert in_progress[0].name.startswith(".")
    assert in_progress[0].suffix == ".part"
    assert not list(root.glob("*.mp4"))

    finish_transfer.set()
    item = await downloading

    assert item.path.endswith(".mp4")
    assert media.is_known_path(item.path)
    assert Path(item.path).read_bytes() == b"first-halfsecond-half"
    assert not list(root.glob("*.part"))


@pytest.mark.asyncio
async def test_robot_download_retries_an_html_not_ready_page_before_accepting_video(
    monkeypatch,
    tmp_path,
):
    attempts = 0
    complete_video = _tiny_avi_bytes(tmp_path)

    def handler(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html>file is still publishing</html>",
            )
        return httpx.Response(
            200,
            headers={"content-type": "video/x-msvideo"},
            content=complete_video,
        )

    _use_mock_download_transport(monkeypatch, httpx.MockTransport(handler))
    root = _use_temporary_download_root(monkeypatch, tmp_path)
    media = MediaService(path=tmp_path / "media-library.json")

    item = await retry_camera_media_download(
        lambda: media.download_url(
            "http://camera.local/REC_3.avi",
            metadata={"kind_hint": "video"},
        ),
        initial_delay_seconds=0,
        retry_delays_seconds=(0,),
    )

    assert attempts == 2
    assert Path(item.path).read_bytes() == complete_video
    assert [path.name for path in root.iterdir() if path.is_file()] == [Path(item.path).name]


@pytest.mark.asyncio
async def test_video_download_rejects_http_complete_but_invalid_container(
    monkeypatch,
    tmp_path,
):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            content=b"not-a-finished-video-container",
        )
    )
    _use_mock_download_transport(monkeypatch, transport)
    root = _use_temporary_download_root(monkeypatch, tmp_path)
    media = MediaService(path=tmp_path / "media-library.json")

    with pytest.raises(MediaDownloadNotReadyError, match="容器尚未写入完整"):
        await media.download_url(
            "http://camera.local/REC_IN_PROGRESS.mp4",
            metadata={"kind_hint": "video"},
        )

    assert not [path for path in root.iterdir() if path.is_file()]


@pytest.mark.asyncio
async def test_cancelled_media_download_removes_its_partial_file(monkeypatch, tmp_path):
    transfer_started = asyncio.Event()
    never_finish = asyncio.Event()

    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"partial"
            transfer_started.set()
            await never_finish.wait()

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            stream=PausedStream(),
        )
    )
    _use_mock_download_transport(monkeypatch, transport)
    root = _use_temporary_download_root(monkeypatch, tmp_path)
    media = MediaService(path=tmp_path / "media-library.json")

    downloading = asyncio.create_task(media.download_url("http://camera.local/REC_2.mp4"))
    await transfer_started.wait()
    downloading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await downloading

    assert not [path for path in root.iterdir() if path.is_file()]


@pytest.mark.asyncio
async def test_failed_replacement_keeps_an_existing_complete_download(monkeypatch, tmp_path):
    transfer_started = asyncio.Event()
    never_finish = asyncio.Event()

    class PausedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"new-partial"
            transfer_started.set()
            await never_finish.wait()

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            headers={"content-type": "video/mp4"},
            stream=PausedStream(),
        )
    )
    _use_mock_download_transport(monkeypatch, transport)
    root = _use_temporary_download_root(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    existing = root / "REC_2.mp4"
    existing.write_bytes(b"previous-complete-video")
    media = MediaService(path=tmp_path / "media-library.json")

    downloading = asyncio.create_task(media.download_url("http://camera.local/REC_2.mp4"))
    await transfer_started.wait()
    downloading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await downloading

    assert existing.read_bytes() == b"previous-complete-video"
    assert not list(root.glob("*.part"))


@pytest.mark.asyncio
async def test_camera_download_has_an_overall_deadline_and_cancels_the_operation():
    cancelled = asyncio.Event()

    async def operation():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(TimeoutError, match="摄像头媒体传输超过"):
        await retry_camera_media_download(
            operation,
            initial_delay_seconds=0,
            retry_delays_seconds=(),
            overall_timeout_seconds=0.01,
        )
    assert cancelled.is_set()
