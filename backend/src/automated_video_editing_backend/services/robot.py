from __future__ import annotations

import asyncio
import json
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextlib import suppress
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from automated_video_editing_backend.core.diagnostics import log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraAngle,
    GimbalCommandDiagnostic,
    GimbalMoveRequest,
    GoalCommandAttemptDiagnostic,
    GoalCommandDiagnostic,
    MoveCommand,
    RobotGoalCommand,
    RobotHeartbeatDiagnostic,
    RobotMode,
    RobotState,
    utc_now,
)
from automated_video_editing_backend.services.media_download import (
    camera_media_error_message,
    exception_detail,
    retry_camera_media_download,
)

if TYPE_CHECKING:
    from automated_video_editing_backend.core.models import MediaItem
    from automated_video_editing_backend.services.media import MediaService


_RECORDING_REPLY_TIMEOUT_SECONDS = 8.0
_RECORDING_LATE_REPLY_GRACE_SECONDS = 8.0
_RECORDING_SHUTDOWN_TIMEOUT_SECONDS = 18.0
_MAP_SWITCH_CONFIRM_TIMEOUT_SECONDS = 10.0
_MAP_SWITCH_CONFIRM_POLL_SECONDS = 0.1


class _ReplyDeadlineExpired(Exception):
    """The local wait elapsed while its shared reply future was still pending."""


class RobotCommandNotSentError(ConnectionError):
    """A command failed preflight and definitely never reached the robot socket."""


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
    async def sweep_camera(
        self,
        target_yaw: float,
        yaw_speed: float,
        *,
        context: str = "camera_sweep",
    ) -> RobotState: ...

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
    async def set_gimbal(
        self,
        command: GimbalMoveRequest,
        *,
        context: str = "manual",
    ) -> RobotState: ...

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
        # Recording acknowledgements have no request id. Serialize the whole request -> state
        # commit boundary so a later opposite command cannot start while the prior caller is
        # still applying its accepted reply.
        self._recording_lock = asyncio.Lock()
        # set_goal mutates arrival ownership before it enters the generic request lock. Keep
        # that whole arm -> write -> acknowledgement transaction single-owner as well, or a
        # concurrent caller can make an old heartbeat resolve a goal that has not been sent.
        self._goal_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future[Any]] = {}
        # Some protocol operations share one top-level response key. Keep the matcher beside
        # the future so, for example, a delayed Start reply cannot complete a later Stop wait.
        self._pending_reply_matchers: dict[str, Callable[[Any], bool]] = {}
        self._reconnect_delay_s = 1.0
        self._max_reconnect_delay_s = 10.0
        self._pending_goal: RobotGoalCommand | None = None
        self._require_non_done = False
        self._arrival_event = asyncio.Event()
        self._arrival_result: str | None = None
        self._heartbeat_yaw: float | None = None
        self._heartbeat_pitch: float | None = None
        self._heartbeat_revision: int | None = None
        self._heartbeat_yaw_pending = False
        self._heartbeat_pitch_pending = False
        self._pending_goal_attempt: GoalCommandAttemptDiagnostic | None = None
        # Photo and video replies share RobotState.media_url. Keep the active recording URL
        # separately so an in-recording photo cannot become the fallback for video stop.
        self._recording_media_url: str | None = None
        # ``RobotState.recording`` defaults to false, but that is not an observation. After a
        # process restart we must wait for a heartbeat/reply before deciding a persisted URL is
        # a finished file that is safe to download.
        self._recording_status_known = False
        # A timed-out/cancelled command may still have reached the camera. Keep that ambiguous
        # operation as the explicit recovery owner; recording retries and endpoint replacement
        # are refused until a matching reply or authoritative heartbeat resolves it.
        self._recording_command_revision = 0
        self._recording_reply_recovery: tuple[int, str] | None = None
        # A cancellation is ambiguous only once execution has reached the physical socket write.
        # Keep this separate from the operation revision: a task can be cancelled while queued
        # behind another request, in which case no recording command was sent and no recovery or
        # compensating Stop is safe.
        self._recording_write_attempt_revision: int | None = None
        self._recording_compensation_task: asyncio.Task[None] | None = None
        self._last_shutdown_recording_confirmed_idle = True

    async def configure_websocket_url(self, websocket_url: str) -> RobotState:
        next_url = websocket_url.strip()
        if next_url == self.websocket_url:
            if next_url:
                self._start_connection_loop()
            return self.state

        if self.recording_operation_pending():
            raise ValueError("上一条录制指令仍在确认中，暂时不能更改机器人连接")
        if self.state.recording:
            raise ValueError("录制进行中，无法更改机器人连接")

        await self._stop_connection_loop()
        self._recording_media_url = None
        self._recording_status_known = False
        self.websocket_url = next_url
        self.state.connected = False
        self.state.connection_status = "disconnected"
        self.state.recording = False
        self.state.media_url = None
        self.state.media_local_path = None
        self.state.media_sync_error = None
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
        if self.state.recording:
            raise ValueError("机器人仍在录制，请先停止录制再断开连接")
        if self.recording_operation_pending():
            raise ValueError("上一条录制指令仍在确认中，暂时不能断开机器人连接")
        await self._stop_connection_loop()
        self._recording_media_url = None
        self._recording_status_known = False
        self.state.connected = False
        self.state.connection_status = "disconnected"
        self.state.error = None
        self._touch()
        await self._publish_state()
        return self.state

    async def shutdown(self, *, require_idle: bool = False) -> RobotState:
        """Best-effort camera-safe process teardown, distinct from user Disconnect.

        A written Start whose reply was lost still precedes this final Stop on the same websocket.
        Superseding its recovery is safe only here: the process is exiting and the required final
        hardware state is unconditionally idle. Transport closure happens even when Stop cannot be
        confirmed, and that failure remains explicit in diagnostics/state.
        """
        cleanup_error = ""
        cleanup_unconfirmed = False
        cleanup_needed = (
            self.state.recording
            or self.recording_operation_pending()
            or (require_idle and not self._recording_status_known)
        )
        self._last_shutdown_recording_confirmed_idle = not cleanup_needed
        try:
            async with asyncio.timeout(_RECORDING_SHUTDOWN_TIMEOUT_SECONDS):
                async with self._recording_lock:
                    cleanup_needed = (
                        self.state.recording
                        or self._recording_reply_recovery is not None
                        or (require_idle and not self._recording_status_known)
                    )
                    self._last_shutdown_recording_confirmed_idle = not cleanup_needed
                    if cleanup_needed:
                        abandoned = self._recording_reply_recovery
                        self._recording_reply_recovery = None
                        try:
                            await self._stop_recording_operation()
                        except Exception as exc:
                            cleanup_error = str(exc)
                            cleanup_unconfirmed = (
                                self.state.recording or not self._recording_status_known
                            )
                            self._last_shutdown_recording_confirmed_idle = (
                                not cleanup_unconfirmed
                            )
                            log_event(
                                "error" if cleanup_unconfirmed else "warning",
                                (
                                    "robot.shutdown.recording_stop_unconfirmed"
                                    if cleanup_unconfirmed
                                    else "robot.shutdown.recording_media_incomplete"
                                ),
                                abandoned_operation=abandoned,
                                error=cleanup_error,
                            )
                        else:
                            self._last_shutdown_recording_confirmed_idle = (
                                not self.state.recording and self._recording_status_known
                            )
                compensation = self._recording_compensation_task
                if compensation is not None and not compensation.done():
                    with suppress(asyncio.CancelledError, Exception):
                        await compensation
        except TimeoutError:
            cleanup_error = (
                f"安全停止超过 {_RECORDING_SHUTDOWN_TIMEOUT_SECONDS:g} 秒"
            )
            cleanup_unconfirmed = cleanup_needed
            self._last_shutdown_recording_confirmed_idle = not cleanup_needed
            log_event(
                "error",
                "robot.shutdown.recording_stop_unconfirmed",
                error=cleanup_error,
            )
        finally:
            await self._stop_connection_loop()
            self.state.connected = False
            self.state.connection_status = "disconnected"
            self.state.error = (
                (
                    f"关闭前未能确认停止录制：{cleanup_error}"
                    if cleanup_unconfirmed
                    else f"录制已停止，但文件信息不完整：{cleanup_error}"
                )
                if cleanup_error
                else None
            )
            self._touch()
            await self._publish_state()
        return self.state

    def shutdown_recording_confirmed_idle(self) -> bool:
        return self._last_shutdown_recording_confirmed_idle

    async def status(self) -> RobotState:
        if self.websocket_url:
            self._start_connection_loop()
        return self.state

    def pending_recording_media_url(self) -> str | None:
        """Return the video URL without exposing a later photo URL as its replacement."""
        return self._recording_media_url

    def recording_status_known(self) -> bool:
        """Whether recording/idle came from hardware rather than the model default."""
        return self._recording_status_known

    async def map_list(self) -> list[str]:
        response = await self._request({"get_map_list": "all"}, "robot_map_list")
        return response if isinstance(response, list) else []

    async def switch_map(self, map_name: str) -> dict[str, Any]:
        response = await self._request({"set_switch_map": map_name}, "robot_switch_map")
        self.state.last_command = "set_switch_map"
        self._touch()
        await self._publish_state()
        # ``robot_switch_map`` acknowledges only that the requested file exists.  It is not
        # evidence that localization has moved to that map; only a later heartbeat may update
        # RobotState.map_name.
        return {"map_name": map_name, "ok": _truthy(response), "raw": response}

    async def path_list(self, map_name: str) -> list[str]:
        response = await self._request({"get_path_list": map_name}, "robot_path_list")
        return response if isinstance(response, list) else []

    async def set_goal(self, command: RobotGoalCommand) -> dict[str, Any]:
        async with self._goal_lock:
            return await self._set_goal_locked(command)

    async def _set_goal_locked(self, command: RobotGoalCommand) -> dict[str, Any]:
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
        self._pending_goal_attempt = None
        try:
            response = await self._request(payload, "robot_goal")
        except Exception as exc:
            if self._pending_goal_attempt is not None:
                outcome = "reply_timeout" if isinstance(exc, TimeoutError) else "reply_error"
                self._finish_goal_attempt(outcome, str(exc))
            self._pending_goal_attempt = None
            self._disarm_goal_tracking()
            self.state.last_command = "set_goal"
            self._touch()
            await self._publish_state()
            raise
        if isinstance(response, dict):
            if _goal_identity_conflicts(response, command):
                if self._pending_goal_attempt is not None:
                    self._finish_goal_attempt(
                        "reply_mismatch",
                        "机器人回复的目标点与本次指令不一致",
                    )
                self._pending_goal_attempt = None
                self._disarm_goal_tracking()
                self.state.last_command = "set_goal"
                self._touch()
                await self._publish_state()
                raise ValueError("机器人回复的目标点与本次指令不一致")
            self.state.path_file = str(response.get("path_file") or command.path_name)
            response_goal_id = _maybe_int(response.get("goal_id"))
            self.state.goal_id = (
                response_goal_id if response_goal_id is not None else command.goal_id
            )
            self.state.goal_object = _maybe_str(response.get("goal_object") or command.goal_object)
            accepted = _truthy(response.get("goal_check"))
            self.state.goal_status = "going" if accepted else "failed"
            if accepted and self._pending_goal_attempt is not None:
                attempt = self._finish_goal_attempt("accepted")
                goal_diagnostic = GoalCommandDiagnostic(
                    context=attempt.context,
                    sent_at=attempt.sent_at,
                    base_motion_intent=attempt.base_motion_intent,
                    payload=deepcopy(attempt.payload),
                )
                self.state.diagnostics.last_goal_command = goal_diagnostic
                current_gimbal = self.state.diagnostics.last_gimbal_command
                current_is_newer = (
                    current_gimbal is not None
                    and current_gimbal.sent_at > goal_diagnostic.sent_at
                )
                if command.goal_object and not current_is_newer:
                    # Object alignment gives the robot ownership of the numeric camera angle,
                    # so the previous direct target is no longer a valid comparison.
                    self.state.diagnostics.last_gimbal_command = GimbalCommandDiagnostic(
                        context="goal_object_alignment",
                        sent_at=goal_diagnostic.sent_at,
                        base_motion_intent="moving",
                        payload=deepcopy(goal_diagnostic.payload),
                    )
                elif (
                    not command.goal_object
                    and current_gimbal is not None
                    and current_gimbal.context == "goal_object_alignment"
                    and not current_is_newer
                ):
                    # Navigation-only leaves a direct numeric gimbal target valid, but an old
                    # robot-owned object target must not leak into the new point.
                    self.state.diagnostics.last_gimbal_command = None
            elif not accepted:
                if self._pending_goal_attempt is not None:
                    rejection_reason = str(
                        response.get("error")
                        or response.get("message")
                        or response.get("status")
                        or "机器人拒绝目标点"
                    )
                    self._finish_goal_attempt("rejected", rejection_reason)
                self._disarm_goal_tracking()
        else:
            if self._pending_goal_attempt is not None:
                self._finish_goal_attempt(
                    "reply_error",
                    "机器人目标点回复格式无效",
                )
            self._disarm_goal_tracking()
        self._pending_goal_attempt = None
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
        """Monotonic revision, or None until both axes form one logical pose sample."""
        return self._heartbeat_revision

    async def sweep_camera(
        self,
        target_yaw: float,
        yaw_speed: float,
        *,
        context: str = "camera_sweep",
    ) -> RobotState:
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
        await self._send(payload, context=context)
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

    def _finish_goal_attempt(
        self,
        outcome: str,
        error: str | None = None,
    ) -> GoalCommandAttemptDiagnostic:
        """Finish only the attempt that still owns the current goal request."""
        attempt = self._pending_goal_attempt
        if attempt is None:
            raise RuntimeError("No goal command attempt is pending")
        finished = attempt.model_copy(
            update={
                "outcome": outcome,
                "completed_at": utc_now(),
                "error": error or None,
            }
        )
        visible = self.state.diagnostics.last_goal_attempt
        if visible is not None and visible.attempt_id == attempt.attempt_id:
            self.state.diagnostics.last_goal_attempt = finished
        self._pending_goal_attempt = finished
        return finished

    def _evaluate_arrival(self, task: dict[str, Any]) -> None:
        if self._pending_goal is None:
            return
        if _goal_identity_conflicts(task, self._pending_goal):
            return
        explicit_current_identity = _goal_identity_is_explicit_match(task, self._pending_goal)
        goal_status = _normalize_failed(_maybe_str(task.get("goal_status")))
        if goal_status not in {"done", "failed"}:
            if goal_status is not None:
                # A non-terminal report proves that subsequent terminal reports are not the
                # cached result from the point we just left.
                self._require_non_done = False
            return
        if self._require_non_done and not explicit_current_identity:
            return
        # Product decision: object recognition/alignment is telemetry only. It must never
        # block, fail, or delay a cruise point; navigation goal_status owns arrival.
        self._resolve_arrival(goal_status)

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
        await self._send(payload, context="manual")
        self.state.camera_angle = angle.angle
        self.state.yaw = angle.angle
        self.state.last_command = "gimbal_control"
        self._touch()
        await self._publish_state()
        return self.state

    async def set_gimbal(
        self,
        command: GimbalMoveRequest,
        *,
        context: str = "manual",
    ) -> RobotState:
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
        await self._send(payload, context=context)
        self.state.camera_angle = command.yaw_end
        self.state.yaw = command.yaw_end
        self.state.pitch = command.pitch_end
        self.state.last_command = "gimbal_control"
        self._touch()
        await self._publish_state()
        return self.state

    def _begin_recording_operation(self, action: str) -> int:
        if self._recording_reply_recovery is not None:
            action_name = "开始录制" if action == "start" else "停止录制"
            raise ValueError(
                f"上一条录制指令仍在确认中，请等待机器人同步状态后再{action_name}"
            )
        self._recording_command_revision += 1
        revision = self._recording_command_revision
        self._recording_write_attempt_revision = None
        return revision

    def recording_operation_pending(self) -> bool:
        """Whether disconnect/reconfiguration could discard recording command ownership."""
        compensation = self._recording_compensation_task
        return (
            self._recording_lock.locked()
            or self._recording_reply_recovery is not None
            or (compensation is not None and not compensation.done())
        )

    def _abandon_recording_operation(self, revision: int, action: str) -> None:
        if revision != self._recording_command_revision:
            return
        if self._recording_write_attempt_revision != revision:
            # Cancellation before socket.send is definitive: this operation did not reach the
            # robot, so a later heartbeat must not be interpreted as its result.
            return
        self._recording_reply_recovery = (revision, action)
        # Until either the matching late reply or a heartbeat showing the command's resulting
        # state arrives, the process does not know whether the camera applied the command.
        self._recording_status_known = False

    async def start_recording(self) -> RobotState:
        async with self._recording_lock:
            revision = self._begin_recording_operation("start")
            # A new capture must never inherit the previous file as its recovery candidate.
            # Clear before sending because send/ack failure is precisely where stale fallback
            # is unsafe.
            self._recording_media_url = None
            try:
                response = await self._request(
                    {"video_record": {"start": 0, "resolution": 4}},
                    "robot_video_record",
                    timeout_s=_RECORDING_REPLY_TIMEOUT_SECONDS,
                )
            except RobotCommandNotSentError:
                raise
            except asyncio.CancelledError:
                self._abandon_recording_operation(revision, "start")
                raise
            except Exception:
                self._abandon_recording_operation(revision, "start")
                raise
            if not _response_ok(response):
                self.state.recording = False
                self._recording_status_known = True
                raise ValueError(f"机器人拒绝开始录制：{_response_error(response)}")
            self.state.recording = True
            self._recording_status_known = True
            self._recording_media_url = str(response.get("url") or "").strip() or None
            self.state.media_url = self._recording_media_url
            self.state.last_command = "video_record:start"
            self._touch()
            await self._publish_state()
            return self.state

    async def _stop_recording_operation(self) -> RobotState:
        """Perform one serialized Stop; caller owns ``_recording_lock``."""
        revision = self._begin_recording_operation("stop")
        try:
            response = await self._request(
                {"video_record": {"stop": 0}},
                "robot_video_record",
                timeout_s=_RECORDING_REPLY_TIMEOUT_SECONDS,
            )
        except RobotCommandNotSentError:
            raise
        except asyncio.CancelledError:
            self._abandon_recording_operation(revision, "stop")
            raise
        except Exception:
            self._abandon_recording_operation(revision, "stop")
            raise
        if not _response_ok(response):
            raise ValueError(f"机器人拒绝停止录制：{_response_error(response)}")
        self.state.recording = False
        self._recording_status_known = True
        self.state.media_url = (
            str(response.get("url") or "").strip() or self._recording_media_url
        )
        self._recording_media_url = None
        if not self.state.media_url:
            raise ValueError("机器人停止了录制，但没有返回视频地址")
        self.state.last_command = "video_record:stop"
        self._touch()
        await self._publish_state()
        return self.state

    async def stop_recording(self) -> RobotState:
        async with self._recording_lock:
            # An orphan-cleanup Stop can win the lock just before an operator/finalizer Stop.
            # Treat that exact, already-confirmed result as idempotent instead of sending an
            # invalid duplicate command to an idle camera.
            if (
                not self.state.recording
                and self._recording_status_known
                and self.state.last_command == "video_record:stop"
                and bool(self.state.media_url)
            ):
                return self.state
            return await self._stop_recording_operation()

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
                raise RobotCommandNotSentError(
                    self.state.error or "Robot websocket is not connected"
                )

            loop = asyncio.get_running_loop()
            future: asyncio.Future[Any] = loop.create_future()
            self._pending[response_key] = future
            reply_matcher = _reply_matcher(payload, response_key)
            if reply_matcher is not None:
                self._pending_reply_matchers[response_key] = reply_matcher
            late_reply_grace_s = (
                _RECORDING_LATE_REPLY_GRACE_SECONDS
                if reply_matcher is not None
                else 0.0
            )
            try:
                await self._send(payload)
                try:
                    # Shielding is essential: wait_for otherwise cancels the shared future at
                    # the primary deadline, making a slightly late hardware acknowledgement
                    # impossible to reconcile with the operation that sent the command.
                    return await _wait_for_reply(future, timeout_s)
                except _ReplyDeadlineExpired as exc:
                    if late_reply_grace_s > 0:
                        log_event(
                            "warning",
                            "robot.reply.delayed",
                            response_key=response_key,
                            primary_timeout_seconds=timeout_s,
                            grace_seconds=late_reply_grace_s,
                        )
                        try:
                            response = await _wait_for_reply(future, late_reply_grace_s)
                        except _ReplyDeadlineExpired:
                            pass
                        else:
                            log_event(
                                "info",
                                "robot.reply.reconciled",
                                response_key=response_key,
                                primary_timeout_seconds=timeout_s,
                            )
                            return response
                    log_event(
                        "error",
                        "robot.reply.timeout",
                        response_key=response_key,
                        timeout_seconds=timeout_s + late_reply_grace_s,
                        primary_timeout_seconds=timeout_s,
                        grace_seconds=late_reply_grace_s,
                    )
                    raise TimeoutError(
                        "等待机器人回复超时："
                        f"{response_key}（{timeout_s + late_reply_grace_s:g} 秒）"
                    ) from exc
            finally:
                if self._pending.get(response_key) is future:
                    self._pending.pop(response_key, None)
                self._pending_reply_matchers.pop(response_key, None)
                if not future.done():
                    future.cancel()

    async def _send(self, payload: dict[str, Any], *, context: str = "") -> None:
        await self.connect()
        if not self._socket or not self.state.connected:
            raise RobotCommandNotSentError(
                self.state.error or "Robot websocket is not connected"
            )
        gimbal = payload.get("gimbal_control")
        goal = payload.get("set_goal")
        is_goal_command = isinstance(goal, dict)
        goal_aligns_object = is_goal_command and bool(_maybe_str(goal.get("goal_object")))
        async with self._send_lock:
            if isinstance(gimbal, dict) or goal_aligns_object:
                # A new target is a physical-sample boundary. A yaw received before this
                # command must never be paired with a pitch received after it (or vice versa).
                self._heartbeat_yaw_pending = False
                self._heartbeat_pitch_pending = False
            # Capture the command boundary before yielding to socket.send. The receive loop can
            # process an immediate reply before send() resumes, but the public diagnostic is
            # still created only after the write succeeds.
            sent_at = utc_now()
            video_record = payload.get("video_record")
            if _video_record_action(video_record) is not None:
                # Mark immediately before the real write. Cancellation anywhere earlier is
                # definitively pre-send; cancellation inside send remains correctly ambiguous.
                self._recording_write_attempt_revision = self._recording_command_revision
            await self._socket.send(json.dumps(payload, ensure_ascii=False))
            log_event("info", "robot.command.sent", payload=payload)
            if isinstance(gimbal, dict):
                # Record only after websocket.send succeeds. This still does not claim hardware
                # execution; the independently received heartbeat is the evidence for that.
                normalized_context = context or "manual"
                if normalized_context == "cruise_moving":
                    base_motion_intent = "moving"
                elif normalized_context in {
                    "cruise_stationary_camerawork",
                    "cruise_stationary_anchor",
                    "cruise_stationary_zoom",
                    "framing_test",
                }:
                    base_motion_intent = "stationary"
                else:
                    base_motion_intent = "unknown"
                self.state.diagnostics.last_gimbal_command = GimbalCommandDiagnostic(
                    context=normalized_context,
                    sent_at=sent_at,
                    base_motion_intent=base_motion_intent,
                    payload={"gimbal_control": deepcopy(gimbal)},
                )
            elif is_goal_command:
                # The public attempt says only that the socket write succeeded.  The separate
                # accepted command remains unchanged until robot_goal confirms this point.
                attempt = GoalCommandAttemptDiagnostic(
                    context=(
                        "goal_object_alignment"
                        if goal_aligns_object
                        else "cruise_navigation_goal"
                    ),
                    sent_at=sent_at,
                    base_motion_intent="moving",
                    payload={"set_goal": deepcopy(goal)},
                )
                self._pending_goal_attempt = attempt
                self.state.diagnostics.last_goal_attempt = attempt
                self.state.last_command = "set_goal"
                self._touch()
                await self._publish_state()

    def _start_connection_loop(self) -> None:
        if not self.websocket_url:
            return
        if self._connection_task and not self._connection_task.done():
            return
        self._connection_task = asyncio.create_task(self._connection_loop())

    async def _stop_connection_loop(self) -> None:
        # Replies queued on the old socket must never recover or complete work on a replacement
        # connection.
        self._recording_command_revision += 1
        self._recording_reply_recovery = None
        self._recording_write_attempt_revision = None
        self._recording_status_known = False
        self._clear_heartbeat_diagnostics()
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
        # Closing/cancelling yields to the receive loop; a final queued heartbeat may arrive
        # after the early clear. Clear again only once the old connection is fully quiescent.
        self._clear_heartbeat_diagnostics()

    async def _connection_loop(self) -> None:
        first_attempt = True
        while self.websocket_url:
            socket: Any | None = None
            try:
                websockets = _load_websockets()
                self._recording_status_known = False
                self._clear_heartbeat_diagnostics()
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
                self._recording_status_known = False
                self.state.connected = False
                self.state.connection_status = "reconnecting"
                self.state.error = str(exc)
                self._touch()
                self._fail_pending(exc)
                await self._publish_state()
                log_event(
                    "error",
                    "robot.websocket.error",
                    error=str(exc),
                    websocket_url=self.websocket_url,
                )
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
        recording_reply_resolved = False
        for key, future in list(self._pending.items()):
            if key in payload and not future.done():
                response = payload[key]
                matcher = self._pending_reply_matchers.get(key)
                if matcher is None or matcher(response):
                    future.set_result(response)
                    resolved = True
                    recording_reply_resolved = (
                        recording_reply_resolved or key == "robot_video_record"
                    )

        recovered_recording_reply = False
        record = payload.get("robot_video_record")
        if isinstance(record, dict) and not recording_reply_resolved:
            recovered_recording_reply = self._recover_recording_reply(record)
            if not recovered_recording_reply:
                log_event(
                    "warning",
                    "robot.reply.quarantined",
                    response_key="robot_video_record",
                    action=_video_record_action(record),
                    reason=(
                        "no_matching_recovery"
                        if self._recording_reply_recovery is None
                        else "action_mismatch_or_ambiguous"
                    ),
                )

        state_changed = self._apply_protocol_state(payload)
        if resolved or recovered_recording_reply or state_changed:
            self._touch()
            await self._publish_state()

    def _recover_recording_reply(self, response: dict[str, Any]) -> bool:
        """Apply a late reply only while its abandoned command still owns recovery.

        The wire protocol has no request id, so a newer recording command is not allowed to
        replace this ownership. Replies without the documented Start/Stop echo are never guessed.
        """
        action = _video_record_action(response)
        recovery = self._recording_reply_recovery
        if action is None or recovery is None:
            return False
        revision, expected_action = recovery
        if revision != self._recording_command_revision or action != expected_action:
            return False
        if response.get(action) != 0:
            return False
        status = str(response.get("status") or "").strip()
        if not status:
            return False

        self._recording_reply_recovery = None
        self._recording_status_known = True
        if _response_ok(response):
            record_url = _maybe_str(response.get("url"))
            if action == "start":
                self.state.recording = True
                self._recording_media_url = record_url
                self.state.media_url = record_url
            else:
                self.state.recording = False
                # Shared state.media_url may have been replaced by an in-recording photo. Only
                # recording-owned URLs are safe fallbacks for a late Stop acknowledgement.
                self.state.media_url = record_url or self._recording_media_url
                self._recording_media_url = None
            self.state.last_command = f"video_record:{action}"
        else:
            # A definitive rejection means the camera stayed in its pre-command state.
            self.state.recording = action == "stop"
        log_event(
            "info",
            "robot.reply.recovered",
            response_key="robot_video_record",
            action=action,
            revision=revision,
            success=_response_ok(response),
        )
        if action == "start" and _response_ok(response):
            # The caller has already given up, so nothing else owns cleanup. Stop this orphaned
            # recording exactly once unless a newer user operation supersedes its revision.
            self._schedule_orphaned_recording_stop(revision)
        return True

    def _schedule_orphaned_recording_stop(self, recovered_revision: int) -> None:
        current = self._recording_compensation_task
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._compensate_orphaned_recording(recovered_revision))
        self._recording_compensation_task = task
        task.add_done_callback(self._forget_recording_compensation_task)

    def _forget_recording_compensation_task(self, task: asyncio.Task[None]) -> None:
        if self._recording_compensation_task is task:
            self._recording_compensation_task = None

    async def _compensate_orphaned_recording(self, recovered_revision: int) -> None:
        """Stop a Start that succeeded only after its caller timed out or was cancelled."""
        async with self._recording_lock:
            if (
                recovered_revision != self._recording_command_revision
                or not self.state.recording
            ):
                return
            try:
                await self._stop_recording_operation()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_event(
                    "error",
                    "robot.recording.orphan_stop_failed",
                    recovered_revision=recovered_revision,
                    error=str(exc),
                )
            else:
                log_event(
                    "info",
                    "robot.recording.orphan_stopped",
                    recovered_revision=recovered_revision,
                )

    def _apply_protocol_state(self, payload: dict[str, Any]) -> bool:
        changed = False
        map_changed = False
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

        heartbeat_payload = {
            key: deepcopy(payload[key])
            for key in ("system", "map", "naviagtion", "navigation", "task", "gimbal")
            if isinstance(payload.get(key), dict)
        }
        heartbeat_received_at = utc_now() if heartbeat_payload else None

        if system or current_map or navigation or task or gimbal:
            self.state.connected = True
            self.state.connection_status = "connected"
            self.state.error = None
            changed = True

        if system:
            if "battery" in system:
                self.state.battery = _maybe_int(system.get("battery"))
            if "status" in system:
                self.state.system_status = _maybe_str(system.get("status"))
        if current_map:
            if "name" in current_map:
                reported_map = _maybe_str(current_map.get("name"))
                if reported_map != self.state.map_name:
                    # A task/path identity belongs to one map.  Do not carry the previous map's
                    # last point into a newly confirmed map when the new heartbeat omits task.
                    map_changed = True
                    self._clear_navigation_state()
                self.state.map_name = reported_map
            if "mode" in current_map:
                self.state.map_mode = _maybe_str(current_map.get("mode"))
            if "status" in current_map:
                self.state.map_status = _normalize_failed(_maybe_str(current_map.get("status")))
        if navigation:
            if "status" in navigation:
                self.state.navigation_status = _maybe_str(navigation.get("status"))
            if "goal_status" in navigation:
                goal_status = _normalize_failed(_maybe_str(navigation.get("goal_status")))
                self.state.goal_status = goal_status
                self.state.moving = goal_status == "going"
        if task:
            if "path_file" in task:
                self.state.path_file = _maybe_str(task.get("path_file"))
            if "goal_id" in task:
                self.state.goal_id = _maybe_int(task.get("goal_id"))
            if "goal_object" in task:
                self.state.goal_object = _maybe_str(task.get("goal_object"))
            if "goal_status" in task:
                goal_status = _normalize_failed(_maybe_str(task.get("goal_status")))
                self.state.goal_status = goal_status or self.state.goal_status
                self.state.moving = goal_status == "going"
            if "object_status" in task:
                self.state.object_status = _normalize_failed(_maybe_str(task.get("object_status")))
            if "goal_status" in task:
                self._evaluate_arrival(task)
        if "goal_status" in navigation and "goal_status" not in task:
            # Some firmware splits navigation status from task identity. Navigation is a valid
            # arrival source when task did not provide its own status; task path/id, if present,
            # still protects the current goal from a delayed previous-point heartbeat.
            self._evaluate_arrival({**task, **navigation})
        if gimbal:
            # Only when the field is actually there. Absent, `.get` returns None, which is not
            # "recording" and would silently rewrite a running recording as stopped — a
            # heartbeat carrying only yaw would do it. Nothing else now decides whether to
            # send the stop command, so a missing field must mean "unchanged", not "no".
            if "record_status" in gimbal:
                self.state.recording = gimbal.get("record_status") == "recording"
                # A heartbeat can confirm an abandoned Start even when its ACK never arrives.
                # In that case the caller is already gone, so compensate just as for a late ACK.
                recovery = self._recording_reply_recovery
                if recovery is not None:
                    revision, action = recovery
                    if action == "start" and self.state.recording:
                        self._recording_reply_recovery = None
                        self._schedule_orphaned_recording_stop(revision)
                    elif action == "stop" and not self.state.recording:
                        # Only the command's resulting state is causal evidence. An idle heartbeat
                        # can precede a queued Start, and a recording heartbeat can precede a queued
                        # Stop; clearing ownership on either would let their late ACK be mistaken
                        # for a retry of the same action.
                        self._recording_reply_recovery = None
                # The physical state is only safe for finalization when no written recording
                # operation can still change it after this heartbeat.
                self._recording_status_known = self._recording_reply_recovery is None
            if "yaw" in gimbal:
                yaw = _maybe_float(gimbal.get("yaw"))
                if yaw is not None:
                    self.state.yaw = yaw
                    self._heartbeat_yaw = yaw
                    self._heartbeat_yaw_pending = True
            if "pitch" in gimbal:
                pitch = _maybe_float(gimbal.get("pitch"))
                if pitch is not None:
                    self.state.pitch = pitch
                    self._heartbeat_pitch = pitch
                    self._heartbeat_pitch_pending = True
            # Some firmware reports the two axes in one frame; some splits them. Advance only
            # once both axes have been updated since the prior complete sample or command.
            if self._heartbeat_yaw_pending and self._heartbeat_pitch_pending:
                self._heartbeat_revision = (self._heartbeat_revision or 0) + 1
                self._heartbeat_yaw_pending = False
                self._heartbeat_pitch_pending = False
            if self._heartbeat_yaw is not None:
                self.state.camera_angle = self._heartbeat_yaw

        if heartbeat_received_at is not None:
            previous = self.state.diagnostics.last_heartbeat
            yaw = previous.yaw if previous else None
            yaw_received_at = previous.yaw_received_at if previous else None
            pitch = previous.pitch if previous else None
            pitch_received_at = previous.pitch_received_at if previous else None
            gimbal_mode = previous.gimbal_mode if previous else None
            task_goal_status = previous.task_goal_status if previous else None
            task_goal_status_received_at = (
                previous.task_goal_status_received_at if previous else None
            )
            task_goal_status_identity = previous.task_goal_status_identity if previous else None
            navigation_goal_status = previous.navigation_goal_status if previous else None
            navigation_goal_status_received_at = (
                previous.navigation_goal_status_received_at if previous else None
            )
            navigation_goal_status_identity = (
                previous.navigation_goal_status_identity if previous else None
            )
            object_status = previous.object_status if previous else None
            object_status_received_at = previous.object_status_received_at if previous else None
            object_status_identity = previous.object_status_identity if previous else None
            path_file = previous.path_file if previous else None
            goal_id = previous.goal_id if previous else None
            if map_changed:
                # Keep independent gimbal samples, but never attach a previous map's task or
                # navigation identity to the first heartbeat for the newly reported map.
                task_goal_status = None
                task_goal_status_received_at = None
                task_goal_status_identity = None
                navigation_goal_status = None
                navigation_goal_status_received_at = None
                navigation_goal_status_identity = None
                object_status = None
                object_status_received_at = None
                object_status_identity = None
                path_file = None
                goal_id = None
            if "goal_status" in task:
                task_goal_status = _normalize_failed(_maybe_str(task.get("goal_status")))
                task_goal_status_received_at = heartbeat_received_at
                task_goal_status_identity = _heartbeat_goal_identity(task)
            if "goal_status" in navigation:
                navigation_goal_status = _normalize_failed(
                    _maybe_str(navigation.get("goal_status"))
                )
                navigation_goal_status_received_at = heartbeat_received_at
                task_identity = _heartbeat_goal_identity(task) or {}
                navigation_identity = _heartbeat_goal_identity(navigation) or {}
                merged_identity = {**task_identity, **navigation_identity}
                navigation_goal_status_identity = merged_identity or None
            if "object_status" in task:
                object_status = _normalize_failed(_maybe_str(task.get("object_status")))
                object_status_received_at = heartbeat_received_at
                object_status_identity = _heartbeat_goal_identity(task)
            if "path_file" in task:
                path_file = _maybe_str(task.get("path_file"))
            if "goal_id" in task:
                goal_id = _maybe_int(task.get("goal_id"))
            if gimbal:
                heartbeat_yaw = _maybe_float(gimbal.get("yaw")) if "yaw" in gimbal else None
                heartbeat_pitch = _maybe_float(gimbal.get("pitch")) if "pitch" in gimbal else None
                if heartbeat_yaw is not None:
                    yaw = heartbeat_yaw
                    yaw_received_at = heartbeat_received_at
                if heartbeat_pitch is not None:
                    pitch = heartbeat_pitch
                    pitch_received_at = heartbeat_received_at
                if "mode" in gimbal and gimbal.get("mode") is not None:
                    raw_mode = gimbal.get("mode")
                    gimbal_mode = raw_mode if isinstance(raw_mode, (int, str)) else str(raw_mode)
            self.state.diagnostics.last_heartbeat = RobotHeartbeatDiagnostic(
                sequence=(previous.sequence if previous else 0) + 1,
                received_at=heartbeat_received_at,
                yaw=yaw,
                yaw_received_at=yaw_received_at,
                pitch=pitch,
                pitch_received_at=pitch_received_at,
                gimbal_mode=gimbal_mode,
                task_goal_status=task_goal_status,
                task_goal_status_received_at=task_goal_status_received_at,
                task_goal_status_identity=task_goal_status_identity,
                navigation_goal_status=navigation_goal_status,
                navigation_goal_status_received_at=navigation_goal_status_received_at,
                navigation_goal_status_identity=navigation_goal_status_identity,
                object_status=object_status,
                object_status_received_at=object_status_received_at,
                object_status_identity=object_status_identity,
                path_file=path_file,
                goal_id=goal_id,
                payload=heartbeat_payload,
            )
            changed = True

        photo = payload.get("robot_take_photo")
        if isinstance(photo, dict):
            # Only the live request owns shared photo state. A delayed/duplicate photo reply
            # after Stop must not replace the final video's URL in the UI.
            if "robot_take_photo" in self._pending:
                self.state.media_url = _maybe_str(photo.get("url")) or self.state.media_url
            changed = True

        return changed

    def _clear_heartbeat_diagnostics(self) -> None:
        """Invalidate comparisons whenever a hardware connection is replaced."""
        self._heartbeat_yaw = None
        self._heartbeat_pitch = None
        self._heartbeat_revision = None
        self._heartbeat_yaw_pending = False
        self._heartbeat_pitch_pending = False
        self._pending_goal_attempt = None
        self.state.diagnostics.last_goal_attempt = None
        self.state.diagnostics.last_goal_command = None
        self.state.diagnostics.last_heartbeat = None
        self.state.diagnostics.last_gimbal_command = None
        # These fields are heartbeat-owned physical state.  Keeping them across a socket
        # replacement would make an old map look current before the new robot has reported.
        self.state.system_status = None
        self.state.map_name = None
        self.state.map_mode = None
        self.state.map_status = None
        self._clear_navigation_state()

    def _clear_navigation_state(self) -> None:
        self.state.moving = False
        self.state.navigation_status = None
        self.state.goal_status = None
        self.state.object_status = None
        self.state.path_file = None
        self.state.goal_id = None
        self.state.goal_object = None

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()
        self._pending_reply_matchers.clear()
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
        # A stop response and the resulting camera-file transfer are one capture boundary.
        # Serializing that boundary prevents a later start/photo from clearing or replacing the
        # shared adapter state while an earlier REC_xxxx file is still being downloaded.
        self._capture_lock = asyncio.Lock()
        # Map commands have no request id.  Keep manual and cruise switches serialized through
        # acknowledgement and (for a cruise) physical heartbeat confirmation.
        self._map_lock = asyncio.Lock()
        # A photo taken during recording replaces RobotState.media_url, so video recovery owns
        # a separate URL that survives a timed-out Stop acknowledgement.
        self._recoverable_video_url: str | None = None
        # HTTP transfers finish independently of WebSocket commands. Tokens stop a late photo
        # completion from publishing state owned by a newer Stop/start/photo operation.
        self._media_revision = 0
        self._last_shutdown_recording_confirmed_idle = True

    async def configure_websocket_url(self, websocket_url: str) -> RobotState:
        async with self._capture_lock:
            self._media_revision += 1
            current = await self.adapter.status()
            if current.recording:
                raise ValueError("录制进行中，无法更改机器人连接")
            previous_url = str(getattr(self.adapter, "websocket_url", "") or "").strip()
            result = await self.adapter.configure_websocket_url(websocket_url)
            if websocket_url.strip() != previous_url:
                self._recoverable_video_url = None
            return result

    async def configure_websocket_url_and_commit(
        self,
        websocket_url: str,
        commit: Callable[[], Any],
        *,
        preserve_capture_recovery: bool = False,
    ) -> Any:
        """Change endpoint and persist its settings as one capture-reserved transaction.

        A restored capture may be blocked only because its saved endpoint is stale.  In that
        recovery flow the operator must be able to repair the endpoint without dropping the
        video URL that still belongs to the active capture.
        """
        async with self._capture_lock:
            self._media_revision += 1
            current = await self.adapter.status()
            if current.recording:
                raise ValueError("录制进行中，无法更改机器人连接")
            previous_url = str(getattr(self.adapter, "websocket_url", "") or "")
            recovery_url = self._select_video_url(str(current.media_url or ""))
            recovery_local_path = (
                str(current.media_local_path)
                if current.media_local_path and Path(current.media_local_path).is_file()
                else None
            )
            recovery_sync_error = current.media_sync_error
            try:
                configured_state = await self.adapter.configure_websocket_url(websocket_url)
                result = commit()
                if (
                    websocket_url.strip() != previous_url.strip()
                    and not preserve_capture_recovery
                ):
                    self._recoverable_video_url = None
                elif preserve_capture_recovery:
                    self._restore_preserved_capture_state(
                        configured_state,
                        recovery_url,
                        recovery_local_path,
                        recovery_sync_error,
                    )
                    await self.events.publish(
                        "ROBOT_STATE",
                        configured_state.model_dump(mode="json"),
                    )
                return result
            except (Exception, asyncio.CancelledError):
                try:
                    rolled_back_state = await asyncio.shield(
                        self.adapter.configure_websocket_url(previous_url)
                    )
                    if preserve_capture_recovery:
                        self._restore_preserved_capture_state(
                            rolled_back_state,
                            recovery_url,
                            recovery_local_path,
                            recovery_sync_error,
                        )
                        await self.events.publish(
                            "ROBOT_STATE",
                            rolled_back_state.model_dump(mode="json"),
                        )
                except Exception as rollback_exc:
                    log_event(
                        "error",
                        "robot.configuration.rollback.failed",
                        previous_url=previous_url,
                        exception=exception_detail(rollback_exc),
                    )
                raise

    def _restore_preserved_capture_state(
        self,
        state: RobotState,
        media_url: str,
        local_path: str | None,
        sync_error: str | None,
    ) -> None:
        """Restore durable capture pointers cleared by an endpoint reconfiguration."""
        # Prefer a fresh URL already reported by the corrected endpoint; otherwise retain the
        # durable pre-repair URL. This also keeps relative camera paths useful with the new host.
        candidate = self._select_video_url(str(state.media_url or ""), prefer_state=True)
        if not candidate:
            candidate = media_url
        self._recoverable_video_url = candidate or None
        state.media_url = candidate or None
        state.media_local_path = local_path
        state.media_sync_error = sync_error

    async def status(self) -> RobotState:
        return await self.adapter.status()

    async def discard_capture_recovery(
        self,
        commit: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Verify hardware idle, durably close the session, then forget its URL atomically."""
        async with self._capture_lock:
            current = await self.adapter.status()
            if current.recording:
                raise ValueError("机器人仍在录制，不能放弃当前采集")
            known_reader = getattr(self.adapter, "recording_status_known", None)
            if callable(known_reader) and (
                not current.connected or not bool(known_reader())
            ):
                raise ConnectionError("正在等待机器人同步录制状态，请稍后再试")
            result = await commit()
            self._recoverable_video_url = None
            current.media_url = None
            current.media_local_path = None
            current.media_sync_error = None
            await self.events.publish("ROBOT_STATE", current.model_dump(mode="json"))
            return result

    def restore_recoverable_video_url(
        self,
        robot_url: str | None,
        local_path: str | None = None,
        sync_error: str | None = None,
    ) -> None:
        """Restore an unfinished capture's transfer state from durable session metadata."""
        candidate = str(robot_url or "").strip()
        if candidate and not _looks_like_image_url(candidate):
            self._recoverable_video_url = candidate
            self.adapter.state.media_url = candidate
        saved_path = Path(str(local_path or ""))
        if (
            local_path
            and saved_path.is_file()
            and not _looks_like_image_url(str(saved_path))
        ):
            self.adapter.state.media_local_path = str(saved_path)
        self.adapter.state.media_sync_error = str(sync_error or "").strip() or None

    async def connect(self) -> RobotState:
        state = await self.adapter.connect()
        await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
        return state

    async def disconnect(self) -> RobotState:
        async with self._capture_lock:
            current = await self.adapter.status()
            if current.recording:
                raise ValueError("机器人仍在录制，请先停止录制再断开连接")
            pending_reader = getattr(self.adapter, "recording_operation_pending", None)
            if callable(pending_reader) and bool(pending_reader()):
                raise ValueError("上一条录制指令仍在确认中，暂时不能断开机器人连接")
            state = await self.adapter.disconnect()
            await self.events.publish("ROBOT_STATE", state.model_dump(mode="json"))
            return state

    async def shutdown(
        self,
        on_media_url: Callable[[str], None] | None = None,
        *,
        require_idle: bool = False,
        sync_media: bool = True,
    ) -> RobotState:
        """Finalize camera ownership for application exit and always close the transport."""
        capture_lock_acquired = False
        try:
            try:
                await asyncio.wait_for(
                    self._capture_lock.acquire(),
                    timeout=_RECORDING_SHUTDOWN_TIMEOUT_SECONDS,
                )
                capture_lock_acquired = True
            except TimeoutError:
                # A capture request may itself be stuck in websocket.send(). Lifecycle shutdown
                # must still reach the adapter's independently bounded final Stop/transport close.
                log_event(
                    "error",
                    "robot.shutdown.capture_owner_stalled",
                    timeout_seconds=_RECORDING_SHUTDOWN_TIMEOUT_SECONDS,
                )
            current = await self.adapter.status()
            pending_reader = getattr(self.adapter, "recording_operation_pending", None)
            cleanup_needed = current.recording or (
                callable(pending_reader) and bool(pending_reader())
            )
            known_reader = getattr(self.adapter, "recording_status_known", None)
            cleanup_needed = cleanup_needed or (
                require_idle and callable(known_reader) and not bool(known_reader())
            )
            if cleanup_needed:
                # Shared media state may currently describe a photo taken during this recording,
                # or an older transfer. It is not evidence that the recording being stopped here
                # is already local. Only a path produced after this boundary may suppress a video
                # download.
                current.media_local_path = None
                current.media_sync_error = None
            shutdown = getattr(self.adapter, "shutdown", None)
            if callable(shutdown):
                if isinstance(self.adapter, HardwareRobotAdapter):
                    state = await shutdown(require_idle=require_idle)
                else:
                    state = await shutdown()
            else:
                # Non-hardware test/custom adapters have no ambiguous websocket ownership.
                state = await self.adapter.disconnect()

            result = state.model_copy(deep=True)
            confirmed_reader = getattr(
                self.adapter,
                "shutdown_recording_confirmed_idle",
                None,
            )
            confirmed_idle = (
                bool(confirmed_reader())
                if callable(confirmed_reader)
                else not result.recording
            )
            self._last_shutdown_recording_confirmed_idle = confirmed_idle
            if cleanup_needed:
                robot_url = self._select_video_url(str(result.media_url or ""), prefer_state=True)
                result.media_url = robot_url or None
                self._recoverable_video_url = robot_url or self._recoverable_video_url
                self._notify_media_url(on_media_url, robot_url)
                if (
                    confirmed_idle
                    and not result.recording
                    and robot_url
                    and not result.media_local_path
                    and sync_media
                ):
                    await self._sync_robot_media(result, robot_url, "video")
                elif not confirmed_idle or result.recording:
                    result.media_sync_error = (
                        result.media_sync_error
                        or result.error
                        or "关闭应用前未能确认机器人已停止录制"
                    )
                state.media_url = result.media_url
                state.media_local_path = result.media_local_path
                state.media_sync_error = result.media_sync_error
            await self.events.publish("ROBOT_STATE", result.model_dump(mode="json"))
            return result
        finally:
            if capture_lock_acquired:
                self._capture_lock.release()

    def shutdown_recording_confirmed_idle(self) -> bool:
        return self._last_shutdown_recording_confirmed_idle

    async def map_list(self) -> list[str]:
        return await self.adapter.map_list()

    async def switch_map(self, map_name: str) -> dict[str, Any]:
        async with self._map_lock:
            return await self._switch_map_unlocked(map_name)

    async def _switch_map_unlocked(self, map_name: str) -> dict[str, Any]:
        result = await self.adapter.switch_map(map_name)
        await self.events.publish("ROBOT_COMMAND", {"type": "set_switch_map", "result": result})
        return result

    async def switch_map_and_confirm(
        self,
        map_name: str,
        *,
        timeout_s: float = _MAP_SWITCH_CONFIRM_TIMEOUT_SECONDS,
    ) -> RobotState:
        """Select a map and require a later, navigation-ready heartbeat for that map.

        The scalar switch reply means only that the file exists.  A cruise must not record or
        dispatch a goal until the robot itself reports the requested map in localization mode.
        The lock also prevents a manual switch from interleaving with this confirmation window.
        """
        target = map_name.strip()
        if not target:
            raise ValueError("巡游清单未指定地图")

        async with self._map_lock:
            result = await self._switch_map_unlocked(target)
            if not isinstance(result, dict) or result.get("ok") is not True:
                raise ValueError(f"机器人拒绝切换到地图“{target}”")

            # A heartbeat received before (or together with) the ACK cannot prove the accepted
            # switch has completed.  Capture the boundary after acceptance and require a newer
            # report.  received_at survives heartbeat sequence resets on reconnect.
            acknowledged_state = await self.adapter.status()
            previous = acknowledged_state.diagnostics.last_heartbeat
            after = previous.received_at if previous is not None else None
            deadline = asyncio.get_running_loop().time() + max(0.0, timeout_s)
            last_observation = "尚未收到新的地图心跳"

            while True:
                state = await self.adapter.status()
                ready, terminal_error, observation = _confirmed_cruise_map(
                    state,
                    target,
                    after=after,
                )
                last_observation = observation or last_observation
                if terminal_error:
                    raise ValueError(terminal_error)
                if ready:
                    return state

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError(
                        f"切换地图“{target}”后未在 {timeout_s:g} 秒内确认可巡游："
                        f"{last_observation}"
                    )
                await asyncio.sleep(min(_MAP_SWITCH_CONFIRM_POLL_SECONDS, remaining))

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

    async def sweep_camera(
        self,
        target_yaw: float,
        yaw_speed: float,
        *,
        context: str = "camera_sweep",
    ) -> RobotState:
        state = await self.adapter.sweep_camera(target_yaw, yaw_speed, context=context)
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

    def recording_status_known(self) -> bool:
        reader = getattr(self.adapter, "recording_status_known", None)
        return bool(reader()) if callable(reader) else True

    def recording_operation_pending(self) -> bool:
        reader = getattr(self.adapter, "recording_operation_pending", None)
        return bool(reader()) if callable(reader) else False

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

    async def set_gimbal(
        self,
        command: GimbalMoveRequest,
        *,
        context: str = "manual",
    ) -> RobotState:
        state = await self.adapter.set_gimbal(command, context=context)
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
        async with self._capture_lock:
            self._media_revision += 1
            current = await self.adapter.status()
            if current.recording:
                raise ValueError("机器人已在录制，请先停止当前录制")
            self._recoverable_video_url = None
            await self._clear_media_state()
            state = await self.adapter.start_recording()
            result = state.model_copy(deep=True)
            self._recoverable_video_url = str(result.media_url or "").strip() or None
            log_event("info", "capture.recording.started", recording=result.recording)
            await self.events.publish("ROBOT_STATE", result.model_dump(mode="json"))
            return result

    async def stop_recording(self, sync_media: bool = True) -> RobotState:
        async with self._capture_lock:
            self._media_revision += 1
            return await self._stop_recording_locked(sync_media)

    async def finalize_capture_recording(
        self,
        on_media_url: Callable[[str], None] | None = None,
    ) -> RobotState:
        """Stop an active recording, or retry saving a late already-stopped recording.

        Some firmware applies Stop even when its WebSocket reply reaches the desktop too late.
        The next heartbeat then says ``recording=false`` and carries the media URL. A capture
        session must be able to consume that URL without sending a second invalid Stop command.
        """
        async with self._capture_lock:
            self._media_revision += 1
            current = await self.adapter.status()
            result = current.model_copy(deep=True)
            # A verified local file was downloaded only after Stop was accepted. It is safe to
            # finalize even before the first post-restart heartbeat.
            if result.media_local_path and Path(result.media_local_path).is_file():
                return result
            if current.recording:
                return await self._stop_recording_locked(
                    sync_media=True,
                    on_media_url=on_media_url,
                )

            known_reader = getattr(self.adapter, "recording_status_known", None)
            if callable(known_reader) and (
                not current.connected or not bool(known_reader())
            ):
                raise ConnectionError("正在等待机器人同步录制状态，请稍后重试保存")

            robot_url = self._select_video_url(str(result.media_url or ""))
            if not robot_url:
                raise ValueError("机器人已停止录制，但没有可恢复的视频地址")

            result.media_url = robot_url
            self._recoverable_video_url = robot_url
            self._notify_media_url(on_media_url, robot_url)
            await self._sync_robot_media(result, robot_url, "video")
            live_result = await self._mirror_media_result(result, recording=False)
            await self.events.publish("ROBOT_STATE", live_result.model_dump(mode="json"))
            return live_result

    async def _stop_recording_locked(
        self,
        sync_media: bool,
        on_media_url: Callable[[str], None] | None = None,
    ) -> RobotState:
        # The shared adapter state may still contain a prior video or an in-recording photo.
        # Clear local outcomes before stop so only values deliberately produced by this stop can
        # survive. Keep media_url: some firmware returns it at start and omits it at stop.
        before_stop = await self.adapter.status()
        if not before_stop.recording:
            raise ValueError("机器人当前未在录制")
        before_stop.media_local_path = None
        before_stop.media_sync_error = None
        state = await self.adapter.stop_recording()
        result = state.model_copy(deep=True)
        robot_url = self._select_video_url(str(result.media_url or ""), prefer_state=True)
        result.media_url = robot_url or None
        self._recoverable_video_url = robot_url or self._recoverable_video_url
        self._notify_media_url(on_media_url, robot_url)
        # Preserve a fresh local path supplied by adapters that perform their own transfer as
        # part of stop_recording; otherwise the desktop media service owns the transfer.
        result.media_sync_error = None
        if sync_media and not result.media_local_path:
            # Recording and transferring the resulting file are separate outcomes. Once the
            # robot accepted stop, a network/routing failure must not rewrite that success as
            # "recording failed"; callers can keep the capture session for a save retry.
            await self._sync_robot_media(result, robot_url, "video")
        live_result = await self._mirror_media_result(result, recording=result.recording)
        log_event(
            "info",
            "capture.recording.stopped",
            robot_url=robot_url,
            local_path=result.media_local_path,
            media_sync_error=result.media_sync_error,
        )
        await self.events.publish("ROBOT_STATE", live_result.model_dump(mode="json"))
        return live_result

    @staticmethod
    def _notify_media_url(
        callback: Callable[[str], None] | None,
        robot_url: str,
    ) -> None:
        """Persist an accepted Stop URL before the potentially long HTTP transfer.

        A storage error must not prevent the already-running camera from stopping or skip the
        transfer. Callers persist the complete transfer result again after this method returns,
        so a transient storage failure still gets a second durable-write opportunity.
        """
        if callback is None or not robot_url:
            return
        try:
            callback(robot_url)
        except Exception as exc:
            log_event(
                "error",
                "capture.media_url.persist.failed",
                robot_url=robot_url,
                exception=exception_detail(exc),
            )

    def _select_video_url(self, state_url: str = "", *, prefer_state: bool = False) -> str:
        reader = getattr(self.adapter, "pending_recording_media_url", None)
        adapter_url = str(reader() or "").strip() if callable(reader) else ""
        state_url = state_url.strip()
        candidates = (
            (state_url, adapter_url, self._recoverable_video_url or "")
            if prefer_state
            else (adapter_url, self._recoverable_video_url or "", state_url)
        )
        for candidate in candidates:
            if candidate and not _looks_like_image_url(candidate):
                return candidate
        return ""

    async def stop_recording_with_download(
        self,
        downloader: Callable[[str], Awaitable[Any]],
    ) -> tuple[RobotState, Any]:
        """Stop and consume disposable media without exposing it as a library item.

        Framing previews use this path so their temporary HTTP transfer participates in the
        same capture lock as ordinary recording sync, while remaining outside MediaService.
        """
        async with self._capture_lock:
            self._media_revision += 1
            state = await self._stop_recording_locked(sync_media=False)
            robot_url = str(state.media_url or "")
            if not robot_url:
                raise RuntimeError("机器人没有返回测试视频地址")
            return state, await downloader(self.resolve_media_url(robot_url))

    def resolve_media_url(self, robot_url: str) -> str:
        from automated_video_editing_backend.services.media import resolve_robot_media_url

        return resolve_robot_media_url(
            robot_url,
            str(getattr(self.adapter, "websocket_url", "") or ""),
        )

    async def capture_photo(self) -> dict[str, Any]:
        # Serialize the physical command and take a private state snapshot, then release the
        # capture lock before the optional HTTP transfer. A stalled photo server must never
        # delay the operator's Stop command while the robot keeps recording.
        async with self._capture_lock:
            self._media_revision += 1
            media_revision = self._media_revision
            await self._clear_media_state()
            result = await self.adapter.capture_photo()
            robot_url = str(result.get("url") or "")
            state = (await self.adapter.status()).model_copy(deep=True)

        synced = await self._sync_robot_media(state, robot_url, "image")
        async with self._capture_lock:
            live_state = None
            if media_revision == self._media_revision:
                live = await self.adapter.status()
                live.media_local_path = state.media_local_path
                live.media_sync_error = state.media_sync_error
                live_state = live.model_copy(deep=True)
                await self.events.publish("ROBOT_STATE", live_state.model_dump(mode="json"))
        if synced:
            result["local_media_item"] = synced.model_dump(mode="json")
        result["media_sync_error"] = state.media_sync_error
        log_event(
            "info",
            "capture.photo.completed",
            robot_url=robot_url,
            local_path=synced.path if synced else None,
            media_sync_error=state.media_sync_error,
        )
        await self.events.publish("ROBOT_PHOTO", result)
        return result

    async def _mirror_media_result(
        self,
        result: RobotState,
        *,
        recording: bool | None = None,
    ) -> RobotState:
        """Copy only this operation's owned fields back to the adapter's live state."""
        live = await self.adapter.status()
        if recording is not None:
            live.recording = recording
        live.media_url = result.media_url
        live.media_local_path = result.media_local_path
        live.media_sync_error = result.media_sync_error
        return live.model_copy(deep=True)

    async def _sync_robot_media(
        self,
        state: RobotState,
        robot_url: str | None,
        kind_hint: str,
    ) -> MediaItem | None:
        state.media_sync_error = None
        if not self.media:
            error = "媒体服务不可用，无法保存机器人文件"
            await self._media_sync_failed(state, robot_url or "", kind_hint, error)
            return None

        try:
            download_url = self.resolve_media_url(str(robot_url or ""))
            log_event(
                "info",
                "robot.media.download.started",
                robot_url=robot_url,
                resolved_url=download_url,
                kind=kind_hint,
            )
            async def download() -> MediaItem:
                return await self.media.download_url(
                    download_url,
                    metadata={
                        "source": "data/downloads",
                        "origin": "robot_hardware",
                        "robot_url": download_url,
                        "kind_hint": kind_hint,
                    },
                    filename_prefix="robot-",
                    request_timeout=httpx.Timeout(60.0, connect=3.0, pool=3.0),
                )

            def log_retry(
                attempt: int,
                total_attempts: int,
                delay: float,
                exc: BaseException,
            ) -> None:
                log_event(
                    "warning",
                    "robot.media.download.retry",
                    robot_url=download_url,
                    kind=kind_hint,
                    failed_attempt=attempt,
                    total_attempts=total_attempts,
                    retry_in_seconds=delay,
                    exception=exception_detail(exc),
                )

            item = await retry_camera_media_download(download, on_retry=log_retry)
        except Exception as exc:
            error = camera_media_error_message(exc)
            await self._media_sync_failed(
                state,
                robot_url or "",
                kind_hint,
                error,
                exception_detail(exc),
            )
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
        exception: str | None = None,
    ) -> None:
        state.media_sync_error = error
        log_event(
            "error",
            "robot.media.download.failed",
            robot_url=robot_url,
            kind=kind_hint,
            error=error,
            exception=exception,
        )
        await self.events.publish(
            "ROBOT_MEDIA_SYNC_FAILED",
            {"url": robot_url, "kind_hint": kind_hint, "error": error},
        )


def _load_websockets() -> Any:
    import websockets

    return websockets


def _looks_like_image_url(url: str) -> bool:
    path = urlsplit(url).path.casefold()
    return path.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"))


async def _wait_for_reply(future: asyncio.Future[Any], timeout_s: float) -> Any:
    """Wait for our deadline without relabelling a TimeoutError delivered by the socket.

    ``asyncio.wait_for`` raises the same exception type both when its own timer expires and when
    the awaited future has already completed with ``TimeoutError``. Inspecting the shielded
    future at the boundary preserves the latter as the real transport failure.
    """
    try:
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout_s)
    except TimeoutError as exc:
        if future.done():
            return future.result()
        raise _ReplyDeadlineExpired from exc


def _video_record_action(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    has_start = "start" in value
    has_stop = "stop" in value
    if has_start == has_stop:
        # Both and neither are ambiguous. The documented protocol makes these fields mutually
        # exclusive, and the real robot log echoes ``start`` on Start acknowledgements.
        return None
    return "start" if has_start else "stop"


def _reply_matcher(
    request_payload: dict[str, Any],
    response_key: str,
) -> Callable[[Any], bool] | None:
    """Match replies for commands that share one protocol response key."""
    if response_key != "robot_video_record":
        return None
    command = request_payload.get("video_record")
    if not isinstance(command, dict):
        return None
    action = _video_record_action(command)
    if action is None:
        return None
    expected_value = command[action]
    return lambda response: (
        _video_record_action(response) == action
        and response.get(action) == expected_value
    )


def _response_error(response: Any) -> str:
    if isinstance(response, dict):
        return str(
            response.get("error")
            or response.get("message")
            or response.get("status")
            or response
        )
    return str(response)


def _response_ok(response: Any) -> bool:
    """Accept harmless firmware casing/whitespace while still requiring explicit success."""
    return (
        isinstance(response, dict)
        and str(response.get("status") or "").strip().casefold() == "ok"
    )


def _confirmed_cruise_map(
    state: RobotState,
    target: str,
    *,
    after: datetime | None,
) -> tuple[bool, str | None, str]:
    """Interpret only a post-ACK heartbeat as physical map readiness."""
    if not state.connected:
        return False, None, "机器人连接已断开"
    heartbeat = state.diagnostics.last_heartbeat
    if heartbeat is None or (after is not None and heartbeat.received_at <= after):
        return False, None, "尚未收到新的地图心跳"

    payload = heartbeat.payload
    current_map = payload.get("map") if isinstance(payload.get("map"), dict) else None
    system = payload.get("system") if isinstance(payload.get("system"), dict) else None
    navigation = (
        payload.get("naviagtion")
        if isinstance(payload.get("naviagtion"), dict)
        else payload.get("navigation")
        if isinstance(payload.get("navigation"), dict)
        else None
    )
    if current_map is None:
        return False, None, "新心跳没有地图状态"

    actual = str(current_map.get("name") or "").strip()
    if actual != target:
        return False, None, f"机器人仍报告地图“{actual or '未命名'}”"

    system_status = str((system or {}).get("status") or "").strip().casefold()
    map_mode = str(current_map.get("mode") or "").strip().casefold()
    map_status = _normalize_failed(
        str(current_map.get("status") or "").strip().casefold() or None
    )
    navigation_status = str((navigation or {}).get("status") or "").strip().casefold()

    if system_status and system_status != "ready":
        return (
            False,
            f"地图已切换到“{target}”，但机器人硬件状态为 {system_status}",
            f"硬件状态 {system_status}",
        )
    if map_status == "failed":
        return (
            False,
            f"地图已切换到“{target}”，但机器人定位失败",
            "定位失败",
        )

    missing = []
    if system_status != "ready":
        missing.append("硬件未就绪")
    if map_mode != "localization":
        missing.append(f"地图模式 {map_mode or '未上报'}")
    if map_status != "ready":
        missing.append(f"定位状态 {map_status or '未上报'}")
    if navigation_status != "ready":
        missing.append(f"导航状态 {navigation_status or '未上报'}")
    if missing:
        return False, None, "、".join(missing)
    return True, None, f"地图“{target}”已定位并可导航"


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
        return value.strip().casefold() == "true"
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


def _heartbeat_goal_identity(block: dict[str, Any]) -> dict[str, Any] | None:
    """Keep the identity beside the status that supplied it.

    The next heartbeat may contain only gimbal data.  Keeping identity only in the latest
    raw frame would then make an old point's retained ``done``/``object_status`` look as if it
    belonged to the new point.  An absent field stays absent: status-only firmware frames are
    still usable, but the UI will describe them as time-correlated rather than identity-proven.
    """
    identity: dict[str, Any] = {}
    path_key = (
        "path_file" if "path_file" in block else "path_name" if "path_name" in block else None
    )
    if path_key is not None:
        path = _maybe_str(block.get(path_key))
        if path is not None:
            identity["path_file"] = path
    if "goal_id" in block:
        goal_id = _maybe_int(block.get("goal_id"))
        if goal_id is not None:
            identity["goal_id"] = goal_id
    if "goal_object" in block:
        goal_object = _maybe_str(block.get("goal_object"))
        identity["goal_object"] = goal_object or None
    return identity or None


def _goal_identity_conflicts(block: dict[str, Any], command: RobotGoalCommand) -> bool:
    """Reject a response/status only when an identity field explicitly contradicts the goal."""
    path_key = (
        "path_file" if "path_file" in block else "path_name" if "path_name" in block else None
    )
    if path_key is not None:
        reported_path = _maybe_str(block.get(path_key))
        if reported_path is not None and reported_path != command.path_name:
            return True
    if "goal_id" in block:
        reported_goal_id = _maybe_int(block.get("goal_id"))
        if reported_goal_id is None or reported_goal_id != command.goal_id:
            return True
    expected_object = _maybe_str(command.goal_object) or None
    if expected_object is not None and "goal_object" in block:
        reported_object = _maybe_str(block.get("goal_object")) or None
        if reported_object != expected_object:
            return True
    return False


def _goal_identity_is_explicit_match(
    block: dict[str, Any], command: RobotGoalCommand
) -> bool:
    """A path and point pair is strong enough to accept a direct terminal heartbeat."""
    path_key = (
        "path_file" if "path_file" in block else "path_name" if "path_name" in block else None
    )
    if path_key is None or "goal_id" not in block:
        return False
    return (
        _maybe_str(block.get(path_key)) == command.path_name
        and _maybe_int(block.get("goal_id")) == command.goal_id
        and not _goal_identity_conflicts(block, command)
    )


def _normalize_failed(value: str | None) -> str | None:
    return "failed" if value == "faild" else value
