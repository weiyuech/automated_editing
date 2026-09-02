from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from automated_video_editing_backend.core.diagnostics import log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraAngle,
    GimbalMoveRequest,
    MoveCommand,
    RobotGoalCommand,
    RobotMode,
    RobotState,
    utc_now,
)

if TYPE_CHECKING:
    from automated_video_editing_backend.core.models import MediaItem
    from automated_video_editing_backend.services.media import MediaService


class RobotAdapter(ABC):
    @abstractmethod
    async def configure_websocket_url(self, websocket_url: str) -> RobotState: ...

    @abstractmethod
    async def connect(self) -> RobotState: ...

    @abstractmethod
    async def disconnect(self) -> RobotState: ...

    @abstractmethod
    async def status(self) -> RobotState: ...

    @abstractmethod
    async def map_list(self) -> list[str]: ...

    @abstractmethod
    async def switch_map(self, map_name: str) -> dict[str, Any]: ...

    @abstractmethod
    async def path_list(self, map_name: str) -> list[str]: ...

    @abstractmethod
    async def set_goal(self, command: RobotGoalCommand) -> dict[str, Any]: ...

    @abstractmethod
    async def wait_for_arrival(self, timeout_s: float = 60.0) -> str: ...

    @abstractmethod
    async def sweep_camera(self, target_yaw: float, yaw_speed: float) -> RobotState: ...

    @abstractmethod
    def heartbeat_yaw(self) -> float | None: ...

    @abstractmethod
    def heartbeat_pitch(self) -> float | None: ...

    @abstractmethod
    def heartbeat_revision(self) -> int | None: ...

    @abstractmethod
    async def stop_motion(self) -> RobotState: ...

    @abstractmethod
    async def move(self, command: MoveCommand) -> RobotState: ...

    @abstractmethod
    async def set_camera_angle(self, angle: CameraAngle) -> RobotState: ...

    @abstractmethod
    async def set_gimbal(self, command: GimbalMoveRequest) -> RobotState: ...

    @abstractmethod
    async def start_recording(self) -> RobotState: ...

    @abstractmethod
    async def stop_recording(self) -> RobotState: ...

    @abstractmethod
    async def capture_photo(self) -> dict[str, Any]: ...


class HardwareRobotAdapter(RobotAdapter):
    def __init__(self, events: EventHub, websocket_url: str = "") -> None:
        self.events = events
        self.websocket_url = websocket_url.strip()
        self.state = RobotState(
            adapter=RobotMode.REAL,
            connection_status="disconnected",
            error=None if self.websocket_url else "Robot websocket URL is not configured",
        )
        self._socket: Any | None = None
        self._connection_task: asyncio.Task[None] | None = None
        self._request_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reconnect_delay_s = 1.0
        self._max_reconnect_delay_s = 10.0
        self._pending_goal: RobotGoalCommand | None = None
        self._require_non_done = False
        self._arrival_event = asyncio.Event()
        self._arrival_result: str | None = None
        self._heartbeat_yaw: float | None = None
        self._heartbeat_pitch: float | None = None
        self._heartbeat_revision: int | None = None

    async def configure_websocket_url(self, websocket_url: str) -> RobotState:
        next_url = websocket_url.strip()
        if next_url == self.websocket_url:
            if next_url:
                self._start_connection_loop()
            return self.state

        await self._stop_connection_loop()
        self.websocket_url = next_url
        self.state.connected = False
        self.state.connection_status = "disconnected"
        self.state.error = None if next_url else "Robot websocket URL is not configured"
        self._touch()
        await self._publish_state()
        if next_url:
            self._start_connection_loop()
            await self.connect()
        return self.state

    async def connect(self) -> RobotState:
        if not self.websocket_url:
            self.state.connected = False
            self.state.connection_status = "disconnected"
            self.state.error = "Robot websocket URL is not configured"
            self._touch()
            await self._publish_state()
            return self.state

        self._start_connection_loop()
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            if self.state.connected:
                return self.state
            await asyncio.sleep(0.05)
        return self.state

    async def disconnect(self) -> RobotState:
        await self._stop_connection_loop()
        self.state.connected = False
        self.state.connection_status = "disconnected"
        self.state.error = None
        self._touch()
        await self._publish_state()
        return self.state

    async def status(self) -> RobotState:
        if self.websocket_url:
            self._start_connection_loop()
        return self.state

    async def map_list(self) -> list[str]:
        response = await self._request({"get_map_list": "all"}, "robot_map_list")
        return response if isinstance(response, list) else []

    async def switch_map(self, map_name: str) -> dict[str, Any]:
        response = await self._request({"set_switch_map": map_name}, "robot_switch_map")
        self.state.map_name = map_name
        self.state.last_command = "set_switch_map"
        self._touch()
        await self._publish_state()
        return {"map_name": map_name, "ok": _truthy(response), "raw": response}

    async def path_list(self, map_name: str) -> list[str]:
        response = await self._request({"get_path_list": map_name}, "robot_path_list")
        return response if isinstance(response, list) else []

    async def set_goal(self, command: RobotGoalCommand) -> dict[str, Any]:
        refuse_if_unfit_to_drive(self.state)
        payload = {
            "set_goal": {
                "path_name": command.path_name,
                "goal_id": command.goal_id,
                "goal_object": command.goal_object,
            }
        }
        # Arm before sending: a heartbeat carrying the *previous* goal's settled status can
        # land before this goal is acknowledged, and _require_non_done rejects that stale one.
        self._arm_goal_tracking(command)
        try:
            response = await self._request(payload, "robot_goal")
        except Exception:
            self._disarm_goal_tracking()
            raise
        if isinstance(response, dict):
            self.state.path_file = str(response.get("path_file") or command.path_name)
            self.state.goal_id = _maybe_int(response.get("goal_id")) or command.goal_id
            self.state.goal_object = _maybe_str(response.get("goal_object") or command.goal_object)
            accepted = _truthy(response.get("goal_check"))
            self.state.goal_status = "going" if accepted else "failed"
            if not accepted:
                self._disarm_goal_tracking()
        self.state.last_command = "set_goal"
        self._touch()
        await self._publish_state()
        return response if isinstance(response, dict) else {"raw": response}

    async def wait_for_arrival(self, timeout_s: float = 60.0) -> str:
        """Block until the robot settles on the goal armed by set_goal().

        Returns 'done' or 'failed'. Raises TimeoutError if the robot never settles, and
        ValueError if no goal is pending.
        """
        if self._pending_goal is None and self._arrival_result is None:
            raise ValueError("No goal is pending")
        await asyncio.wait_for(self._arrival_event.wait(), timeout=timeout_s)
        return self._arrival_result or "failed"

    def heartbeat_yaw(self) -> float | None:
        """Gimbal yaw as last reported by the robot.

        Distinct from state.yaw, which set_camera_angle updates optimistically. Only this
        value is safe to poll while a sweep is in flight.
        """
        return self._heartbeat_yaw

    def heartbeat_pitch(self) -> float | None:
        """Gimbal pitch as last reported by the robot, never an optimistic command target."""
        return self._heartbeat_pitch

    def heartbeat_revision(self) -> int | None:
        """Monotonic gimbal-heartbeat revision, or None until the first physical sample."""
        return self._heartbeat_revision

    async def sweep_camera(self, target_yaw: float, yaw_speed: float) -> RobotState:
        """Drive the gimbal toward target_yaw at an explicit speed.

        Unlike set_camera_angle this leaves state.yaw to the heartbeat, so callers can poll
        heartbeat_yaw() to tell when the move actually finished.
        """
        start = self._heartbeat_yaw if self._heartbeat_yaw is not None else (self.state.yaw or 0.0)
        pitch = self.state.pitch or 0
        payload = {
            "gimbal_control": {
                "mode": 1,
                "yaw_start": _gimbal_num(start),
                "yaw_speed": _gimbal_num(yaw_speed),
                "yaw_end": _gimbal_num(target_yaw),
                "pitch_start": _gimbal_num(pitch),
                "pitch_speed": 0,
                "pitch_end": _gimbal_num(pitch),
                "zoom_start": 1,
                "zoom_speed": 0,
                "zoom_end": 1,
            }
        }
        await self._send(payload)
        self.state.last_command = "gimbal_control"
        self._touch()
        await self._publish_state()
        return self.state

    def _arm_goal_tracking(self, command: RobotGoalCommand) -> None:
        self._pending_goal = command
        self._require_non_done = True
        self._arrival_result = None
        self._arrival_event.clear()

    def _disarm_goal_tracking(self) -> None:
        self._pending_goal = None
        self._require_non_done = False

    def _resolve_arrival(self, status: str) -> None:
        self._pending_goal = None
        self._require_non_done = False
        self._arrival_result = status
        self._arrival_event.set()

    def _evaluate_arrival(self, task: dict[str, Any]) -> None:
        if self._pending_goal is None:
            return
        goal_status = _normalize_failed(_maybe_str(task.get("goal_status")))
        if goal_status not in {"done", "failed"}:
            # Robot is actively navigating, so any later settled status belongs to this goal.
            self._require_non_done = False
            return
        if self._require_non_done:
            return
        if self._pending_goal.goal_object and task.get("object_status") == "going":
            # Only points that named a goal_object wait for the gimbal to finish aligning.
            return
        self._resolve_arrival(goal_status or "failed")

    async def stop_motion(self) -> RobotState:
        self.state.moving = False
        self.state.last_command = "stop_motion"
        self.state.error = "Stop motion is not defined by the hardware websocket protocol"
        self._touch()
        await self._publish_state()
        return self.state

    async def move(self, command: MoveCommand) -> RobotState:
        raise ValueError("Manual movement is not defined by the hardware websocket protocol")

    async def set_camera_angle(self, angle: CameraAngle) -> RobotState:
        yaw_start = self.state.yaw if self.state.yaw is not None else self.state.camera_angle
        pitch = self.state.pitch if self.state.pitch is not None else 0
        payload = {
            "gimbal_control": {
                "mode": 1,
                "yaw_start": _gimbal_num(yaw_start),
                "yaw_speed": 5,
                "yaw_end": _gimbal_num(angle.angle),
                "pitch_start": _gimbal_num(pitch),
                "pitch_speed": 0,
                "pitch_end": _gimbal_num(pitch),
                "zoom_start": 1,
                "zoom_speed": 0,
                "zoom_end": 1,
            }
        }
        await self._send(payload)
        self.state.camera_angle = angle.angle
        self.state.yaw = angle.angle
        self.state.last_command = "gimbal_control"
        self._touch()
        await self._publish_state()
        return self.state

    async def set_gimbal(self, command: GimbalMoveRequest) -> RobotState:
        payload = {
            "gimbal_control": {
                "mode": 1,
                "yaw_start": _gimbal_num(command.yaw_start),
                "yaw_speed": _gimbal_num(command.yaw_speed),
                "yaw_end": _gimbal_num(command.yaw_end),
                "pitch_start": _gimbal_num(command.pitch_start),
                "pitch_speed": _gimbal_num(command.pitch_speed),
                "pitch_end": _gimbal_num(command.pitch_end),
                "zoom_start": _gimbal_num(command.zoom_start),
                "zoom_speed": 0,
                "zoom_end": _gimbal_num(command.zoom_end),
            }
        }
        await self._send(payload)
        self.state.camera_angle = command.yaw_end
        self.state.yaw = command.yaw_end
        self.state.pitch = command.pitch_end
        self.state.last_command = "gimbal_control"
        self._touch()
        await self._publish_state()
        return self.state

    async def start_recording(self) -> RobotState:
        response = await self._request(
            {"video_record": {"start": 0, "resolution": 4}},
            "robot_video_record",
            timeout_s=8.0,
        )
        if not _response_ok(response):
            self.state.recording = False
            raise ValueError(f"机器人拒绝开始录制：{_response_error(response)}")
        self.state.recording = True
        self.state.media_url = str(response.get("url") or "") or self.state.media_url
        self.state.last_command = "video_record:start"
        self._touch()
        await self._publish_state()
        return self.state

    async def stop_recording(self) -> RobotState:
        response = await self._request(
            {"video_record": {"stop": 0}},
            "robot_video_record",
            timeout_s=8.0,
        )
        if not _response_ok(response):
            raise ValueError(f"机器人拒绝停止录制：{_response_error(response)}")
        self.state.recording = False
        self.state.media_url = str(response.get("url") or "") or self.state.media_url
        if not self.state.media_url:
            raise ValueError("机器人停止了录制，但没有返回视频地址")
        self.state.last_command = "video_record:stop"
        self._touch()
        await self._publish_state()
        return self.state

    async def capture_photo(self) -> dict[str, Any]:
        response = await self._request(
            {"take_photo": {"counter": 1, "gap": 0}},
            "robot_take_photo",
            timeout_s=8.0,
        )
        if not _response_ok(response):
            raise ValueError(f"机器人拍照失败：{_response_error(response)}")
        self.state.media_url = str(response.get("url") or "")
        if not self.state.media_url:
            raise ValueError("机器人报告拍照成功，但没有返回照片地址")
        self.state.last_command = "take_photo"
        self._touch()
        await self._publish_state()
        return response if isinstance(response, dict) else {"raw": response}

    async def _request(
        self,
        payload: dict[str, Any],
        response_key: str,
        timeout_s: float = 5.0,
    ) -> Any:
        async with self._request_lock:
            await self.connect()
            if not self._socket or not self.state.connected:
                raise ConnectionError(self.state.error or "Robot websocket is not connected")

            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()
            self._pending[response_key] = future
            try:
                await self._send(payload)
                try:
                    return await asyncio.wait_for(future, timeout=timeout_s)
                except TimeoutError as exc:
                    log_event(
                        "error",
                        "robot.reply.timeout",
                        response_key=response_key,
                        timeout_seconds=timeout_s,
                    )
                    raise TimeoutError(
                        f"等待机器人回复超时：{response_key}（{timeout_s:g} 秒）"
                    ) from exc
            finally:
                if self._pending.get(response_key) is future:
                    self._pending.pop(response_key, None)

    async def _send(self, payload: dict[str, Any]) -> None:
        await self.connect()
        if not self._socket or not self.state.connected:
            raise ConnectionError(self.state.error or "Robot websocket is not connected")
        async with self._send_lock:
            log_event("info", "robot.command.sent", payload=payload)
            await self._socket.send(json.dumps(payload, ensure_ascii=False))

    def _start_connection_loop(self) -> None:
        if not self.websocket_url:
            return
        if self._connection_task and not self._connection_task.done():
            return
        self._connection_task = asyncio.create_task(self._connection_loop())

    async def _stop_connection_loop(self) -> None:
        self._fail_pending(ConnectionError("Robot websocket was reconfigured"))
        socket = self._socket
        self._socket = None
        if socket:
            with suppress(Exception):
                await socket.close()
        if self._connection_task and not self._connection_task.done():
            self._connection_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._connection_task
        self._connection_task = None

    async def _connection_loop(self) -> None:
        first_attempt = True
        while self.websocket_url:
            socket: Any | None = None
            try:
                websockets = _load_websockets()
                self.state.connected = False
                self.state.connection_status = "connecting" if first_attempt else "reconnecting"
                self.state.error = None
                self._touch()
                await self._publish_state()

                socket = await websockets.connect(
                    self.websocket_url,
                    ping_interval=15,
                    ping_timeout=8,
                    open_timeout=5,
                    close_timeout=2,
                    max_queue=32,
                )
                self._socket = socket
                self._reconnect_delay_s = 1.0
                first_attempt = False
                self.state.connected = True
                self.state.connection_status = "connected"
                self.state.error = None
                self._touch()
                await self._publish_state()
                log_event("info", "robot.websocket.connected", websocket_url=self.websocket_url)
                await self._receive_messages(socket)
            except asyncio.CancelledError:
                raise
            except ModuleNotFoundError as exc:
                self.state.connected = False
                self.state.connection_status = "error"
                self.state.error = "Python package 'websockets' is required for robot hardware"
                self._touch()
                self._fail_pending(exc)
                await self._publish_state()
                return
            except Exception as exc:
                self.state.connected = False
                self.state.connection_status = "reconnecting"
                self.state.error = str(exc)
                self._touch()
                self._fail_pending(exc)
                await self._publish_state()
                log_event("error", "robot.websocket.error", error=str(exc), websocket_url=self.websocket_url)
                await asyncio.sleep(self._reconnect_delay_s)
                self._reconnect_delay_s = min(
                    self._max_reconnect_delay_s,
                    self._reconnect_delay_s * 1.7,
                )
            finally:
                if socket and self._socket is socket:
                    self._socket = None

    async def _receive_messages(self, socket: Any) -> None:
        async for raw in socket:
            await self._handle_message(raw)

    async def _handle_message(self, raw: str | bytes) -> None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return

        replies = {key: value for key, value in payload.items() if key.startswith("robot_")}
        if replies:
            log_event("info", "robot.reply.received", reply=replies)

        resolved = False
        for key, future in list(self._pending.items()):
            if key in payload and not future.done():
                future.set_result(payload[key])
                resolved = True

        state_changed = self._apply_protocol_state(payload)
        if resolved or state_changed:
            self._touch()
            await self._publish_state()

    def _apply_protocol_state(self, payload: dict[str, Any]) -> bool:
        changed = False
        system = payload.get("system") if isinstance(payload.get("system"), dict) else {}
        current_map = payload.get("map") if isinstance(payload.get("map"), dict) else {}
        navigation = (
            payload.get("naviagtion")
            if isinstance(payload.get("naviagtion"), dict)
            else payload.get("navigation")
            if isinstance(payload.get("navigation"), dict)
            else {}
        )
        task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
        gimbal = payload.get("gimbal") if isinstance(payload.get("gimbal"), dict) else {}

        if system or current_map or navigation or task or gimbal:
            self.state.connected = True
            self.state.connection_status = "connected"
            self.state.error = None
            changed = True

        if system:
            self.state.battery = _maybe_int(system.get("battery"))
            self.state.system_status = _maybe_str(system.get("status"))
        if current_map:
            self.state.map_name = _maybe_str(current_map.get("name"))
            self.state.map_mode = _maybe_str(current_map.get("mode"))
            self.state.map_status = _normalize_failed(_maybe_str(current_map.get("status")))
        if navigation:
            self.state.navigation_status = _maybe_str(navigation.get("status"))
            self.state.goal_status = _maybe_str(navigation.get("goal_status"))
            self.state.moving = navigation.get("goal_status") == "going"
        if task:
            self.state.path_file = _maybe_str(task.get("path_file"))
            self.state.goal_id = _maybe_int(task.get("goal_id"))
            self.state.goal_object = _maybe_str(task.get("goal_object"))
            self.state.goal_status = _normalize_failed(_maybe_str(task.get("goal_status"))) or self.state.goal_status
            self.state.object_status = _normalize_failed(_maybe_str(task.get("object_status")))
            self.state.moving = task.get("goal_status") == "going"
            self._evaluate_arrival(task)
        if gimbal:
            # Only when the field is actually there. Absent, `.get` returns None, which is not
            # "recording" and would silently rewrite a running recording as stopped — a
            # heartbeat carrying only yaw would do it. Nothing else now decides whether to
            # send the stop command, so a missing field must mean "unchanged", not "no".
            if "record_status" in gimbal:
                self.state.recording = gimbal.get("record_status") == "recording"
            self.state.yaw = _maybe_float(gimbal.get("yaw"))
            self._heartbeat_yaw = self.state.yaw
            self.state.pitch = _maybe_float(gimbal.get("pitch"))
            self._heartbeat_pitch = self.state.pitch
            self._heartbeat_revision = (self._heartbeat_revision or 0) + 1
            if self.state.yaw is not None:
                self.state.camera_angle = self.state.yaw

        record = payload.get("robot_video_record")
        if isinstance(record, dict):
            if record.get("status") == "ok" and "stop" in record:
                self.state.recording = False
            elif record.get("status") == "ok":
                self.state.recording = True
            self.state.media_url = _maybe_str(record.get("url")) or self.state.media_url
            changed = True

        photo = payload.get("robot_take_photo")
        if isinstance(photo, dict):
            self.state.media_url = _maybe_str(photo.get("url")) or self.state.media_url
            changed = True

        return changed

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        # Never leave a cruise blocked on an arrival that can no longer be reported.
        if self._pending_goal is not None:
            self._resolve_arrival("failed")

    async def _publish_state(self) -> None:
        await self.events.publish("ROBOT_STATE", self.state.model_dump(mode="json"))

    def _touch(self) -> None:
        self.state.updated_at = utc_now()


class RobotService:
    def __init__(
        self,
        events: EventHub,
        adapter: RobotAdapter | None = None,
        websocket_url: str = "",
        media: MediaService | None = None,
    ) -> None:
        self.events = events
        self.adapter = adapter or HardwareRobotAdapter(events, websocket_url)
        self.media = media

    async def configure_websocket_url(self, websocket_url: str) -> RobotState:
        return await self.adapter.configure_websocket_url(websocket_url)

    async def status(self) -> RobotState:
        return await self.adapter.status()

    async def connect(self) -> RobotState:
        state = await self.adapter.connect()
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def disconnect(self) -> RobotState:
        state = await self.adapter.disconnect()
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def map_list(self) -> list[str]:
        return await self.adapter.map_list()

    async def switch_map(self, map_name: str) -> dict[str, Any]:
        result = await self.adapter.switch_map(map_name)
        await self.events.publish("ROBOT_COMMAND", {"type": "set_switch_map", "result": result})
        return result

    async def path_list(self, map_name: str) -> list[str]:
        return await self.adapter.path_list(map_name)

    def refuse_if_unfit_to_drive(self) -> None:
        refuse_if_unfit_to_drive(getattr(self.adapter, "state", None))

    async def set_goal(self, command: RobotGoalCommand) -> dict[str, Any]:
        result = await self.adapter.set_goal(command)
        await self.events.publish("ROBOT_COMMAND", {"type": "set_goal", "result": result})
        return result

    async def wait_for_arrival(self, timeout_s: float = 60.0) -> str:
        return await self.adapter.wait_for_arrival(timeout_s)

    async def sweep_camera(self, target_yaw: float, yaw_speed: float) -> RobotState:
        state = await self.adapter.sweep_camera(target_yaw, yaw_speed)
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    def heartbeat_yaw(self) -> float | None:
        return self.adapter.heartbeat_yaw()

    def heartbeat_pitch(self) -> float | None:
        reader = getattr(self.adapter, "heartbeat_pitch", None)
        return reader() if callable(reader) else None

    def heartbeat_revision(self) -> int | None:
        reader = getattr(self.adapter, "heartbeat_revision", None)
        return reader() if callable(reader) else None

    async def stop_motion(self) -> RobotState:
        state = await self.adapter.stop_motion()
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def move(self, command: MoveCommand) -> RobotState:
        state = await self.adapter.move(command)
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def set_camera_angle(self, angle: CameraAngle) -> RobotState:
        state = await self.adapter.set_camera_angle(angle)
        log_event("info", "gimbal.commanded", target_yaw=angle.angle)
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def set_gimbal(self, command: GimbalMoveRequest) -> RobotState:
        state = await self.adapter.set_gimbal(command)
        log_event(
            "info", "gimbal.moved",
            yaw_end=command.yaw_end, pitch_end=command.pitch_end, zoom_end=command.zoom_end,
        )
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def _clear_media_state(self) -> None:
        state = await self.adapter.status()
        state.media_url = None
        state.media_local_path = None
        state.media_sync_error = None
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))

    async def start_recording(self) -> RobotState:
        """Start recording at the operator's current lens angle.

        The protocol does not define which reported yaw is the physical front, so ordinary
        capture must not issue an implicit gimbal command. Camera movement remains explicit.
        """
        await self._clear_media_state()
        state = await self.adapter.start_recording()
        log_event("info", "capture.recording.started", recording=state.recording)
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def stop_recording(self, sync_media: bool = True) -> RobotState:
        state = await self.adapter.stop_recording()
        if sync_media:
            # Recording and transferring the resulting file are separate outcomes. Once the
            # robot accepted stop, a network/routing failure must not rewrite that success as
            # "recording failed" or leave the capture session active.
            await self._sync_robot_media(state.media_url, "video")
        log_event(
            "info",
            "capture.recording.stopped",
            robot_url=state.media_url,
            local_path=state.media_local_path,
            media_sync_error=state.media_sync_error,
        )
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def capture_photo(self) -> dict[str, Any]:
        await self._clear_media_state()
        result = await self.adapter.capture_photo()
        synced = await self._sync_robot_media(str(result.get("url") or ""), "image")
        if synced:
            result["local_media_item"] = synced.model_dump(mode="json")
        state = await self.adapter.status()
        result["media_sync_error"] = state.media_sync_error
        log_event(
            "info",
            "capture.photo.completed",
            robot_url=result.get("url"),
            local_path=synced.path if synced else None,
            media_sync_error=state.media_sync_error,
        )
        await self.events.publish("ROBOT_PHOTO", result)
        return result

    async def _sync_robot_media(
        self,
        robot_url: str | None,
        kind_hint: str,
    ) -> MediaItem | None:
        from automated_video_editing_backend.services.media import resolve_robot_media_url

        state = await self.adapter.status()
        state.media_sync_error = None
        if not self.media:
            error = "媒体服务不可用，无法保存机器人文件"
            await self._media_sync_failed(state, robot_url or "", kind_hint, error)
            return None

        try:
            download_url = resolve_robot_media_url(
                str(robot_url or ""),
                str(getattr(self.adapter, "websocket_url", "") or ""),
            )
            log_event(
                "info",
                "robot.media.download.started",
                robot_url=robot_url,
                resolved_url=download_url,
                kind=kind_hint,
            )
            item = await self.media.download_url(
                download_url,
                metadata={
                    "source": "data/downloads",
                    "origin": "robot_hardware",
                    "robot_url": download_url,
                    "kind_hint": kind_hint,
                },
                filename_prefix="robot-",
            )
        except Exception as exc:
            error = str(exc)
            await self._media_sync_failed(state, robot_url or "", kind_hint, error)
            return None

        state.media_local_path = item.path
        state.media_sync_error = None
        log_event(
            "info",
            "robot.media.download.completed",
            robot_url=robot_url,
            local_path=item.path,
            kind=kind_hint,
        )
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        await self.events.publish(
            "ROBOT_MEDIA_SYNCED",
            {"url": robot_url, "kind_hint": kind_hint, "media_item": item.model_dump(mode="json")},
        )
        return item

    async def _media_sync_failed(
        self,
        state: RobotState,
        robot_url: str,
        kind_hint: str,
        error: str,
    ) -> None:
        state.media_sync_error = error
        log_event(
            "error",
            "robot.media.download.failed",
            robot_url=robot_url,
            kind=kind_hint,
            error=error,
        )
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        await self.events.publish(
            "ROBOT_MEDIA_SYNC_FAILED",
            {"url": robot_url, "kind_hint": kind_hint, "error": error},
        )


def _load_websockets() -> Any:
    import websockets

    return websockets


def _response_error(response: Any) -> str:
    if isinstance(response, dict):
        return str(response.get("error") or response.get("message") or response.get("status") or response)
    return str(response)


def _response_ok(response: Any) -> bool:
    """Accept harmless firmware casing/whitespace while still requiring explicit success."""
    return isinstance(response, dict) and str(response.get("status") or "").strip().casefold() == "ok"


def refuse_if_unfit_to_drive(state: RobotState | None) -> None:
    """Stop a goal the robot has already told us it cannot carry out.

    All three of these arrive in every heartbeat and were previously read past: the goal went
    out, the robot rejected or ignored it, and the failure surfaced as a bare timeout with
    nothing pointing at the cause. A robot that has not reported yet leaves these unset, and
    an unset field never blocks — otherwise nothing would work until a heartbeat happened to
    land.
    """
    if state is None:
        return
    if state.map_mode == "mapping":
        raise ValueError("机器人正在扫图模式，无法导航。请先切回定位模式。")
    if state.system_status and state.system_status != "ready":
        raise ValueError(f"机器人硬件状态异常（{state.system_status}），先检查设备。")
    if state.map_status == "failed":
        raise ValueError("机器人定位失败，无法导航。请检查地图和当前位置。")


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _maybe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _gimbal_num(value: Any) -> int | float:
    """Serialise a gimbal number the way the working browser console (任意门) does: a whole
    number as an int (``30``, not ``30.0``) and only a real fraction as a float (``3.5``).

    The robot firmware treats yaw/pitch/speed as integers and silently ignores a whole value
    written as a float — which is why the app's gimbal commands did nothing while the browser
    tool, whose JSON.stringify emits ``30``, worked. Aligning the wire format is the fix.
    """
    number = float(value)
    return int(number) if number.is_integer() else number


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _normalize_failed(value: str | None) -> str | None:
    return "failed" if value == "faild" else value
