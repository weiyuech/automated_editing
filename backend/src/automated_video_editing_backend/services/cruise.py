from __future__ import annotations

import asyncio
import json
import random
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CruiseRequest,
    CruiseRoute,
    CruiseRouteIssue,
    CruiseRouteValidation,
    CruiseRun,
    CruiseSegment,
    GimbalMoveRequest,
    GimbalScanConfig,
    RobotGoalCommand,
    utc_now,
)
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.robot import RobotService

# Backend-only 自动运镜. The UI is a single on/off toggle; when on, each camerawork run picks a
# style by these weights (never exposed): a wandering A→B→C path, a two-point ping-pong, or a
# walk with brief holds. All legs are slow (2–5°/s) and go to fresh targets, so the gimbal never
# hits its own auto-loop reset. Ranges are the safe UI bands, inside the hardware limits.
_CAMERAWORK_MODES: tuple[tuple[str, float], ...] = (("wander", 50.0), ("pingpong", 30.0), ("holds", 20.0))
_CW_SPEED = (2.0, 5.0)
_CW_YAW = (-90.0, 90.0)
_CW_PITCH = (-60.0, 15.0)
_CW_ZOOM = (1.0, 3.5)
_CW_ZOOM_PROB = 0.4       # chance a dwell leg also samples a new zoom
_CW_HOLD_PROB = 0.25      # chance a "holds"-mode leg is a brief pause instead of a move
_CW_HOLD_SECONDS = 1.5
_CW_LEG_MARGIN = 0.6      # settle headroom added to a leg's travel time


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class CruiseService:
    """Drives the robot through an ordered list of points while a single recording runs.

    Deliberately narrower than a live-tour engine: no knowledge base, no narration, no
    repeat count. Points carry no goal_object by default, so the robot is never asked to
    align its gimbal on arrival and never stalls waiting for object recognition.
    """

    def __init__(self, events: EventHub, robot: RobotService, capture: CaptureService) -> None:
        self.events = events
        self.robot = robot
        self.capture = capture
        self._run: CruiseRun | None = None
        self._task: asyncio.Task[None] | None = None
        self._cancel = asyncio.Event()
        self._origin_monotonic = 0.0
        # Our last commanded pitch/zoom. Zoom has no heartbeat feedback, so we track what we sent
        # to use as the next leg's start; pitch is tracked for the same reason (start continuity).
        self._cw_pitch = 0.0
        self._cw_zoom = 1.0
        # (video_time, yaw, pitch) samples recorded during an auto-camerawork run, written beside
        # the video so the grader can tell a slow pan over a plain surface from a true freeze.
        self._telemetry: list[tuple[float, float, float]] = []
        self._telemetry_task: asyncio.Task[None] | None = None

    def current(self) -> CruiseRun | None:
        return self._run

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, request: CruiseRequest) -> CruiseRun:
        if self.is_running:
            raise ValueError("A cruise is already running")
        # Recording and capture sessions are single-resource. Without this the cruise would
        # adopt the manual session, send a second video_record start, and close a recording
        # the operator opened.
        if self.capture.active_session() is not None:
            raise ValueError("A manual capture is running; stop it before starting a cruise")
        # Checked before anything is opened. The first goal would refuse anyway, but by then a
        # recording has started and a run is on screen half-failed for no stated reason.
        self.robot.refuse_if_unfit_to_drive()

        run = CruiseRun(
            title=request.title,
            map_name=request.map_name,
            recording=request.record,
            segments=[
                CruiseSegment(
                    index=index,
                    path_name=point.path_name,
                    goal_id=point.goal_id,
                    goal_object=point.goal_object,
                )
                for index, point in enumerate(request.points)
            ],
        )
        self._run = run
        self._cancel = asyncio.Event()
        self._task = asyncio.create_task(self._execute(request, run))
        await self.events.publish("CRUISE_STARTED", run.model_dump(mode="json"))
        return run

    async def cancel(self) -> CruiseRun | None:
        if not self.is_running:
            return self._run
        self._cancel.set()
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
        request = route.request
        issues: list[CruiseRouteIssue] = []

        try:
            maps = await self.robot.map_list()
        except Exception as exc:
            return CruiseRouteValidation(
                route_id=route.id,
                checked=False,
                issues=[
                    CruiseRouteIssue(
                        level="warning",
                        field="robot",
                        message=f"Robot unreachable, map and path were not verified: {exc}",
                    )
                ],
            )

        map_name = request.map_name or (await self.robot.status()).map_name
        if not map_name:
            issues.append(
                CruiseRouteIssue(
                    level="warning",
                    field="map_name",
                    message="Route names no map and no map is active, so paths were not verified",
                )
            )
            return CruiseRouteValidation(route_id=route.id, issues=issues)

        if not maps:
            issues.append(
                CruiseRouteIssue(
                    level="warning",
                    field="map_name",
                    value=map_name,
                    message="Robot returned an empty map list, so nothing was verified",
                )
            )
            return CruiseRouteValidation(route_id=route.id, issues=issues)

        if map_name not in maps:
            issues.append(
                CruiseRouteIssue(
                    level="error",
                    field="map_name",
                    value=map_name,
                    message=f"Robot has no map named '{map_name}'",
                )
            )
            return CruiseRouteValidation(route_id=route.id, issues=issues)

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
            return CruiseRouteValidation(route_id=route.id, checked=False, issues=issues)

        points_by_path: dict[str, list[int]] = {}
        for index, point in enumerate(request.points):
            points_by_path.setdefault(point.path_name, []).append(index)

        if not paths:
            issues.append(
                CruiseRouteIssue(
                    level="warning",
                    field="path_name",
                    value=map_name,
                    message=f"Robot returned no paths for map '{map_name}', so paths were not verified",
                )
            )
            return CruiseRouteValidation(route_id=route.id, issues=issues)

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

        return CruiseRouteValidation(route_id=route.id, issues=issues)

    async def _execute(self, request: CruiseRequest, run: CruiseRun) -> None:
        try:
            if request.map_name:
                await self.robot.switch_map(request.map_name)

            session = await self.capture.start(request.title)
            run.capture_session_id = session.id

            if request.record:
                await self.robot.start_recording()
            # Every timestamp on this run is seconds from here, so markers line up with the
            # start of the recorded file rather than with wall-clock time.
            self._origin_monotonic = time.monotonic()

            if request.auto_camerawork and request.record:
                self._telemetry = []
                self._telemetry_task = asyncio.create_task(self._sample_telemetry())

            canceled = False
            for segment in run.segments:
                if self._cancel.is_set():
                    canceled = True
                    break
                await self._run_segment(request, run, segment)
            run.status = "canceled" if canceled or self._cancel.is_set() else "succeeded"
        except asyncio.CancelledError:
            run.status = "canceled"
            raise
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
        finally:
            await self._finish(request, run)

    async def _run_segment(
        self,
        request: CruiseRequest,
        run: CruiseRun,
        segment: CruiseSegment,
    ) -> None:
        segment.status = "navigating"
        segment.transit_start_seconds = self._elapsed()
        await self._publish_segment("CRUISE_POINT_DISPATCHED", run, segment)

        try:
            result = await self.robot.set_goal(
                RobotGoalCommand(
                    path_name=segment.path_name,
                    goal_id=segment.goal_id,
                    goal_object=segment.goal_object,
                )
            )
        except ConnectionError:
            # The robot is gone; failing every remaining point one by one would be noise.
            raise
        except Exception as exc:
            await self._fail_segment(run, segment, str(exc))
            return

        if not _goal_accepted(result):
            await self._fail_segment(run, segment, "Robot rejected the goal (goal_check false)")
            return

        if request.auto_camerawork:
            # Oscillate the gimbal while the robot drives to the point, so transit footage moves.
            camerawork = asyncio.create_task(self._run_camerawork(
                deadline=time.monotonic() + request.arrival_timeout_seconds, allow_zoom=False,
            ))
            try:
                arrival = await self._await_arrival(request.arrival_timeout_seconds)
            finally:
                camerawork.cancel()
                with suppress(asyncio.CancelledError):
                    await camerawork
        else:
            arrival = await self._await_arrival(request.arrival_timeout_seconds)
        if arrival == "canceled":
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return
        if arrival != "done":
            await self._fail_segment(run, segment, f"Robot reported '{arrival}' for this point")
            return

        segment.status = "arrived"
        segment.arrived_at_seconds = self._elapsed()
        marker = await self.capture.add_marker(
            segment.arrived_at_seconds,
            f"{segment.path_name}#{segment.goal_id}",
        )
        if marker is not None:
            run.markers.append(marker)
        await self._publish_segment("CRUISE_POINT_ARRIVED", run, segment)

        await self._dwell(request, segment)

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

    async def _dwell(self, request: CruiseRequest, segment: CruiseSegment) -> None:
        """Hold position long enough to get a usable shot, optionally panning while parked."""
        scan = request.gimbal_scan
        dwell = random.uniform(request.dwell_min_seconds, request.dwell_max_seconds)
        if scan.enabled:
            # Floor the dwell at the scan's worst case so a pan is never cut mid-return.
            dwell = max(dwell, scan.budget_seconds)

        deadline = time.monotonic() + dwell
        if request.auto_camerawork:
            # Slow drift + occasional zoom for the whole dwell, so a parked shot is never frozen.
            await self._run_camerawork(deadline=deadline, allow_zoom=True)
            segment.scanned = True
        elif scan.enabled:
            segment.scanned = await self._scan(scan)
        remaining = deadline - time.monotonic()
        if remaining > 0:
            await self._sleep_or_cancel(remaining)

    async def _scan(self, scan: GimbalScanConfig) -> bool:
        """Slow pan away from the current angle and back again. Runs only while parked."""
        center = self.robot.heartbeat_yaw()
        if center is None:
            center = (await self.robot.status()).yaw
        if center is None:
            # Camera position unknown, so a return-to-origin cannot be guaranteed.
            return False

        target = max(-90.0, min(90.0, center + scan.signed_offset_deg))
        if abs(target - center) < scan.settle_tolerance_deg:
            return False

        await self.robot.sweep_camera(target, scan.yaw_speed_deg_s)
        await self._await_yaw(target, scan)
        if self._cancel.is_set():
            return False
        await self.robot.sweep_camera(center, scan.yaw_speed_deg_s)
        await self._await_yaw(center, scan)
        return True

    async def _await_yaw(self, target: float, scan: GimbalScanConfig) -> bool:
        """Poll heartbeat yaw until the gimbal reaches target.

        A robot that never reports gimbal yaw simply burns the leg budget here, which is
        the time-based fallback for the same move.
        """
        deadline = time.monotonic() + scan.leg_budget_seconds
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                return False
            yaw = self.robot.heartbeat_yaw()
            if yaw is not None and abs(yaw - target) <= scan.settle_tolerance_deg:
                return True
            await asyncio.sleep(0.2)
        return False

    def _pick_camerawork_mode(self) -> str:
        roll = random.uniform(0.0, sum(weight for _name, weight in _CAMERAWORK_MODES))
        cumulative = 0.0
        for name, weight in _CAMERAWORK_MODES:
            cumulative += weight
            if roll <= cumulative:
                return name
        return _CAMERAWORK_MODES[0][0]

    def _camerawork_pose(self) -> tuple[float, float]:
        return (round(random.uniform(*_CW_YAW), 1), round(random.uniform(*_CW_PITCH), 1))

    async def _camerawork_leg(
        self, target: tuple[float, float], speed: float, zoom: float | None, deadline: float,
    ) -> None:
        """One slow move to a fresh (yaw, pitch) target at 2–5°/s, optionally re-zooming.

        A failed gimbal command must never break the cruise, so anything short of a lost
        connection is swallowed. The wait is time-based (travel ÷ speed): we move to a new target
        each time, so there is no auto-loop to out-run, and zoom has no arrival feedback anyway.
        """
        heartbeat_yaw = self.robot.heartbeat_yaw()
        start_yaw = _clamp(heartbeat_yaw if heartbeat_yaw is not None else 0.0, *_CW_YAW)
        start_pitch = _clamp(self._cw_pitch, *_CW_PITCH)
        target_yaw, target_pitch = target
        zoom_end = zoom if zoom is not None else self._cw_zoom
        command = GimbalMoveRequest(
            yaw_start=start_yaw, yaw_end=target_yaw, yaw_speed=speed,
            pitch_start=start_pitch, pitch_end=target_pitch, pitch_speed=speed,
            zoom_start=self._cw_zoom, zoom_end=zoom_end,
        )
        try:
            await self.robot.set_gimbal(command)
        except ConnectionError:
            raise
        except Exception:
            return
        self._cw_pitch = target_pitch
        self._cw_zoom = zoom_end
        travel = max(abs(target_yaw - start_yaw), abs(target_pitch - start_pitch))
        budget = (travel / speed + _CW_LEG_MARGIN) if speed > 0 else _CW_LEG_MARGIN
        await self._sleep_or_cancel(max(0.3, min(budget, deadline - time.monotonic())))

    async def _run_camerawork(self, deadline: float, allow_zoom: bool) -> None:
        """Slow, organic camerawork until `deadline` or cancel — never the auto-loop reset."""
        mode = self._pick_camerawork_mode()
        endpoint_a, endpoint_b = self._camerawork_pose(), self._camerawork_pose()
        toggle = False
        while time.monotonic() < deadline and not self._cancel.is_set():
            if mode == "pingpong":
                target = endpoint_a if toggle else endpoint_b
                toggle = not toggle
            elif mode == "holds" and random.random() < _CW_HOLD_PROB:
                await self._sleep_or_cancel(min(_CW_HOLD_SECONDS, max(0.0, deadline - time.monotonic())))
                continue
            else:
                target = self._camerawork_pose()
            speed = random.uniform(*_CW_SPEED)
            zoom = (
                round(random.uniform(*_CW_ZOOM), 2)
                if allow_zoom and random.random() < _CW_ZOOM_PROB
                else None
            )
            await self._camerawork_leg(target, speed, zoom, deadline)

    async def _sample_telemetry(self) -> None:
        """Record (video_time, yaw, pitch) a few times a second while the recording runs."""
        while not self._cancel.is_set():
            try:
                state = await self.robot.status()
                if state.yaw is not None and state.pitch is not None:
                    self._telemetry.append((
                        round(self._elapsed(), 3),
                        round(float(state.yaw), 3),
                        round(float(state.pitch), 3),
                    ))
            except Exception:
                pass
            await asyncio.sleep(0.3)

    def _write_gimbal_sidecar(self, media_local_path: str | None) -> None:
        """Persist the run's gimbal track beside the video so analysis can rescue slow pans."""
        if not media_local_path or not self._telemetry:
            return
        try:
            Path(str(media_local_path) + ".gimbal.json").write_text(
                json.dumps({"samples": [list(sample) for sample in self._telemetry]}, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass

    async def _fail_segment(self, run: CruiseRun, segment: CruiseSegment, error: str) -> None:
        """Failure policy: keep recording, mark the moment, move to the next point.

        Nothing is cut automatically. Transit footage is usable, and so is whatever was
        filmed while a point failed, so the failure is recorded as a labelled marker for a
        human to judge rather than as an instruction to discard.
        """
        segment.status = "failed"
        segment.error = error
        segment.departed_at_seconds = self._elapsed()
        marker = await self.capture.add_marker(
            segment.transit_start_seconds if segment.transit_start_seconds is not None else self._elapsed(),
            f"{segment.path_name}#{segment.goal_id} 失败",
        )
        if marker is not None:
            run.markers.append(marker)
        await self._publish_segment("CRUISE_POINT_FAILED", run, segment)

    async def _finish(self, request: CruiseRequest, run: CruiseRun) -> None:
        if self._telemetry_task is not None:
            self._telemetry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._telemetry_task
            self._telemetry_task = None
        for segment in run.segments:
            if segment.status in {"pending", "navigating"}:
                segment.status = "skipped"

        # Stop what is actually recording, rather than what was asked for. The two differ in
        # both directions: a start that failed leaves nothing to stop, and a run cancelled
        # before its first point may never have reached the start at all. The robot's own
        # state answers it — the heartbeat keeps `recording` current, and a successful start
        # sets it directly — so there is nothing to track separately.
        if await self._is_recording():
            try:
                state = await self.robot.stop_recording()
            except Exception as exc:
                run.error = run.error or f"Stop recording failed: {exc}"
                state = await self.robot.status()
            run.media_url = state.media_url
            run.media_local_path = state.media_local_path
            self._write_gimbal_sidecar(run.media_local_path)

        session = self.capture.active_session()
        with suppress(Exception):
            await self.capture.stop()

        # The spans are the whole reason editing can tell a parked shot from a moving one.
        # Losing them costs far more than it used to: footage without them is edited as
        # ordinary video, which drops most of the editing choices available to it. So each way
        # this can fail now says so, rather than leaving a recording that looks complete and
        # quietly is not.
        if request.record:
            self._attach_spans(run, session)

        run.ended_at = utc_now()
        event = {
            "canceled": "CRUISE_CANCELED",
            "failed": "CRUISE_FAILED",
        }.get(run.status, "CRUISE_FINISHED")
        await self.events.publish(event, run.model_dump(mode="json"))

    async def _is_recording(self) -> bool:
        try:
            return bool((await self.robot.status()).recording)
        except Exception:
            return False

    def _attach_spans(self, run: CruiseRun, session) -> None:
        """Write the run's notes and point spans beside the recording, and say if it did not.

        Three ways this fails, each of which used to pass in silence: no session, no file
        synced back from the robot, or the write itself refused. All three end with footage
        that looks fine and carries none of what the robot knew about it.
        """
        if session is None:
            run.warnings.append("本次没有采集会话，点位信息未能写入")
            return
        if not run.media_local_path:
            run.warnings.append("录制文件未同步到本地，点位信息未能写入")
            return
        try:
            written = self.capture.attach_to_recording(session, run.media_local_path, run.segments)
        except Exception as exc:
            run.warnings.append(f"点位信息写入失败：{exc}")
            return
        if written is None:
            run.warnings.append("点位信息写入失败，剪辑时将按普通视频处理")

    async def _publish_segment(self, event: str, run: CruiseRun, segment: CruiseSegment) -> None:
        await self.events.publish(
            event,
            {"run_id": run.id, "segment": segment.model_dump(mode="json")},
        )

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
