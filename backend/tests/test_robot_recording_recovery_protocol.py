"""Real-WebSocket regression for a delayed camera Stop URL."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest
import websockets

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.services import robot as robot_module
from automated_video_editing_backend.services.robot import HardwareRobotAdapter, RobotService


async def _wait_until(predicate, *, timeout: float = 0.5) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.002)


@pytest.mark.asyncio
async def test_blank_start_url_recovers_late_stop_url_and_downloads_once(
    monkeypatch,
    tmp_path,
):
    """An idle heartbeat may end Stop uncertainty without discarding its late URL."""

    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)

    commands: list[dict] = []
    downloads: list[str] = []
    server_errors: list[BaseException] = []
    stop_received = asyncio.Event()
    release_idle_heartbeat = asyncio.Event()
    idle_heartbeat_sent = asyncio.Event()
    release_late_stop_reply = asyncio.Event()
    late_stop_reply_sent = asyncio.Event()
    background_tasks: list[asyncio.Task[None]] = []
    media_url = "http://robot.local/media/REC_LATE.mp4"

    async def robot_endpoint(connection) -> None:
        send_lock = asyncio.Lock()

        async def send(payload: dict) -> None:
            async with send_lock:
                await connection.send(json.dumps(payload))

        async def finish_stop_after_client_timeout() -> None:
            await release_idle_heartbeat.wait()
            await send(
                {
                    "gimbal": {
                        "record_status": "idle",
                        "yaw": 0,
                        "pitch": 0,
                        "mode": 1,
                    }
                }
            )
            idle_heartbeat_sent.set()
            await release_late_stop_reply.wait()
            await send(
                {
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": media_url,
                    }
                }
            )
            late_stop_reply_sent.set()

        try:
            await send(
                {
                    "gimbal": {
                        "record_status": "idle",
                        "yaw": 0,
                        "pitch": 0,
                        "mode": 1,
                    }
                }
            )
            async for raw in connection:
                payload = json.loads(raw)
                commands.append(payload)
                if payload.get("video_record", {}).get("start") == 0:
                    await send(
                        {
                            "robot_video_record": {
                                "start": 0,
                                "status": "ok",
                                "url": "",
                            }
                        }
                    )
                elif payload.get("video_record", {}).get("stop") == 0:
                    stop_received.set()
                    task = asyncio.create_task(finish_stop_after_client_timeout())
                    background_tasks.append(task)
        except asyncio.CancelledError:
            raise
        # Preserve failures raised inside the independently scheduled server handler so the
        # client-side assertions cannot pass after a silent protocol-emulator crash.
        except Exception as exc:  # noqa: BLE001
            server_errors.append(exc)

    class FileMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            downloads.append(url)
            target = tmp_path / "REC_LATE.mp4"
            target.write_bytes(b"late-stop-video")
            return MediaItem(path=str(target), kind="video", metadata=metadata or {})

    async with websockets.serve(robot_endpoint, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        events = EventHub()
        adapter = HardwareRobotAdapter(events, f"ws://127.0.0.1:{port}")
        robot = RobotService(events, adapter=adapter, media=FileMedia())
        robot.resolve_media_url = lambda url: url

        try:
            started = await asyncio.wait_for(robot.start_recording(), timeout=1.0)
            assert started.recording is True
            assert started.media_url == ""

            first_finalize = asyncio.create_task(robot.finalize_capture_recording())
            await asyncio.wait_for(stop_received.wait(), timeout=0.5)
            with pytest.raises(TimeoutError, match="等待机器人回复超时"):
                await asyncio.wait_for(first_finalize, timeout=0.5)

            assert downloads == []
            assert adapter.recording_operation_pending() is True

            release_idle_heartbeat.set()
            await asyncio.wait_for(idle_heartbeat_sent.wait(), timeout=0.5)
            await _wait_until(
                lambda: (
                    not adapter.state.recording
                    and adapter.recording_status_known()
                    and not adapter.recording_operation_pending()
                )
            )

            release_late_stop_reply.set()
            await asyncio.wait_for(late_stop_reply_sent.wait(), timeout=0.5)
            await _wait_until(lambda: adapter.state.media_url == media_url)

            recovered = await asyncio.wait_for(
                robot.finalize_capture_recording(),
                timeout=1.5,
            )
        finally:
            release_idle_heartbeat.set()
            release_late_stop_reply.set()
            if background_tasks:
                await asyncio.gather(*background_tasks, return_exceptions=True)
            with suppress(Exception):
                await adapter.disconnect()

    assert server_errors == []
    assert recovered.recording is False
    assert recovered.media_url == media_url
    assert recovered.media_local_path == str(tmp_path / "REC_LATE.mp4")
    assert downloads == [media_url]
    assert sum(payload.get("video_record", {}).get("start") == 0 for payload in commands) == 1
    assert sum(payload.get("video_record", {}).get("stop") == 0 for payload in commands) == 1


@pytest.mark.asyncio
async def test_start_without_ack_still_requires_one_explicit_stop(monkeypatch, tmp_path):
    """Cross the real socket boundary, lose Start's ACK, then finalize safely."""

    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    commands: list[dict] = []
    downloads: list[str] = []
    server_errors: list[BaseException] = []
    media_url = "http://robot.local/media/REC_NO_ACK.mp4"

    async def robot_endpoint(connection) -> None:
        try:
            await connection.send(json.dumps({
                "gimbal": {
                    "record_status": "idle",
                    "yaw": 0,
                    "pitch": 0,
                    "mode": 1,
                }
            }))
            async for raw in connection:
                payload = json.loads(raw)
                commands.append(payload)
                if payload.get("video_record", {}).get("start") == 0:
                    # The firmware accepted the frame but its reply was lost.
                    continue
                if payload.get("video_record", {}).get("stop") == 0:
                    await connection.send(json.dumps({
                        "robot_video_record": {
                            "stop": 0,
                            "status": "ok",
                            "url": media_url,
                        }
                    }))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            server_errors.append(exc)

    class FileMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            downloads.append(url)
            target = tmp_path / "REC_NO_ACK.mp4"
            target.write_bytes(b"no-ack-video")
            return MediaItem(path=str(target), kind="video", metadata=metadata or {})

    async with websockets.serve(robot_endpoint, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        events = EventHub()
        adapter = HardwareRobotAdapter(events, f"ws://127.0.0.1:{port}")
        robot = RobotService(events, adapter=adapter, media=FileMedia())
        robot.resolve_media_url = lambda url: url
        try:
            with pytest.raises(TimeoutError, match="等待机器人回复超时"):
                await asyncio.wait_for(robot.start_recording(), timeout=0.5)

            assert adapter.recording_stop_required() is True
            recovered = await asyncio.wait_for(
                robot.finalize_capture_recording(),
                timeout=1.0,
            )
        finally:
            with suppress(Exception):
                await adapter.disconnect()

    assert server_errors == []
    assert recovered.recording is False
    assert recovered.media_local_path == str(tmp_path / "REC_NO_ACK.mp4")
    assert downloads == [media_url]
    assert sum(payload.get("video_record", {}).get("start") == 0 for payload in commands) == 1
    assert sum(payload.get("video_record", {}).get("stop") == 0 for payload in commands) == 1


@pytest.mark.asyncio
async def test_gimbal_send_timeout_reconnects_before_queued_final_stop(monkeypatch, tmp_path):
    """A failed gimbal transport cannot poison the Stop queued on the shared socket."""

    monkeypatch.setattr(robot_module, "_WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.03)
    monkeypatch.setattr(robot_module, "_WEBSOCKET_CLOSE_AFTER_SEND_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.1)
    commands: list[dict] = []
    downloads: list[str] = []
    server_errors: list[BaseException] = []
    connection_count = 0
    recording = False
    media_url = "http://robot.local/media/REC_AFTER_RECONNECT.mp4"

    async def robot_endpoint(connection) -> None:
        nonlocal connection_count, recording
        connection_count += 1
        try:
            await connection.send(json.dumps({
                "gimbal": {
                    "record_status": "recording" if recording else "idle",
                    "yaw": 0,
                    "pitch": 0,
                    "mode": 1,
                }
            }))
            async for raw in connection:
                payload = json.loads(raw)
                commands.append(payload)
                if payload.get("video_record", {}).get("start") == 0:
                    recording = True
                    await connection.send(json.dumps({
                        "robot_video_record": {
                            "start": 0,
                            "status": "ok",
                            "url": "",
                        }
                    }))
                elif payload.get("video_record", {}).get("stop") == 0:
                    recording = False
                    await connection.send(json.dumps({
                        "robot_video_record": {
                            "stop": 0,
                            "status": "ok",
                            "url": media_url,
                        }
                    }))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            server_errors.append(exc)

    class FileMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            downloads.append(url)
            target = tmp_path / "REC_AFTER_RECONNECT.mp4"
            target.write_bytes(b"reconnected-video")
            return MediaItem(path=str(target), kind="video", metadata=metadata or {})

    async with websockets.serve(robot_endpoint, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        events = EventHub()
        adapter = HardwareRobotAdapter(events, f"ws://127.0.0.1:{port}")
        # Keep the deterministic reconnect within the regression's short wall-clock budget.
        adapter._reconnect_delay_s = 0.01
        adapter._max_reconnect_delay_s = 0.05
        robot = RobotService(events, adapter=adapter, media=FileMedia())
        robot.resolve_media_url = lambda url: url
        gimbal_write_started = asyncio.Event()
        never_finish_gimbal_write = asyncio.Event()

        class StallOneGimbalWrite:
            def __init__(self, inner):
                self.inner = inner
                self.stalled = False

            async def send(self, message):
                payload = json.loads(message)
                if "gimbal_control" in payload and not self.stalled:
                    self.stalled = True
                    gimbal_write_started.set()
                    await never_finish_gimbal_write.wait()
                await self.inner.send(message)

            async def close(self):
                await self.inner.close()

        try:
            started = await asyncio.wait_for(robot.start_recording(), timeout=1.0)
            assert started.recording is True
            assert adapter._socket is not None
            adapter._socket = StallOneGimbalWrite(adapter._socket)

            gimbal = asyncio.create_task(adapter.set_gimbal(
                robot_module.GimbalMoveRequest(
                    yaw_start=0,
                    yaw_end=30,
                    yaw_speed=3,
                    pitch_start=0,
                    pitch_end=0,
                    pitch_speed=3,
                    zoom_start=1,
                    zoom_end=1,
                )
            ))
            await asyncio.wait_for(gimbal_write_started.wait(), timeout=0.5)
            finalizing = asyncio.create_task(robot.finalize_capture_recording())

            with pytest.raises(ConnectionError, match="发送指令超过"):
                await asyncio.wait_for(gimbal, timeout=0.5)
            recovered = await asyncio.wait_for(finalizing, timeout=2.0)
        finally:
            never_finish_gimbal_write.set()
            with suppress(Exception):
                await adapter.disconnect()

    assert server_errors == []
    assert connection_count >= 2
    assert recovered.recording is False
    assert recovered.media_local_path == str(tmp_path / "REC_AFTER_RECONNECT.mp4")
    assert downloads == [media_url]
    assert sum(payload.get("video_record", {}).get("start") == 0 for payload in commands) == 1
    assert sum(payload.get("video_record", {}).get("stop") == 0 for payload in commands) == 1
