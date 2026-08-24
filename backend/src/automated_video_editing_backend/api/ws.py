from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from automated_video_editing_backend.core.diagnostics import log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import CameraAngle, CruiseRequest, GimbalMoveRequest, MoveCommand
from automated_video_editing_backend.core.security import require_ws_token
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.robot import RobotService

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
    "CAPTURE_NOTE",
    "CRUISE_START",
    "CRUISE_CANCEL",
}


async def websocket_endpoint(
    websocket: WebSocket,
    events: EventHub,
    robot: RobotService,
    capture: CaptureService,
    cruise: CruiseService,
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
                await _handle_command(websocket, msg_type, data, robot, capture, cruise)
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
) -> None:
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
        await websocket.send_json({"type": "ROBOT_PHOTO", "data": await robot.capture_photo()})
    elif msg_type == "CAPTURE_START":
        if cruise.is_running:
            raise ValueError("A cruise is running; it already controls recording")
        title = str(data.get("title") or "Untitled capture")
        await robot.start_recording()
        session = await capture.start(title)
        await websocket.send_json({"type": "CAPTURE_STARTED", "data": session.model_dump(mode="json")})
    elif msg_type == "CAPTURE_STOP":
        if cruise.is_running:
            raise ValueError("A cruise is running; cancel it instead of stopping the recording")
        try:
            state = await robot.stop_recording()
        except Exception:
            # The hardware may have stopped successfully before Windows failed to download the
            # file. The session is still over and must not remain stuck as an active recording.
            await capture.stop()
            raise
        session = await capture.stop()
        if session is not None and state.media_local_path:
            capture.attach_to_recording(session, state.media_local_path)
        payload = session.model_dump(mode="json") if session else {}
        payload.update(
            {
                "media_url": state.media_url,
                "media_local_path": state.media_local_path,
                "media_sync_error": state.media_sync_error,
            }
        )
        await websocket.send_json({"type": "CAPTURE_STOPPED", "data": payload})
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
