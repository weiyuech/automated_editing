from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

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
    GimbalScanConfig,
    RobotGoalCommand,
    utc_now,
)
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.robot import RobotCommandNotSentError, RobotService

# Preserve the original 50/30/20 choice, but replace the unsafe temporary-hold block with a
# visible move back to the operator's anchor. The anchor choice is one action, not permission to
# spend a whole transit leg motionless; after reaching it the planner immediately chooses again.
_CAMERAWORK_MODES: tuple[tuple[str, float], ...] = (
    ("wander", 50.0),
    ("pingpong", 30.0),
    ("anchor", 20.0),
)
_CW_OPPOSITE_PROB = 0.80
_CW_OPPOSITE_DISTANCE = (0.60, 0.90)
_CW_OUTWARD_DISTANCE = (0.10, 0.30)
_CW_CENTER_DISTANCE = (0.40, 0.80)
# Direction selection only; targets still use the complete configured yaw range.
_CW_DIRECTION_ROOM_DEG = 10.0
_CW_LEG_MARGIN = 0.6
_CW_ZOOM_SETTLE_SECONDS = 1.0
_CW_POSE_TOLERANCE_DEG = 2.0
_CW_POSE_POLL_SECONDS = 0.2
_CW_POSE_STABLE_SAMPLES = 2
_CW_ANCHOR_HOLD_SECONDS = 1.0
# Operator-facing timeout configuration was removed. Keep one execution-level value so
# legacy saved routes that still contain 180 seconds cannot silently restore the old behavior.
_ARRIVAL_TIMEOUT_SECONDS = 60.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


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
        self._cancel = asyncio.Event()
        self._origin_monotonic = 0.0
        self._camerawork_config_provider = camerawork_config_provider or CameraworkConfig
        # Our last commanded pitch/zoom. Zoom has no heartbeat feedback, so we track what we sent
        # to use as the next leg's start; pitch is tracked for the same reason (start continuity).
        self._cw_yaw = 0.0
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

        camerawork = self._camerawork_config_provider() if request.auto_camerawork else None
        if camerawork is not None and not camerawork.configured:
            raise ValueError("Automatic camerawork is not configured; save it in 镜头设置 first")

        # Product invariant: cruise is navigation-only. ``goal_object`` remains accepted in
        # stored/API data for backward compatibility, but an old route must never re-enable
        # robot-owned object alignment or make shooting wait on object recognition.
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
        self._cancel = asyncio.Event()
        self._task = asyncio.create_task(self._execute(request, run, camerawork))
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
            if request.map_name:
                await self.robot.switch_map(request.map_name)

            # Establish the operator's intended resting composition before footage or base
            # movement begins. Zoom has no heartbeat feedback, so every run also resets our
            # command-side tracking here.
            if camerawork is not None:
                self._cw_zoom = 1.0
                await self._return_to_anchor(camerawork)

            session = await self.capture.start(request.title)
            run.capture_session_id = session.id

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
            # Every timestamp on this run is seconds from here, so markers line up with the
            # start of the recorded file rather than with wall-clock time.
            self._origin_monotonic = time.monotonic()

            if (request.auto_camerawork or request.gimbal_scan.enabled) and request.record:
                self._telemetry_task = asyncio.create_task(self._sample_telemetry())

            canceled = False
            for segment in run.segments:
                if self._cancel.is_set():
                    canceled = True
                    break
                await self._run_segment(request, run, segment, camerawork)
            run.status = "canceled" if canceled or self._cancel.is_set() else "succeeded"
        except asyncio.CancelledError:
            run.status = "canceled"
            raise
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
        finally:
            await self._finish(
                request,
                run,
                camerawork,
                recording_start_rejected,
                recording_start_attempted,
            )

    async def _run_segment(
        self,
        request: CruiseRequest,
        run: CruiseRun,
        segment: CruiseSegment,
        camerawork: CameraworkConfig | None,
    ) -> None:
        segment.status = "navigating"
        segment.transit_start_seconds = self._elapsed()
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
            # The robot is gone; failing every remaining point one by one would be noise.
            raise
        except Exception as exc:
            await self._fail_segment(run, segment, str(exc))
            return

        if not _goal_accepted(result):
            await self._fail_segment(run, segment, "Robot rejected the goal (goal_check false)")
            return

        camerawork_task: asyncio.Task[None] | None = None
        stop_camerawork: asyncio.Event | None = None
        if camerawork is not None:
            # Yaw and pitch are planned only while the base is travelling. Arrival sets this
            # boundary and cancels the planner wait before the parked anchor command takes over.
            stop_camerawork = asyncio.Event()
            camerawork_task = asyncio.create_task(self._run_camerawork(
                deadline=time.monotonic() + _ARRIVAL_TIMEOUT_SECONDS,
                config=camerawork,
                stop_requested=stop_camerawork,
            ))
            try:
                arrival = await self._await_arrival(_ARRIVAL_TIMEOUT_SECONDS)
            except BaseException:
                await self._cancel_camerawork(camerawork_task)
                raise
            finally:
                stop_camerawork.set()
        else:
            arrival = await self._await_arrival(_ARRIVAL_TIMEOUT_SECONDS)

        # Arrival is a phase boundary, not a request to let an old random target keep owning
        # the camera while the base is parked. Cancelling stops the planner's wait; the parked
        # phase immediately replaces the in-flight device target with the anchor command.
        await self._cancel_camerawork(camerawork_task)
        if arrival == "canceled":
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return
        if arrival != "done":
            if camerawork is not None:
                with suppress(Exception):
                    await self._return_to_anchor(camerawork)
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

        await self._dwell(request, segment, camerawork)

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

    async def _dwell(
        self,
        request: CruiseRequest,
        segment: CruiseSegment,
        camerawork: CameraworkConfig | None,
    ) -> None:
        """Use the parked budget for safe camerawork and finish visibly on the anchor."""
        scan = request.gimbal_scan
        dwell = random.uniform(request.dwell_min_seconds, request.dwell_max_seconds)
        if scan.enabled and camerawork is None:
            # Floor the dwell at the scan's worst case so a pan is never cut mid-return.
            dwell = max(dwell, scan.budget_seconds)

        deadline = time.monotonic() + dwell
        if camerawork is not None:
            # The first command after arrival is a smooth return from the latest heartbeat pose.
            # Zoom then operates on the safe anchor composition, never on a random stopped pose.
            if not self._cancel.is_set():
                segment.scanned = await self._parked_zoom_and_anchor(camerawork)
        elif scan.enabled:
            segment.scanned = await self._scan(scan)
        remaining = max(0.0, deadline - time.monotonic())
        if camerawork is not None and not self._cancel.is_set():
            # The old deadline could expire during zoom/return and let the next transit command
            # overwrite the anchor immediately. Always leave a short, usable anchor shot.
            remaining = max(_CW_ANCHOR_HOLD_SECONDS, remaining)
        if remaining > 0:
            await self._sleep_or_cancel(remaining)

    async def _cancel_camerawork(self, task: asyncio.Task[None] | None) -> None:
        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

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

        await self.robot.sweep_camera(
            target,
            scan.yaw_speed_deg_s,
            context="cruise_stationary_scan",
        )
        await self._await_yaw(target, scan)
        if self._cancel.is_set():
            return False
        await self.robot.sweep_camera(
            center,
            scan.yaw_speed_deg_s,
            context="cruise_stationary_scan",
        )
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

    def _adaptive_yaw_target(self, current: float, config: CameraworkConfig) -> float:
        """Pick the exact large-opposite/small-outward movement requested by operators.

        Positive yaw is physical left in this protocol and negative yaw is physical right,
        regardless of whether the configured interval is symmetric. A left-side pose therefore
        moves broadly toward the right boundary, or uses only a small fraction of its remaining
        left room; the right-side case mirrors it. Every leg re-reads the real current pose.
        """
        low, high = config.yaw_min, config.yaw_max
        current = _clamp(current, low, high)
        # Left/right is a physical protocol convention, not a property of the operator's
        # configured interval: positive yaw is left and negative yaw is right.  Using the
        # interval midpoint reverses the bias for valid asymmetric ranges (for example +5°
        # inside -10°..+50° is still physically left, even though it is below +20°).
        if current > 0:  # physically left
            inward_room = current - low
            outward_room = high - current
            choose_opposite = inward_room > _CW_DIRECTION_ROOM_DEG and (
                outward_room <= _CW_DIRECTION_ROOM_DEG or random.random() < _CW_OPPOSITE_PROB
            )
            if choose_opposite:
                distance = max(1.0, random.uniform(*_CW_OPPOSITE_DISTANCE) * inward_room)
                return round(_clamp(current - distance, low, high))
            distance = max(1.0, random.uniform(*_CW_OUTWARD_DISTANCE) * outward_room)
            return round(_clamp(current + distance, low, high))

        if current < 0:  # physically right
            inward_room = high - current
            outward_room = current - low
            choose_opposite = inward_room > _CW_DIRECTION_ROOM_DEG and (
                outward_room <= _CW_DIRECTION_ROOM_DEG or random.random() < _CW_OPPOSITE_PROB
            )
            if choose_opposite:
                distance = max(1.0, random.uniform(*_CW_OPPOSITE_DISTANCE) * inward_room)
                return round(_clamp(current + distance, low, high))
            distance = max(1.0, random.uniform(*_CW_OUTWARD_DISTANCE) * outward_room)
            return round(_clamp(current - distance, low, high))

        # At the physical centre there is no "farther outward" side. Pick either available
        # direction equally, but still demand a visible medium/large sweep rather than jitter.
        right_room = current - low
        left_room = high - current
        choose_right = right_room > _CW_DIRECTION_ROOM_DEG and (
            left_room <= _CW_DIRECTION_ROOM_DEG or random.random() < 0.5
        )
        if choose_right:
            distance = max(1.0, random.uniform(*_CW_CENTER_DISTANCE) * (current - low))
            return round(_clamp(current - distance, low, high))
        distance = max(1.0, random.uniform(*_CW_CENTER_DISTANCE) * (high - current))
        return round(_clamp(current + distance, low, high))

    def _camerawork_pose(
        self, current_yaw: float, config: CameraworkConfig,
    ) -> tuple[float, float]:
        return (
            self._adaptive_yaw_target(current_yaw, config),
            random.randint(config.pitch_min, config.pitch_max),
        )

    def _separate_camerawork_target(
        self,
        target: tuple[float, float],
        current_yaw: float,
        current_pitch: float,
        config: CameraworkConfig,
    ) -> tuple[float, float]:
        """Keep the selected yaw, and choose a pitch in a different angular region.

        Regions are the four halves of the user's yaw/pitch rectangle, not protocol-zero
        quadrants. A point on a dividing line belongs to the greater-value half. This filter
        runs before each random leg, including the second ping-pong leg; anchors bypass it.
        Crossing either dividing line is sufficient; no minimum travel distance is imposed.
        """
        current_yaw = _clamp(current_yaw, config.yaw_min, config.yaw_max)
        current_pitch = _clamp(current_pitch, config.pitch_min, config.pitch_max)
        yaw = round(_clamp(target[0], config.yaw_min, config.yaw_max))
        pitch = round(_clamp(target[1], config.pitch_min, config.pitch_max))
        yaw_mid = (config.yaw_min + config.yaw_max) / 2.0
        pitch_mid = (config.pitch_min + config.pitch_max) / 2.0
        yaw_changes_half = (yaw >= yaw_mid) != (current_yaw >= yaw_mid)

        def acceptable(candidate: int) -> bool:
            return yaw_changes_half or (
                (candidate >= pitch_mid) != (current_pitch >= pitch_mid)
            )

        if not acceptable(pitch):
            # Valid profiles have distinct integer pitch bounds. The opposite pitch endpoint
            # always changes half, so this is nonempty even for a one-degree pitch range.
            choices = [
                candidate for candidate in range(config.pitch_min, config.pitch_max + 1)
                if acceptable(candidate)
            ]
            pitch = random.choice(choices)
        return yaw, pitch

    def _pingpong_poses(
        self, current_yaw: float, config: CameraworkConfig,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Return opposite-first endpoints on physical sides when the allowed range permits."""
        midpoint = (config.yaw_min + config.yaw_max) / 2.0
        span = config.yaw_max - config.yaw_min
        quarter = span * 0.25
        # Prefer genuinely negative/right and positive/left endpoints whenever the configured
        # range contains them. A one-sided range falls back to its lower and upper quarters.
        right_high = min(0.0, midpoint - quarter)
        if right_high <= config.yaw_min:
            right_high = config.yaw_min + quarter
        left_low = max(0.0, midpoint + quarter)
        if left_low >= config.yaw_max:
            left_low = config.yaw_max - quarter
        right = (
            round(random.uniform(config.yaw_min, right_high)),
            random.randint(config.pitch_min, config.pitch_max),
        )
        left = (
            round(random.uniform(left_low, config.yaw_max)),
            random.randint(config.pitch_min, config.pitch_max),
        )
        if current_yaw > 0:  # currently left: sweep right first
            return right, left
        if current_yaw < 0:  # currently right: sweep left first
            return left, right
        return (right, left) if random.random() < 0.5 else (left, right)

    def _current_yaw(self, config: CameraworkConfig) -> float:
        heartbeat = self.robot.heartbeat_yaw()
        value = heartbeat if heartbeat is not None else self._cw_yaw
        return _clamp(value, config.yaw_min, config.yaw_max)

    def _heartbeat_pitch(self) -> float | None:
        reader = getattr(self.robot, "heartbeat_pitch", None)
        return reader() if callable(reader) else None

    def _heartbeat_revision(self) -> int | None:
        reader = getattr(self.robot, "heartbeat_revision", None)
        return reader() if callable(reader) else None

    def _current_pitch(self, config: CameraworkConfig) -> float:
        heartbeat = self._heartbeat_pitch()
        value = heartbeat if heartbeat is not None else self._cw_pitch
        return _clamp(value, config.pitch_min, config.pitch_max)

    async def _await_camerawork_pose(
        self,
        target_yaw: float,
        target_pitch: float,
        budget: float,
        after_revision: int | None = None,
    ) -> tuple[bool, bool]:
        """Wait for consecutive physical yaw/pitch samples, or use the time budget as fallback.

        The second result says whether complete heartbeat feedback was seen. Callers may retry a
        missed anchor only when the robot actually reported a different pose; no-feedback robots
        already consumed the conservative travel budget and must not be commanded twice blindly.
        """
        deadline = time.monotonic() + max(0.0, budget)
        stable = 0
        observed = False
        last_revision = after_revision
        while time.monotonic() < deadline:
            if self._cancel.is_set():
                return False, observed
            yaw = self.robot.heartbeat_yaw()
            pitch = self._heartbeat_pitch()
            revision = self._heartbeat_revision()
            # A pose is usable only when the adapter reports a new complete yaw+pitch
            # heartbeat sample. Treating ``revision is None`` as fresh would repeatedly
            # count cached split-axis values at startup and could falsely confirm arrival.
            fresh = revision is not None and (
                last_revision is None or revision > last_revision
            )
            if fresh and yaw is not None and pitch is not None:
                observed = True
                last_revision = revision
                if (
                    abs(yaw - target_yaw) <= _CW_POSE_TOLERANCE_DEG
                    and abs(pitch - target_pitch) <= _CW_POSE_TOLERANCE_DEG
                ):
                    stable += 1
                    if stable >= _CW_POSE_STABLE_SAMPLES:
                        return True, True
                else:
                    stable = 0
            await asyncio.sleep(min(_CW_POSE_POLL_SECONDS, max(0.0, deadline - time.monotonic())))
        return False, observed

    async def _camerawork_leg(
        self,
        target: tuple[float, float],
        speed: float,
        deadline: float,
        config: CameraworkConfig,
    ) -> None:
        """One slow transit move to a fresh yaw/pitch target; zoom remains fixed.

        A failed gimbal command must never break the cruise, so anything short of a lost
        connection is swallowed. Fresh yaw/pitch feedback can confirm arrival; otherwise the
        travel ÷ speed budget allows both axes time to move before the next command.
        """
        start_yaw = self._current_yaw(config)
        start_pitch = self._current_pitch(config)
        target_yaw, target_pitch = target
        command = GimbalMoveRequest(
            yaw_start=start_yaw, yaw_end=target_yaw, yaw_speed=speed,
            pitch_start=start_pitch, pitch_end=target_pitch, pitch_speed=speed,
            zoom_start=self._cw_zoom, zoom_end=self._cw_zoom,
        )
        try:
            await self.robot.set_gimbal(command, context="cruise_moving")
        except ConnectionError:
            raise
        except Exception:
            return
        # Capture the boundary after the command is written so no pre-command heartbeat can
        # be mistaken for feedback to this target.
        heartbeat_revision = self._heartbeat_revision()
        self._cw_yaw = target_yaw
        self._cw_pitch = target_pitch
        travel = max(abs(target_yaw - start_yaw), abs(target_pitch - start_pitch))
        budget = (travel / speed + _CW_LEG_MARGIN) if speed > 0 else _CW_LEG_MARGIN
        await self._await_camerawork_pose(
            target_yaw,
            target_pitch,
            max(0.3, min(budget, deadline - time.monotonic())),
            after_revision=heartbeat_revision,
        )

    async def _run_camerawork(
        self,
        deadline: float,
        config: CameraworkConfig,
        stop_requested: asyncio.Event,
    ) -> None:
        """Continuously choose 50/30/20 move blocks while the base is in transit."""
        while (
            time.monotonic() < deadline
            and not self._cancel.is_set()
            and not stop_requested.is_set()
        ):
            mode = self._pick_camerawork_mode()
            current_yaw = self._current_yaw(config)
            current_pitch = self._current_pitch(config)
            separate_target = mode != "anchor"
            if mode == "anchor":
                if (
                    abs(current_yaw - config.anchor_yaw) <= _CW_POSE_TOLERANCE_DEG
                    and abs(current_pitch - config.anchor_pitch) <= _CW_POSE_TOLERANCE_DEG
                ):
                    # Returning to where we already are would recreate the removed static hold.
                    targets = [self._camerawork_pose(current_yaw, config)]
                    separate_target = True
                else:
                    targets = [(config.anchor_yaw, config.anchor_pitch)]
            elif mode == "pingpong":
                targets = list(self._pingpong_poses(current_yaw, config))
            else:
                targets = [self._camerawork_pose(current_yaw, config)]

            for target in targets:
                if (
                    time.monotonic() >= deadline
                    or self._cancel.is_set()
                    or stop_requested.is_set()
                ):
                    break
                if separate_target:
                    target = self._separate_camerawork_target(
                        target, self._current_yaw(config), self._current_pitch(config), config,
                    )
                speed = random.randint(config.speed_min, config.speed_max)
                await self._camerawork_leg(target, speed, deadline, config)

    def _parked_zoom_target(self, config: CameraworkConfig) -> float:
        """Choose a visible zoom away from the anchor; this is called only after arrival."""
        midpoint = (config.zoom_min + config.zoom_max) / 2.0
        if config.anchor_zoom <= midpoint:
            return round(random.uniform(midpoint, config.zoom_max), 2)
        return round(random.uniform(config.zoom_min, midpoint), 2)

    async def _parked_zoom_and_anchor(self, config: CameraworkConfig) -> bool:
        anchored = await self._return_to_anchor(config)
        if self._cancel.is_set():
            return False
        if not anchored:
            # Keep the last command pointed at the anchor, but do not add zoom motion to a pose
            # that the physical feedback says is still somewhere unsafe.
            return False

        zoom_target = self._parked_zoom_target(config)
        try:
            await self.robot.set_gimbal(
                GimbalMoveRequest(
                    yaw_start=config.anchor_yaw, yaw_end=config.anchor_yaw,
                    yaw_speed=config.speed_min,
                    pitch_start=config.anchor_pitch, pitch_end=config.anchor_pitch,
                    pitch_speed=config.speed_min,
                    zoom_start=self._cw_zoom, zoom_end=zoom_target,
                ),
                context="cruise_stationary_zoom",
            )
        except ConnectionError:
            raise
        except Exception:
            with suppress(Exception):
                await self._return_to_anchor(config)
            return False
        self._cw_yaw = config.anchor_yaw
        self._cw_pitch = config.anchor_pitch
        self._cw_zoom = zoom_target
        await self._sleep_or_cancel(_CW_ZOOM_SETTLE_SECONDS)
        return await self._return_to_anchor(config)

    async def _return_to_anchor(self, config: CameraworkConfig, *, wait: bool = True) -> bool:
        """Command the complete resting pose and confirm physical yaw/pitch when available."""
        attempts = 2 if wait else 1
        ever_observed_conflict = False
        for attempt in range(attempts):
            start_yaw = self._current_yaw(config)
            start_pitch = self._current_pitch(config)
            start_zoom = self._cw_zoom
            speed = config.speed_max
            await self.robot.set_gimbal(
                GimbalMoveRequest(
                    yaw_start=start_yaw, yaw_end=config.anchor_yaw, yaw_speed=speed,
                    pitch_start=start_pitch, pitch_end=config.anchor_pitch, pitch_speed=speed,
                    zoom_start=start_zoom, zoom_end=config.anchor_zoom,
                ),
                context="cruise_stationary_anchor",
            )
            # This boundary must be taken after the command reaches the adapter. It excludes
            # any heartbeat that raced in before the new target was actually written.
            heartbeat_revision = self._heartbeat_revision()
            self._cw_yaw = config.anchor_yaw
            self._cw_pitch = config.anchor_pitch
            self._cw_zoom = config.anchor_zoom
            if not wait:
                return False

            started = time.monotonic()
            travel = max(
                abs(config.anchor_yaw - start_yaw),
                abs(config.anchor_pitch - start_pitch),
            )
            budget = max(0.3, travel / speed + _CW_LEG_MARGIN)
            reached, observed = await self._await_camerawork_pose(
                config.anchor_yaw,
                config.anchor_pitch,
                budget,
                after_revision=heartbeat_revision,
            )
            if start_zoom != config.anchor_zoom:
                remaining_zoom = _CW_ZOOM_SETTLE_SECONDS - (time.monotonic() - started)
                if remaining_zoom > 0:
                    await self._sleep_or_cancel(remaining_zoom)
            if reached:
                return True
            if observed:
                ever_observed_conflict = True
            if not observed and not ever_observed_conflict:
                # Compatibility fallback for robots that do not expose both axes: the complete
                # conservative travel budget elapsed without a conflicting physical reading.
                return True
            if self._cancel.is_set() or attempt + 1 >= attempts:
                return False
        return False

    async def _sample_telemetry(self) -> None:
        """Record physical heartbeat yaw/pitch, never optimistic command endpoints."""
        while not self._cancel.is_set():
            try:
                yaw = self.robot.heartbeat_yaw()
                pitch = self._heartbeat_pitch()
                if yaw is not None and pitch is not None:
                    self._telemetry.append((
                        round(self._elapsed(), 3),
                        round(float(yaw), 3),
                        round(float(pitch), 3),
                    ))
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
        segment.departed_at_seconds = self._elapsed()
        marker = await self.capture.add_marker(
            segment.transit_start_seconds if segment.transit_start_seconds is not None else self._elapsed(),
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

        # A cancel/failure can happen during transit, before the normal parked return. Send the
        # home pose once more before recording closes; cancellation intentionally skips waiting.
        if camerawork is not None:
            with suppress(Exception):
                await self._return_to_anchor(camerawork, wait=not self._cancel.is_set())

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
                state = await self.robot.finalize_capture_recording(
                    on_media_url=remember_final_url,
                )
            except Exception as exc:
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

        if request.record and not run.media_local_path:
            run.error = run.error or media_sync_error or "录制文件尚未保存到本地"
            run.status = "failed"

        # Stopped on the robot is not the same as safely finalized on the desktop. Attach the
        # point/gimbal sidecars first; only then may the session disappear from recovery UI.
        if request.record and run.media_local_path:
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
            run.warnings.append("录制文件未同步到本地，点位信息未能写入")

        run.ended_at = utc_now()
        event = {
            "canceled": "CRUISE_CANCELED",
            "failed": "CRUISE_FAILED",
        }.get(run.status, "CRUISE_FINISHED")
        await self.events.publish(event, run.model_dump(mode="json"))

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
