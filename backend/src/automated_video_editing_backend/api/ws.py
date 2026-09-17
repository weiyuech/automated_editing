from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from automated_video_editing_backend.core.diagnostics import log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraAngle,
    CruiseRequest,
    GimbalMoveRequest,
    MoveCommand,
)
from automated_video_editing_backend.core.security import require_ws_token
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.framing_test import FramingTestService
from automated_video_editing_backend.services.robot import RobotCommandNotSentError, RobotService

ALLOWED_COMMANDS = {
    "PING",
    "ROBOT_STOP",
    "ROBOT_MOVE",
    "ROBOT_CAMERA_ANGLE",
    "ROBOT_GIMBAL",
    "ROBOT_START_RECORDING",
    "ROBOT_STOP_RECORDING",
    "ROBOT_CAPTURE_PHOTO",
    "CAPTURE_START",
    "CAPTURE_STOP",
    "CAPTURE_DISCARD",
    "CAPTURE_NOTE",
    "CRUISE_START",
    "CRUISE_CANCEL",
}


def _recording_idle_confirmed(robot: Any, state: Any) -> bool:
    reader = getattr(robot, "recording_idle_confirmed", None)
    if callable(reader):
        return bool(reader())
    known_reader = getattr(robot, "recording_status_known", None)
    known = bool(known_reader()) if callable(known_reader) else True
    return bool(known and not state.recording)


async def websocket_endpoint(
    websocket: WebSocket,
    events: EventHub,
    robot: RobotService,
    capture: CaptureService,
    cruise: CruiseService,
    framing_test: FramingTestService,
) -> None:
    subprotocol = await require_ws_token(websocket)
    await websocket.accept(subprotocol=subprotocol)
    sender = asyncio.create_task(_send_events(websocket, events))
    try:
        await websocket.send_json({"type": "CONNECTED", "data": {"ok": True}})
        while True:
            try:
                message = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            msg_type = message.get("type")
            data = message.get("data") or {}
            if msg_type not in ALLOWED_COMMANDS:
                await websocket.send_json({"type": "ERROR", "data": {"message": "Unknown command"}})
                continue
            try:
                await _handle_command(
                    websocket, msg_type, data, robot, capture, cruise, framing_test
                )
            except Exception as exc:
                log_event("error", "ui.command.failed", command=msg_type, error=str(exc))
                await websocket.send_json(
                    {"type": "ERROR", "data": {"message": str(exc), "command": msg_type}}
                )
    finally:
        sender.cancel()
        with suppress(asyncio.CancelledError):
            await sender


async def _send_events(websocket: WebSocket, events: EventHub) -> None:
    async for event in events.subscribe():
        await websocket.send_json(event)


async def _handle_command(
    websocket: WebSocket,
    msg_type: str,
    data: dict[str, Any],
    robot: RobotService,
    capture: CaptureService,
    cruise: CruiseService,
    framing_test: FramingTestService,
) -> None:
    camera_commands = {
        "ROBOT_CAMERA_ANGLE",
        "ROBOT_GIMBAL",
        "ROBOT_START_RECORDING",
        "ROBOT_STOP_RECORDING",
        "ROBOT_CAPTURE_PHOTO",
        "CAPTURE_START",
        "CAPTURE_STOP",
        "CAPTURE_DISCARD",
    }
    if msg_type in camera_commands and framing_test.status().get("running"):
        raise ValueError("取景测试正在进行，请等待测试完成")
    if msg_type in camera_commands and cruise.is_running:
        raise ValueError("巡游正在进行，镜头和录制由巡游控制")
    if msg_type == "ROBOT_MOVE" and cruise.is_running:
        raise ValueError("巡游正在进行，地图与导航由巡游控制")
    if (
        msg_type in {"ROBOT_START_RECORDING", "ROBOT_STOP_RECORDING"}
        and capture.active_session() is not None
    ):
        raise ValueError("原地采集正在进行，请使用采集停止操作")
    if msg_type == "CRUISE_START" and framing_test.status().get("running"):
        raise ValueError("取景测试正在进行，请等待测试完成")

    if msg_type == "PING":
        await websocket.send_json({"type": "PONG", "data": {}})
    elif msg_type == "ROBOT_STOP":
        await websocket.send_json({"type": "ROBOT_STATE", "data": (await robot.stop_motion()).model_dump(mode="json")})
    elif msg_type == "ROBOT_MOVE":
        state = await robot.move(MoveCommand(**data))
        await websocket.send_json({"type": "ROBOT_STATE", "data": state.model_dump(mode="json")})
    elif msg_type == "ROBOT_CAMERA_ANGLE":
        state = await robot.set_camera_angle(CameraAngle(**data))
        await websocket.send_json({"type": "ROBOT_STATE", "data": state.model_dump(mode="json")})
    elif msg_type == "ROBOT_GIMBAL":
        state = await robot.set_gimbal(GimbalMoveRequest(**data))
        await websocket.send_json({"type": "ROBOT_STATE", "data": state.model_dump(mode="json")})
    elif msg_type == "ROBOT_START_RECORDING":
        state = await robot.start_recording()
        await websocket.send_json({"type": "ROBOT_STATE", "data": state.model_dump(mode="json")})
    elif msg_type == "ROBOT_STOP_RECORDING":
        state = await robot.stop_recording()
        await websocket.send_json({"type": "ROBOT_STATE", "data": state.model_dump(mode="json")})
    elif msg_type == "ROBOT_CAPTURE_PHOTO":
        if capture.active_session() is not None and not (await robot.status()).recording:
            raise ValueError("当前视频尚未保存，请先重试保存")
        await websocket.send_json({"type": "ROBOT_PHOTO", "data": await robot.capture_photo()})
    elif msg_type == "CAPTURE_START":
        if capture.active_session() is not None:
            raise ValueError("已有采集正在进行，请先停止当前采集")
        title = str(data.get("title") or "Untitled capture")
        session = await capture.start(title)
        try:
            state = await robot.start_recording()
        except (RobotCommandNotSentError, ValueError):
            with suppress(OSError):
                await capture.stop()
            raise
        capture.remember_pending_media(session, state.media_url)
        await websocket.send_json({"type": "CAPTURE_STARTED", "data": session.model_dump(mode="json")})
    elif msg_type == "CAPTURE_STOP":
        session = capture.active_session()

        def remember_final_url(media_url: str) -> None:
            if session is not None:
                capture.remember_pending_media(session, media_url)

        try:
            state = await robot.finalize_capture_recording(
                on_media_url=remember_final_url,
            )
        except asyncio.CancelledError:
            with suppress(Exception):
                current = await robot.status()
                pending_session = capture.active_session()
                if pending_session is not None:
                    capture.remember_pending_media(
                        pending_session,
                        current.media_url,
                        current.media_sync_error,
                        current.media_local_path,
                    )
                if _recording_idle_confirmed(robot, current) and current.media_local_path:
                    await capture.complete_with_recording(current.media_local_path)
            raise
        except Exception:
            # Preserve notes/markers when stop was never confirmed and the robot may still be
            # recording. An idle recording without a local file also remains recoverable: a
            # late heartbeat may still provide its URL for the operator's next retry.
            with suppress(Exception):
                current = await robot.status()
                pending_session = capture.active_session()
                if pending_session is not None:
                    capture.remember_pending_media(
                        pending_session,
                        current.media_url,
                        current.media_sync_error,
                        current.media_local_path,
                    )
                if _recording_idle_confirmed(robot, current) and current.media_local_path:
                    await capture.complete_with_recording(current.media_local_path)
            raise
        session = capture.active_session()
        if session is not None:
            capture.remember_pending_media(
                session,
                state.media_url,
                state.media_sync_error,
                state.media_local_path,
            )
        if state.media_local_path:
            if not _recording_idle_confirmed(robot, state):
                raise RuntimeError("尚未确认机器人已停止录制，拍摄会话已保留")
            session = await capture.complete_with_recording(state.media_local_path)
        payload = session.model_dump(mode="json") if session else {}
        payload.update(
            {
                "media_url": state.media_url,
                "media_local_path": state.media_local_path,
                "media_sync_error": state.media_sync_error,
            }
        )
        await websocket.send_json({"type": "CAPTURE_STOPPED", "data": payload})
    elif msg_type == "CAPTURE_DISCARD":
        session = await robot.discard_capture_recovery(capture.discard)
        await websocket.send_json({
            "type": "CAPTURE_DISCARDED",
            "data": session.model_dump(mode="json") if session else {},
        })
    elif msg_type == "CAPTURE_NOTE":
        note = str(data.get("note") or "")[:500]
        session = await capture.add_note(note)
        await websocket.send_json({"type": "CAPTURE_NOTE", "data": session.model_dump(mode="json") if session else {}})
    elif msg_type == "CRUISE_START":
        run = await cruise.start(CruiseRequest(**data))
        await websocket.send_json({"type": "CRUISE_RUN", "data": run.model_dump(mode="json")})
    elif msg_type == "CRUISE_CANCEL":
        run = await cruise.cancel()
        await websocket.send_json({"type": "CRUISE_RUN", "data": run.model_dump(mode="json") if run else {}})
