from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from automated_video_editing_backend.core.diagnostics import log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraworkConfig,
    CruiseRequest,
    CruiseRoute,
    CruiseRouteIssue,
    CruiseRouteValidation,
    CruiseRun,
    CruiseSegment,
    GimbalMoveRequest,
    RobotGoalCommand,
    utc_now,
)
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.robot import RobotCommandNotSentError, RobotService

from automated_video_editing_backend.core.gimbal_limits import (
    POSE_STABLE_SAMPLES as _CW_POSE_STABLE_SAMPLES,
    POSE_SETTLED_DELTA_DEG as _CW_POSE_SETTLED_DELTA_DEG,
    ZOOM_SETTLED_DELTA as _CW_ZOOM_SETTLED_DELTA,
)
from automated_video_editing_backend.services.camera_program import camera_program

_CW_POSE_POLL_SECONDS = 0.2
_CW_OUT_OF_TOLERANCE_GRACE_SECONDS = 3.0
_CW_OUT_OF_TOLERANCE_MAX_CHECKS = 3
# This is only a fail-safe for missing or endlessly changing telemetry. It is not
# added to the robot's estimated movement time and cannot make a move complete.
_CW_MONITOR_FAILSAFE_SECONDS = 60.0
_ARRIVAL_TIMEOUT_SECONDS = 60.0


class CruisePreflightError(ValueError):
    """A non-driving route check that must block every cruise start surface."""

    def __init__(self, validation: CruiseRouteValidation) -> None:
        self.validation = validation
        message = next(
            (issue.message for issue in validation.issues),
            "巡游地图与路径未通过启动检查",
        )
        super().__init__(message)


class CruiseService:
    """Drives the robot through an ordered list of points while a single recording runs.

    Deliberately narrower than a live-tour engine: no knowledge base, no narration, no
    repeat count. Every point is dispatched navigation-only, so legacy goal_object data can
    never make shooting wait for object recognition or hand camera ownership to the robot.
    """

    def __init__(
        self,
        events: EventHub,
        robot: RobotService,
        capture: CaptureService,
        camerawork_config_provider: Callable[[], CameraworkConfig] | None = None,
    ) -> None:
        self.events = events
        self.robot = robot
        self.capture = capture
        self._run: CruiseRun | None = None
        self._task: asyncio.Task[None] | None = None
        self._start_lock = asyncio.Lock()
        self._starting = False
        self._cancel = asyncio.Event()
        self._origin_monotonic = 0.0
        self._capture_visit: CruiseSegment | None = None
        self._camerawork_config_provider = camerawork_config_provider or CameraworkConfig
        # Zoom uses the last command; yaw/pitch always come from physical heartbeat samples.
        self._cw_zoom = 1.0
        self._point_dwell_baseline_seconds = 7.5
        self._telemetry: list[tuple[float, float, float]] = []
        self._telemetry_task: asyncio.Task[None] | None = None

    def current(self) -> CruiseRun | None:
        return self._run

    @property
    def is_running(self) -> bool:
        return self._starting or (self._task is not None and not self._task.done())

    async def start(self, request: CruiseRequest) -> CruiseRun:
        run, _validation = await self._start(request)
        return run

    async def start_route(
        self,
        route: CruiseRoute,
    ) -> tuple[CruiseRun, CruiseRouteValidation]:
        """Start a saved route and return the exact preflight result used for that start."""
        return await self._start(route.request, route_id=route.id)

    async def _start(
        self,
        request: CruiseRequest,
        *,
        route_id: str | None = None,
    ) -> tuple[CruiseRun, CruiseRouteValidation]:
        # Serialize the complete check -> switch -> run-publication boundary.  Otherwise two
        # callers can both see an idle service, interleave map switches, and start together.
        async with self._start_lock:
            if self._task is not None and not self._task.done():
                raise ValueError("A cruise is already running")
            self._starting = True
            self._cancel = asyncio.Event()
            try:
                # Recording and capture sessions are single-resource. Without this the cruise
                # would adopt a manual session and close a recording the operator opened.
                if self.capture.active_session() is not None:
                    raise ValueError(
                        "A manual capture is running; stop it before starting a cruise"
                    )

                camerawork = self._camerawork_config_provider() if request.auto_camerawork else None
                if camerawork is not None and not camerawork.configured:
                    raise ValueError(
                        "Automatic camerawork is not configured; save it in 镜头设置 first"
                    )

                if camerawork is not None:
                    for point in request.points:
                        camera_program(
                            camerawork,
                            point.piece_ids
                            if point.piece_ids is not None
                            else camerawork.piece_ids,
                        )
                validation = await self._preflight_request(request, route_id=route_id)
                if self._cancel.is_set():
                    raise ValueError("Cruise start was canceled")

                # A manual capture can be opened while map verification is waiting on the
                # robot. Recheck before publishing a run or opening our own capture session.
                if self.capture.active_session() is not None:
                    raise ValueError(
                        "A manual capture is running; stop it before starting a cruise"
                    )

                # Product invariant: cruise is navigation-only. ``goal_object`` remains in
                # stored/API data for compatibility but can never restore object alignment.
                run = CruiseRun(
                    title=request.title,
                    map_name=request.map_name,
                    recording=request.record,
                    segments=[
                        CruiseSegment(
                            index=index,
                            path_name=point.path_name,
                            goal_id=point.goal_id,
                            goal_object=None,
                        )
                        for index, point in enumerate(request.points)
                    ],
                )
                self._run = run
                self._task = asyncio.create_task(self._execute(request, run, camerawork))
                log_event(
                    "info",
                    "cruise.run.started",
                    run_id=run.id,
                    map_name=run.map_name,
                    point_count=len(run.segments),
                    recording=request.record,
                    auto_camerawork=request.auto_camerawork,
                )
                await self.events.publish("CRUISE_STARTED", run.model_dump(mode="json"))
                return run, validation
            finally:
                self._starting = False

    async def cancel(self) -> CruiseRun | None:
        if not self.is_running:
            return self._run
        self._cancel.set()
        if self._starting:
            # Let the serialized preflight observe cancellation before looking for its task.
            async with self._start_lock:
                pass
        task = self._task
        if task is not None:
            with suppress(asyncio.CancelledError):
                await task
        return self._run

    async def validate_route(self, route: CruiseRoute) -> CruiseRouteValidation:
        """Check the parts of a saved route the robot can confirm without moving.

        map_name and path_name are enumerable, so both are verified up front. goal_id is
        not: the only way to test an id is set_goal, which drives the robot whenever the id
        is valid, and the protocol has no way to abort a movement once accepted. Ids are
        therefore checked inline at dispatch, where a rejected one costs no movement.
        """
        return await self._validate_request(route.request, route_id=route.id)

    async def _preflight_request(
        self,
        request: CruiseRequest,
        *,
        route_id: str | None,
    ) -> CruiseRouteValidation:
        """Fail closed, activate the request's map, then verify it is safe to drive.

        Validation deliberately runs before switching so a missing map/path can never move the
        robot.  Drive-readiness runs after switching because a bad *old* map must not prevent a
        valid target map from repairing localization.
        """
        validation = await self._validate_request(request, route_id=route_id)
        if not validation.checked or validation.issues:
            # Manual "校验" may report uncertainty as a warning.  Starting is different: an
            # unverified map/path must be a launch-blocking error, and the structured response
            # should say ``ok: false`` rather than contradicting that decision.
            blocked = validation.model_copy(deep=True)
            for issue in blocked.issues:
                issue.level = "error"
            raise CruisePreflightError(blocked)

        # _validate_request makes this operational requirement explicit while the Pydantic
        # field stays Optional only so old files can still be loaded and repaired in the UI.
        target_map = str(request.map_name or "").strip()
        await self.robot.switch_map_and_confirm(target_map)
        self.robot.refuse_if_unfit_to_drive()
        return validation

    async def _validate_request(
        self,
        request: CruiseRequest,
        *,
        route_id: str | None = None,
    ) -> CruiseRouteValidation:
        issues: list[CruiseRouteIssue] = []

        map_name = str(request.map_name or "").strip()
        if not map_name:
            return CruiseRouteValidation(
                route_id=route_id,
                issues=[
                    CruiseRouteIssue(
                        level="error",
                        field="map_name",
                        message="巡游清单未指定地图，请先选择本次巡游地图",
                    )
                ],
            )

        try:
            maps = await self.robot.map_list()
        except Exception as exc:
            return CruiseRouteValidation(
                route_id=route_id,
                checked=False,
                issues=[
                    CruiseRouteIssue(
                        level="warning",
                        field="robot",
                        message=f"Robot unreachable, map and path were not verified: {exc}",
                    )
                ],
            )

        if not maps:
            issues.append(
                CruiseRouteIssue(
                    level="warning",
                    field="map_name",
                    value=map_name,
                    message="Robot returned an empty map list, so nothing was verified",
                )
            )
            return CruiseRouteValidation(route_id=route_id, issues=issues)

        if map_name not in maps:
            issues.append(
                CruiseRouteIssue(
                    level="error",
                    field="map_name",
                    value=map_name,
                    message=f"Robot has no map named '{map_name}'",
                )
            )
            return CruiseRouteValidation(route_id=route_id, issues=issues)

        try:
            paths = await self.robot.path_list(map_name)
        except Exception as exc:
            issues.append(
                CruiseRouteIssue(
                    level="warning",
                    field="path_name",
                    message=f"Path list unavailable, paths were not verified: {exc}",
                )
            )
            return CruiseRouteValidation(route_id=route_id, checked=False, issues=issues)

        points_by_path: dict[str, list[int]] = {}
        for index, point in enumerate(request.points):
            points_by_path.setdefault(point.path_name, []).append(index)

        if not paths:
            issues.append(
                CruiseRouteIssue(
                    level="warning",
                    field="path_name",
                    value=map_name,
                    message=(
                        f"Robot returned no paths for map '{map_name}', so paths were not verified"
                    ),
                )
            )
            return CruiseRouteValidation(route_id=route_id, issues=issues)

        known = set(paths)
        for path_name, indexes in points_by_path.items():
            if path_name not in known:
                issues.append(
                    CruiseRouteIssue(
                        level="error",
                        field="path_name",
                        value=path_name,
                        message=f"Robot has no path '{path_name}' on map '{map_name}'",
                        point_indexes=indexes,
                    )
                )

        return CruiseRouteValidation(route_id=route_id, issues=issues)

    async def _execute(
        self,
        request: CruiseRequest,
        run: CruiseRun,
        camerawork: CameraworkConfig | None,
    ) -> None:
        recording_start_rejected = False
        recording_start_attempted = False
        # Never let motion samples from an earlier run leak into a later ordinary cruise.
        self._telemetry = []
        try:
            # Establish the operator's intended resting composition before footage or base
            # movement begins. Prefer physical zoom feedback; retain a starting default
            # only for old adapters until a zoom operation requires real feedback.
            if camerawork is not None:
                self._cw_zoom = 1.0
                await self._move_to_pose((0, 0), camerawork)

            session = await self.capture.start(request.title)
            run.capture_session_id = session.id
            self._origin_monotonic = time.monotonic()
            self._capture_visit = None
            observer = getattr(self.robot, "set_capture_observer", None)
            if callable(observer):
                observer(lambda event: self._observe_recording(run, session, event))

            if request.record:
                try:
                    recording_start_attempted = True
                    recording_state = await self.robot.start_recording()
                except (RobotCommandNotSentError, ValueError):
                    # An explicit protocol refusal is definitive. Connection/time-out errors
                    # remain ambiguous because the robot may have applied Start before loss.
                    recording_start_rejected = True
                    raise
                self.capture.remember_pending_media(session, recording_state.media_url)
                try:
                    self.capture.remember_recording_clock(
                        session,
                        confirmation_delay_seconds=self._elapsed(),
                        quality="application_estimate",
                    )
                except OSError:
                    run.warnings.append("录制对时信息暂存失败；完整视频仍会保存")
            if request.auto_camerawork and request.record:
                self._telemetry_task = asyncio.create_task(self._sample_telemetry())

            canceled = False
            for segment in run.segments:
                if self._cancel.is_set():
                    canceled = True
                    break
                await self._run_segment(request, run, segment, camerawork)
            if canceled or self._cancel.is_set():
                run.status = "canceled"
            elif not any(segment.status == "arrived" for segment in run.segments):
                run.status = "failed"
                run.error = "巡游未到达任何点位"
            else:
                # Individual points may still fail and remain visible on their segments; a run
                # that reached at least one requested point retains the existing partial-success
                # behavior instead of turning one bad id into a total loss.
                run.status = "succeeded"
        except asyncio.CancelledError:
            run.status = "canceled"
            raise
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
        finally:
            camerawork_quiesced = True
            await self._finish(
                request,
                run,
                camerawork,
                recording_start_rejected,
                recording_start_attempted,
                camerawork_quiesced,
            )
            observer = getattr(self.robot, "set_capture_observer", None)
            if callable(observer):
                observer(None)

    async def _run_segment(
        self,
        request: CruiseRequest,
        run: CruiseRun,
        segment: CruiseSegment,
        camerawork: CameraworkConfig | None,
    ) -> None:
        self._capture_visit = segment
        if self._cancel.is_set():
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return

        # The previous point's entire program has completed before this method can run.
        # The protocol has no stop-motion command; never dispatch after cancellation.
        segment.status = "navigating"
        segment.transit_start_seconds = self._elapsed()
        log_event(
            "info",
            "cruise.point.dispatched",
            run_id=run.id,
            point_index=segment.index,
            path_name=segment.path_name,
            goal_id=segment.goal_id,
        )
        await self._publish_segment("CRUISE_POINT_DISPATCHED", run, segment)
        try:
            result = await self.robot.set_goal(
                RobotGoalCommand(
                    path_name=segment.path_name,
                    goal_id=segment.goal_id,
                    goal_object=None,
                )
            )
        except ConnectionError:
            self._record_capture_transition(run, "navigation_uncertain")
            # The robot is gone; failing every remaining point one by one would be noise.
            raise
        except Exception as exc:
            await self._fail_segment(run, segment, str(exc))
            return

        if not _goal_accepted(result):
            self._record_capture_transition(run, "goal_rejected")
            await self._fail_segment(run, segment, "Robot rejected the goal (goal_check false)")
            return

        self._record_capture_transition(run, "goal_accepted")

        log_event(
            "info",
            "cruise.point.arrival_wait.started",
            run_id=run.id,
            point_index=segment.index,
            timeout_seconds=_ARRIVAL_TIMEOUT_SECONDS,
        )
        arrival = await self._await_arrival(_ARRIVAL_TIMEOUT_SECONDS)
        log_event(
            "info",
            "cruise.point.arrival_wait.finished",
            run_id=run.id,
            point_index=segment.index,
            result=arrival,
        )
        if arrival == "canceled":
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return
        if arrival != "done":
            await self._fail_segment(run, segment, f"Robot reported '{arrival}' for this point")
            return

        segment.status = "arrived"
        segment.arrived_at_seconds = (
            segment.arrived_at_seconds
            if segment.arrived_at_seconds is not None
            else self._elapsed()
        )
        self._record_capture_transition(run, "goal_done")
        marker = await self.capture.add_marker(
            segment.arrived_at_seconds,
            f"{segment.path_name}#{segment.goal_id}",
        )
        if marker is not None:
            run.markers.append(marker)
        await self._publish_segment("CRUISE_POINT_ARRIVED", run, segment)

        if camerawork is not None:
            selected = request.points[segment.index].piece_ids
            try:
                await self._run_camera_program(run, segment, camerawork, selected)
            except Exception as exc:
                segment.error = str(exc)
                await self._publish_segment("CRUISE_SHOT_FAILED", run, segment)
                raise
        elif request.record:
            await self._sleep_or_cancel(self._point_dwell_baseline_seconds)

        segment.departed_at_seconds = self._elapsed()
        segment.dwell_seconds = segment.departed_at_seconds - segment.arrived_at_seconds
        await self._publish_segment("CRUISE_POINT_DEPARTED", run, segment)

    async def _await_arrival(self, timeout_s: float) -> str:
        """Wait for the robot to settle. Returns 'done', 'failed', 'timeout' or 'canceled'."""
        arrival = asyncio.create_task(self.robot.wait_for_arrival(timeout_s))
        canceled = asyncio.create_task(self._cancel.wait())
        try:
            done, _ = await asyncio.wait(
                {arrival, canceled},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if arrival not in done:
                return "canceled"
            try:
                return arrival.result()
            except TimeoutError:
                return "timeout"
        finally:
            for task in (arrival, canceled):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    def _heartbeat_pitch(self) -> float | None:
        reader = getattr(self.robot, "heartbeat_pitch", None)
        return reader() if callable(reader) else None

    def _heartbeat_revision(self) -> int | None:
        reader = getattr(self.robot, "heartbeat_revision", None)
        return reader() if callable(reader) else None

    def _heartbeat_zoom(self) -> float | None:
        reader = getattr(self.robot, "heartbeat_zoom", None)
        return reader() if callable(reader) else None

    def _zoom_revision(self) -> int:
        reader = getattr(self.robot, "heartbeat_zoom_revision", None)
        return reader() if callable(reader) else 0

    async def _move_to_pose(
        self, target: tuple[float, float], config: CameraworkConfig,
        *, target_zoom: float | None = None,
    ) -> None:
        """Send one leg only after readiness; a busy rejection is resent at most three times."""
        desired_zoom = config.anchor_zoom if target_zoom is None else target_zoom
        await self._wait_gimbal_settled()
        for attempt in range(1, 5):
            if self._cancel.is_set():
                raise asyncio.CancelledError
            yaw, pitch = self.robot.heartbeat_yaw(), self._heartbeat_pitch()
            if yaw is None or pitch is None:
                raise ValueError("缺少云台角度反馈，已停止后续导航；请检查机器人心跳")
            zoom = self._heartbeat_zoom()
            if zoom is None:
                raise ValueError("缺少变焦倍率反馈，已停止后续镜头和导航；请检查机器人心跳 zoom")
            start_zoom = zoom
            speed = config.speed_max
            # Capture the feedback boundary before the write. An immediate robot reply can
            # be consumed before send_cruise_gimbal() resumes; requiring three newer complete
            # samples still prevents cached pre-command state from confirming this leg.
            revision = self._heartbeat_revision()
            zoom_revision = self._zoom_revision()
            command = GimbalMoveRequest(
                yaw_start=yaw, yaw_end=target[0], yaw_speed=speed,
                pitch_start=pitch, pitch_end=target[1], pitch_speed=speed,
                zoom_start=start_zoom, zoom_end=desired_zoom,
            )
            context = (
                "cruise_fixed_zoom"
                if target_zoom is not None or abs(start_zoom - desired_zoom) > config.zoom_tolerance
                else "cruise_fixed_piece"
            )
            sender = getattr(self.robot, "send_cruise_gimbal", None)
            owner = None
            if sender is None:
                await asyncio.wait_for(self.robot.set_gimbal(command, context=context), timeout=10)
            else:
                owner = await asyncio.wait_for(
                    sender(command, context=context, cancelled=self._cancel.is_set), timeout=10,
                )
            angle_estimated_seconds = max(
                abs(target[0] - yaw) / speed,
                abs(target[1] - pitch) / speed,
            )
            reached, _ = await self._await_camerawork_pose(
                *target,
                angle_estimated_seconds,
                after_revision=revision,
                target_zoom=desired_zoom,
                after_zoom_revision=zoom_revision,
                angle_tolerance=config.angle_tolerance_degrees,
                zoom_tolerance=config.zoom_tolerance,
                busy_owner=owner,
            )
            if self._cancel.is_set():
                raise asyncio.CancelledError
            if reached:
                self._cw_zoom = desired_zoom
                return
            busy = getattr(self.robot, "gimbal_busy_seen", None)
            if owner is None or not callable(busy) or not busy(owner):
                raise ValueError(
                    f"云台未确认到达 ({target[0]}, {target[1]})，倍率 {desired_zoom:g}×"
                    f"（已发送 {attempt} 次）；已停止后续镜头和导航"
                )
            log_event("warning", "cruise.gimbal.busy_ignored", attempt=attempt,
                      target_yaw=target[0], target_pitch=target[1], target_zoom=desired_zoom)
        raise ValueError("机器人连续 4 次拒绝云台指令（含 3 次补发）；已停止后续镜头和导航")

    async def _wait_gimbal_settled(self) -> None:
        reader = getattr(self.robot, "gimbal_stable_now", None)
        if not callable(reader):
            return
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                raise asyncio.CancelledError
            if reader():
                return
            await asyncio.sleep(_CW_POSE_POLL_SECONDS)
        raise ValueError("云台心跳未确认停稳，未发送下一条指令；请检查角度与倍率反馈")

    async def _run_camera_program(self, run, segment, config, selected) -> None:
        pieces = camera_program(config, config.piece_ids if selected is None else selected)

        async def record_piece(key, label, targets, kind, zooms=None):
            shot = {
                "id": key,
                "label": label,
                "kind": kind,
                "start": self._elapsed(),
                "end": None,
                "status": "running",
                "boundary_source": "command_to_pose_feedback",
            }
            if zooms is not None:
                shot.update(zoom_start=zooms[0], zoom_end=zooms[1])
            segment.shots.append(shot)
            await self._publish_segment("CRUISE_SHOT_STARTED", run, segment)
            try:
                for target in targets:
                    if zooms is None:
                        await self._move_to_pose(target, config)
                    else:
                        await self._move_to_pose(target, config, target_zoom=zooms[1])
                shot["status"] = "complete"
            finally:
                shot["end"] = self._elapsed()
                if shot["status"] != "complete":
                    shot["status"] = "incomplete"
                await self._publish_segment("CRUISE_SHOT_FINISHED", run, segment)

        previous = (0, 0)
        for piece in pieces:
            if self._cancel.is_set():
                raise asyncio.CancelledError
            # Even an unchanged intended pose may have drifted during chassis navigation.
            observed = (self.robot.heartbeat_yaw(), self._heartbeat_pitch())
            zoom_start = config.anchor_zoom if piece.zooms is None else piece.zooms[0]
            observed_zoom = self._heartbeat_zoom()
            zoom_needs_preparation = (
                observed_zoom is not None
                and abs(observed_zoom - zoom_start) > config.zoom_tolerance
            )
            if zoom_needs_preparation or previous != piece.poses[0] or any(
                value is None or abs(value - desired) > config.angle_tolerance_degrees
                for value, desired in zip(observed, piece.poses[0])
            ):
                await record_piece(
                    f"prepare-{piece.id}", "镜头准备", [piece.poses[0]], "preparation",
                    (self._cw_zoom, zoom_start) if piece.zooms is not None or zoom_needs_preparation else None,
                )
            await record_piece(piece.id, piece.label, piece.poses[1:], "shot", piece.zooms)
            previous = piece.poses[-1]
        if previous != (0, 0):
            await record_piece("return-origin", "回原点准备", [(0, 0)], "preparation")

    async def _await_camerawork_pose(
        self,
        target_yaw: float,
        target_pitch: float,
        angle_estimated_seconds: float,
        after_revision: int | None = None,
        target_zoom: float | None = None,
        after_zoom_revision: int = 0,
        angle_tolerance: float = 5.0,
        zoom_tolerance: float = 0.1,
        busy_owner: tuple[int, int] | None = None,
        monitor_failsafe_seconds: float | None = None,
    ) -> tuple[bool, bool]:
        """Wait for travel time, then use physical stability before applying tolerances.

        Tolerance says whether a settled result is acceptable; it never proves the robot
        stopped. Zoom has no protocol speed, so it is deliberately judged only from the same
        fresh heartbeat time series as yaw and pitch.
        """
        started_at = time.monotonic()
        evaluate_after = started_at + max(0.0, angle_estimated_seconds)
        failsafe = (
            _CW_MONITOR_FAILSAFE_SECONDS
            if monitor_failsafe_seconds is None
            else max(0.0, monitor_failsafe_seconds)
        )
        deadline = evaluate_after + failsafe
        samples: deque[tuple[float, float, float]] = deque(maxlen=_CW_POSE_STABLE_SAMPLES)
        observation_deadline: float | None = None
        observation_check = 0
        observed = False
        last_revision = after_revision
        last_zoom_revision = after_zoom_revision
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                return False, observed
            busy = getattr(self.robot, "gimbal_busy_seen", None)
            if busy_owner is not None and callable(busy) and busy(busy_owner):
                return False, observed
            yaw = self.robot.heartbeat_yaw()
            pitch = self._heartbeat_pitch()
            revision = self._heartbeat_revision()
            # A pose is usable only when the adapter reports a new complete yaw+pitch
            # heartbeat sample. Treating ``revision is None`` as fresh would repeatedly
            # count cached split-axis values at startup and could falsely confirm arrival.
            fresh = revision is not None and (last_revision is None or revision > last_revision)
            if fresh and yaw is not None and pitch is not None:
                zoom_revision = self._zoom_revision()
                zoom = self._heartbeat_zoom()
                zoom_fresh = zoom_revision > last_zoom_revision
                last_revision = revision
                if zoom_fresh and zoom is not None:
                    observed = True
                    last_zoom_revision = zoom_revision
                    now = time.monotonic()
                    # Do not even begin the stability window before the angle estimate.
                    # Pre-estimate readings remain useful diagnostics, but cannot contribute
                    # one of the three samples that permits the next command.
                    if now < evaluate_after:
                        continue
                    samples.append((float(yaw), float(pitch), float(zoom)))
                    if len(samples) == _CW_POSE_STABLE_SAMPLES:
                        yaw_range = max(value[0] for value in samples) - min(value[0] for value in samples)
                        pitch_range = max(value[1] for value in samples) - min(value[1] for value in samples)
                        zoom_range = max(value[2] for value in samples) - min(value[2] for value in samples)
                        stable = (
                            yaw_range <= _CW_POSE_SETTLED_DELTA_DEG
                            and pitch_range <= _CW_POSE_SETTLED_DELTA_DEG
                            and zoom_range <= _CW_ZOOM_SETTLED_DELTA
                        )
                        yaw_error = abs(yaw - target_yaw)
                        pitch_error = abs(pitch - target_pitch)
                        zoom_error = 0.0 if target_zoom is None else abs(zoom - target_zoom)
                        inside = (
                            yaw_error <= angle_tolerance
                            and pitch_error <= angle_tolerance
                            and zoom_error <= zoom_tolerance
                        )
                        if stable and inside:
                            return True, True

                        # A settled but unacceptable pose opens a three-second observation
                        # window. At each boundary, a stationary miss fails immediately;
                        # continuing motion earns another window, up to three total.
                        if stable and not inside and observation_deadline is None:
                            observation_check = 1
                            observation_deadline = now + _CW_OUT_OF_TOLERANCE_GRACE_SECONDS

                        if observation_deadline is not None and now >= observation_deadline:
                            detail = (
                                f"水平 {yaw_error:.2f}°（允许 {angle_tolerance:g}°），"
                                f"俯仰 {pitch_error:.2f}°（允许 {angle_tolerance:g}°），"
                                f"倍率 {zoom_error:.2f}×（允许 {zoom_tolerance:g}×）"
                            )
                            if stable:
                                raise ValueError(
                                    f"云台已停稳但超出可接受偏差：{detail}；"
                                    f"第 {observation_check} 轮观察结束，停止后续镜头和导航"
                                )
                            if observation_check >= _CW_OUT_OF_TOLERANCE_MAX_CHECKS:
                                raise ValueError(
                                    f"云台连续 {_CW_OUT_OF_TOLERANCE_MAX_CHECKS} 轮观察仍未停稳："
                                    f"{detail}；停止后续镜头和导航"
                                )
                            observation_check += 1
                            observation_deadline = (
                                now + _CW_OUT_OF_TOLERANCE_GRACE_SECONDS
                            )
            await asyncio.sleep(min(_CW_POSE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        return False, observed

    async def _sample_telemetry(self) -> None:
        """Record physical heartbeat yaw/pitch, never optimistic command endpoints."""
        while not self._cancel.is_set():
            try:
                yaw = self.robot.heartbeat_yaw()
                pitch = self._heartbeat_pitch()
                if yaw is not None and pitch is not None:
                    self._telemetry.append(
                        (
                            round(self._elapsed(), 3),
                            round(float(yaw), 3),
                            round(float(pitch), 3),
                        )
                    )
            except Exception:
                pass
            await asyncio.sleep(0.3)

    async def _fail_segment(self, run: CruiseRun, segment: CruiseSegment, error: str) -> None:
        """Failure policy: keep recording, mark the moment, move to the next point.

        Nothing is cut automatically. Transit footage is usable, and so is whatever was
        filmed while a point failed, so the failure is recorded as a labelled marker for a
        human to judge rather than as an instruction to discard.
        """
        segment.status = "failed"
        segment.error = error
        self._record_capture_transition(run, "navigation_uncertain")
        segment.departed_at_seconds = self._elapsed()
        marker = await self.capture.add_marker(
            segment.transit_start_seconds
            if segment.transit_start_seconds is not None
            else self._elapsed(),
            f"{segment.path_name}#{segment.goal_id} 失败",
        )
        if marker is not None:
            run.markers.append(marker)
        await self._publish_segment("CRUISE_POINT_FAILED", run, segment)

    async def _finish(
        self,
        request: CruiseRequest,
        run: CruiseRun,
        camerawork: CameraworkConfig | None,
        recording_start_rejected: bool = False,
        recording_start_attempted: bool = True,
        camerawork_quiesced: bool = True,
    ) -> None:
        for segment in run.segments:
            if segment.status in {"pending", "navigating"}:
                segment.status = "skipped"

        session = self.capture.active_session()
        if session is not None and session.id == run.capture_session_id:
            # Save the timing evidence before stop/download. A failed transfer deliberately
            # leaves this capture active so a later manual retry can attach the same spans.
            try:
                self.capture.remember_segments(session, run.segments)
            except OSError as exc:
                run.status = "failed"
                run.error = run.error or str(exc)
                run.warnings.append("点位信息暂存失败；录制仍会立即停止")

        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._telemetry_task
            self._telemetry_task = None
        if session is not None and session.id == run.capture_session_id:
            # The transfer may fail after recording has stopped. Persist the measured track
            # before that boundary so a later manual retry writes the same gimbal sidecar.
            try:
                self.capture.remember_gimbal_samples(session, self._telemetry)
            except OSError as exc:
                run.status = "failed"
                run.error = run.error or str(exc)
                run.warnings.append("云台轨迹暂存失败；录制仍会立即停止")

        # Stop what is actually recording, or recover a Stop that the robot applied before its
        # reply reached us. The finalizer does not send Stop when the current state is already
        # idle; it consumes the late video URL instead.
        media_sync_error = ""
        state = None
        if request.record and recording_start_attempted and not recording_start_rejected:

            def remember_final_url(media_url: str) -> None:
                if session is not None and session.id == run.capture_session_id:
                    self.capture.remember_pending_media(session, media_url)

            try:
                log_event(
                    "info",
                    "cruise.recording.finalize.started",
                    run_id=run.id,
                )
                state = await self.robot.finalize_capture_recording(
                    on_media_url=remember_final_url,
                )
                log_event(
                    "info",
                    "cruise.recording.finalize.finished",
                    run_id=run.id,
                    recording=state.recording,
                    has_media_url=bool(state.media_url),
                    has_local_file=bool(state.media_local_path),
                    media_sync_error=state.media_sync_error,
                )
            except Exception as exc:
                log_event(
                    "error",
                    "cruise.recording.finalize.failed",
                    run_id=run.id,
                    error=str(exc),
                )
                run.error = run.error or f"Stop recording failed: {exc}"
                run.status = "failed"
                with suppress(Exception):
                    state = await self.robot.status()
        if state is not None:
            run.media_url = state.media_url
            run.media_local_path = state.media_local_path
            media_sync_error = str(state.media_sync_error or "")
            if session is not None and session.id == run.capture_session_id:
                try:
                    self.capture.remember_pending_media(
                        session,
                        state.media_url,
                        state.media_sync_error,
                        state.media_local_path,
                    )
                except OSError as exc:
                    run.status = "failed"
                    run.error = run.error or str(exc)
                    run.warnings.append("待保存视频地址暂存失败")

        # The anchor is the physical resting pose, but it is not unbudgeted footage. Return only
        # after Stop is confirmed (or when this run never recorded), so even a 0% anchor share is
        # respected by the complete recorded file. If Stop is still ambiguous, leave the camera
        # untouched rather than contaminating a recording that may still be active.
        recording_is_stopped = (
            not request.record
            or recording_start_rejected
            or not recording_start_attempted
            or (state is not None and not state.recording and self._recording_status_known())
        )

        if request.record and not run.media_local_path:
            run.error = run.error or media_sync_error or "录制文件尚未保存到本地"
            run.status = "failed"

        # Stopped on the robot is not the same as safely finalized on the desktop. Attach the
        # point/gimbal sidecars first; only then may the session disappear from recovery UI.
        if request.record and run.media_local_path and recording_is_stopped:
            try:
                completed = await self.capture.complete_with_recording(
                    run.media_local_path,
                    run.segments,
                )
                if completed is None:
                    raise OSError("本次没有可恢复的采集会话")
            except OSError as exc:
                run.status = "failed"
                run.error = run.error or str(exc)
                run.warnings.append("拍摄信息写入失败，请到「拍摄」中重试保存")
        elif not request.record or recording_start_rejected or not recording_start_attempted:
            try:
                await self.capture.stop()
            except OSError as exc:
                run.status = "failed"
                run.error = run.error or str(exc)
                run.warnings.append("采集会话状态未能保存，请重试后再开始下一次拍摄")
        elif request.record:
            # The spans are the whole reason editing can tell a parked shot from a moving one.
            # No local file means they cannot be attached yet, so keep the session recoverable.
            if recording_is_stopped:
                run.warnings.append(
                    "录制文件未同步到本地，点位信息未能写入；拍摄会话已保留以便重试"
                )
            else:
                run.warnings.append("录制尚未确认停止，或文件未同步到本地；拍摄会话已保留以便重试")

        run.ended_at = utc_now()
        log_event(
            "info",
            "cruise.run.finished",
            run_id=run.id,
            status=run.status,
            error=run.error,
            warnings=run.warnings,
            has_local_file=bool(run.media_local_path),
        )
        event = {
            "canceled": "CRUISE_CANCELED",
            "failed": "CRUISE_FAILED",
        }.get(run.status, "CRUISE_FINISHED")
        await self.events.publish(event, run.model_dump(mode="json"))

    async def _publish_segment(self, event: str, run: CruiseRun, segment: CruiseSegment) -> None:
        session = self.capture.active_session()
        if session is not None and session.id == run.capture_session_id:
            try:
                self.capture.remember_segments(session, run.segments)
            except OSError:
                warning = "点位过程暂存失败；停止录制时会重试保存"
                if warning not in run.warnings:
                    run.warnings.append(warning)
        await self.events.publish(
            event,
            {"run_id": run.id, "segment": segment.model_dump(mode="json")},
        )

    def _record_capture_transition(self, run: CruiseRun, kind: str) -> None:
        session = self.capture.active_session()
        if session is not None and session.id == run.capture_session_id:
            self._observe_recording(run, session, {"type": kind, "monotonic": time.monotonic()})

    def _observe_recording(self, run: CruiseRun, session, event: dict) -> None:
        kind = event["type"]
        if kind == "record_write":
            self._origin_monotonic = event["monotonic"]
            session.recording_clock.update(
                origin="record_write_estimate", quality="application_estimate"
            )
        segment = self._capture_visit
        if (
            kind.startswith("goal_")
            and event.get("path_name") is not None
            and (
                segment is None
                or (segment.path_name, segment.goal_id)
                != (event["path_name"], event.get("goal_id"))
            )
        ):
            return
        index = segment.index if segment else None
        if any(
            e["type"] == kind and e.get("visit_index") == index for e in session.recording_events
        ):
            return
        seconds = max(0.0, event["monotonic"] - self._origin_monotonic)
        if segment is not None:
            if kind == "goal_write":
                segment.transit_start_seconds = seconds
            elif kind == "goal_done":
                segment.arrived_at_seconds = seconds
        try:
            self.capture.remember_recording_event(
                session,
                {
                    "type": kind,
                    "seconds": seconds,
                    "visit_index": index,
                    "sequence": len(session.recording_events),
                },
            )
        except OSError:
            warning = "拍摄分段事件暂存失败；停止录制时会重试保存"
            if warning not in run.warnings:
                run.warnings.append(warning)

    def _recording_status_known(self) -> bool:
        reader = getattr(self.robot, "recording_status_known", None)
        return bool(reader()) if callable(reader) else True

    async def _sleep_or_cancel(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._cancel.wait(), timeout=seconds)
            return True
        except TimeoutError:
            return False

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._origin_monotonic)


def _goal_accepted(result: Any) -> bool:
    if not isinstance(result, dict):
        return True
    check = result.get("goal_check")
    if check is None:
        return True
    return str(check).lower() == "true"
