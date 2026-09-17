from __future__ import annotations

import asyncio
import math
import random
import time
from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
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

# The four-quadrant planner has one deliberately hidden source of timing variation. Operators
# choose one anchor duration; each real anchor window varies around it by at most 30%.
_CW_ANCHOR_DWELL_JITTER = (0.70, 1.30)
_CW_PHASE_QUADRANTS = "quadrants"
_CW_PHASE_ANCHOR = "anchor"
_CW_LEG_MARGIN = 0.6
_CW_COMMAND_RETRY_SECONDS = 0.5
_CW_ZOOM_SETTLE_SECONDS = 1.0
_CW_POSE_TOLERANCE_DEG = 2.0
_CW_POSE_POLL_SECONDS = 0.2
_CW_POSE_STABLE_SAMPLES = 2
_CW_RUNNER_STOP_TIMEOUT_SECONDS = 3.0
_FINAL_ANCHOR_TIMEOUT_SECONDS = 3.0
# Route-point dwell is deliberately not an operator control. It only gives the robot a short,
# usable stationary shot after navigation; non-recording trial runs skip it entirely.
_POINT_DWELL_BASELINE_SECONDS = 7.5
_DWELL_JITTER = (0.70, 1.30)
# Operator-facing timeout configuration was removed. Keep one execution-level value so
# legacy saved routes that still contain 180 seconds cannot silently restore the old behavior.
_ARRIVAL_TIMEOUT_SECONDS = 60.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _randomized_point_dwell_seconds(
    baseline: float = _POINT_DWELL_BASELINE_SECONDS,
) -> float:
    return random.uniform(
        baseline * _DWELL_JITTER[0],
        baseline * _DWELL_JITTER[1],
    )


def _consume_background_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached task's result so a late failure is not reported as unhandled."""
    with suppress(asyncio.CancelledError, Exception):
        task.result()


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
        # Our last commanded pitch/zoom. Zoom has no heartbeat feedback, so we track what we sent
        # to use as the next leg's start; pitch is tracked for the same reason (start continuity).
        self._cw_yaw = 0.0
        self._cw_pitch = 0.0
        self._cw_zoom = 1.0
        self._point_dwell_baseline_seconds = _POINT_DWELL_BASELINE_SECONDS
        self._cw_generation = 0
        self._cw_owner_generation: ContextVar[int | None] = ContextVar(
            f"cruise_camerawork_owner_{id(self)}",
            default=None,
        )
        self._reset_camerawork_runtime()
        # (video_time, yaw, pitch) samples recorded during an auto-camerawork run, written beside
        # the video so the grader can tell a slow pan over a plain surface from a true freeze.
        self._telemetry: list[tuple[float, float, float]] = []
        self._telemetry_task: asyncio.Task[None] | None = None

    def current(self) -> CruiseRun | None:
        return self._run

    @property
    def is_running(self) -> bool:
        return self._starting or (self._task is not None and not self._task.done())

    def _reset_camerawork_runtime(self) -> None:
        """Create one clean planner/runner state for a service or a new cruise."""
        self._cw_generation += 1
        self._cw_phase: str | None = None
        self._cw_phase_deadline = 0.0
        self._cw_pending_anchor_seconds = 0.0
        self._cw_anchor_commanded = False
        self._cw_anchor_zoomed = False
        self._cw_last_quadrant: int | None = None
        self._cw_base_stationary = True
        self._cw_stationary_until = 0.0
        self._cw_base_state_changed = asyncio.Event()
        # Serializes the complete parked zoom-out/return cycle with the transition back to
        # transit.  The route may not declare the base moving (and therefore may not dispatch
        # the next goal) while a stationary-only zoom cycle still owns the lens.
        self._cw_stationary_zoom_lock = asyncio.Lock()
        self._cw_runner_stop = asyncio.Event()
        self._cw_runner_task: asyncio.Task[None] | None = None

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

                camerawork = (
                    self._camerawork_config_provider() if request.auto_camerawork else None
                )
                if camerawork is not None and not camerawork.configured:
                    raise ValueError(
                        "Automatic camerawork is not configured; save it in 镜头设置 first"
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
                self._reset_camerawork_runtime()
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
                        f"Robot returned no paths for map '{map_name}', "
                        "so paths were not verified"
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
            # movement begins. Zoom has no heartbeat feedback, so every run also resets our
            # command-side tracking here.
            if camerawork is not None:
                self._cw_zoom = 1.0
                await self._return_to_anchor(camerawork)

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
                    self.capture.remember_recording_clock(session, confirmation_delay_seconds=self._elapsed(),
                                                         quality="application_estimate")
                except OSError:
                    run.warnings.append("录制对时信息暂存失败；完整视频仍会保存")
            if camerawork is not None:
                self._initialize_camerawork_schedule(camerawork)
                generation = self._cw_generation
                self._cw_runner_task = asyncio.create_task(
                    self._run_owned_camerawork(
                        camerawork,
                        self._cw_runner_stop,
                        generation,
                    )
                )

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
            camerawork_quiesced = await self._stop_camerawork_runner()
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
        if camerawork is not None and not await self._begin_camerawork_transit():
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return
        if self._cancel.is_set():
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return

        # Only announce transit after the stationary zoom gate has released and cancellation has
        # been re-checked. The robot protocol has no stop-motion command, so sending a new goal
        # after the operator cancels would be irreversible.
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
            if camerawork is not None:
                self._set_camerawork_stationary(True)
            raise
        except Exception as exc:
            if camerawork is not None:
                self._set_camerawork_stationary(True)
            await self._fail_segment(run, segment, str(exc))
            return

        if not _goal_accepted(result):
            self._record_capture_transition(run, "goal_rejected")
            if camerawork is not None:
                self._set_camerawork_stationary(True)
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
        if camerawork is not None:
            # Preserve the current phase and in-flight target. The one route-wide runner merely
            # learns that zoom is now safe; it does not redraw a target at the arrival boundary.
            self._set_camerawork_stationary(True)
        if arrival == "canceled":
            segment.status = "skipped"
            segment.departed_at_seconds = self._elapsed()
            return
        if arrival != "done":
            await self._fail_segment(run, segment, f"Robot reported '{arrival}' for this point")
            return

        segment.status = "arrived"
        segment.arrived_at_seconds = segment.arrived_at_seconds if segment.arrived_at_seconds is not None else self._elapsed()
        self._record_capture_transition(run, "goal_done")
        marker = await self.capture.add_marker(
            segment.arrived_at_seconds,
            f"{segment.path_name}#{segment.goal_id}",
        )
        if marker is not None:
            run.markers.append(marker)
        await self._publish_segment("CRUISE_POINT_ARRIVED", run, segment)

        await self._dwell(record=request.record, camerawork=camerawork)

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
        *,
        record: bool,
        camerawork: CameraworkConfig | None,
    ) -> None:
        """Create an internal stationary capture window after a navigation arrival.

        This is not a pause between camera targets and is intentionally absent from the request
        schema. A non-recording one-point trial must return immediately. During a real recording,
        quadrant motion can continue; zoom remains restricted to an anchor phase that overlaps
        this stationary window.
        """
        dwell = (
            _randomized_point_dwell_seconds(self._point_dwell_baseline_seconds)
            if record
            else 0.0
        )
        deadline = time.monotonic() + dwell
        if camerawork is not None:
            self._set_camerawork_stationary(True, until=deadline)
        remaining = max(0.0, deadline - time.monotonic())
        if remaining > 0:
            await self._sleep_or_cancel(remaining)

    def _set_camerawork_stationary(
        self,
        stationary: bool,
        *,
        until: float | None = None,
    ) -> None:
        stationary_until = float(until or 0.0) if stationary else 0.0
        if (
            self._cw_base_stationary == stationary
            and self._cw_stationary_until == stationary_until
        ):
            return
        self._cw_base_stationary = stationary
        self._cw_stationary_until = stationary_until
        self._cw_base_state_changed.set()

    async def _begin_camerawork_transit(self) -> bool:
        """Close parked zoom before navigation; false means cancellation won the gate."""
        async with self._cw_stationary_zoom_lock:
            if self._cancel.is_set():
                return False
            self._set_camerawork_stationary(False)
            return True

    async def _stop_camerawork_runner(self) -> bool:
        """Retire the route-wide camera owner and report whether it actually exited."""
        task = self._cw_runner_task
        self._cw_runner_task = None
        if task is None:
            return True
        # Revoke physical-command and state-mutation ownership, then wake every cooperative wait.
        # Do not cancel here: cancelling while HardwareRobotAdapter is inside websocket.send()
        # deliberately closes the transport because the frame may be partial. That would make
        # the following recording Stop race a reconnect. Once an in-flight command returns, each
        # production checkpoint sees the stale generation and exits without another command.
        self._cw_generation += 1
        self._cw_runner_stop.set()
        self._cw_base_state_changed.set()
        done, _pending = await asyncio.wait(
            {task},
            timeout=_CW_RUNNER_STOP_TIMEOUT_SECONDS,
        )
        if task in done:
            with suppress(asyncio.CancelledError, Exception):
                task.result()
            return True
        # Camera motion must never own the recording lifecycle. Keep the retired task detached
        # (and deliberately uncancelled) so a wedged/slow send cannot tear down the shared socket
        # immediately before finalization sends recording Stop.
        log_event(
            "error",
            "cruise.camerawork.stop_timeout",
            run_id=self._run.id if self._run else None,
            timeout_seconds=_CW_RUNNER_STOP_TIMEOUT_SECONDS,
        )
        task.add_done_callback(_consume_background_task)
        return False

    async def _return_to_final_anchor(self, config: CameraworkConfig) -> None:
        """Issue the post-capture resting command without letting it own run completion."""
        generation = self._cw_generation

        async def command() -> None:
            owner = self._cw_owner_generation.set(generation)
            try:
                await self._return_to_anchor(config, wait=False)
            finally:
                self._cw_owner_generation.reset(owner)

        task = asyncio.create_task(command())
        done, _pending = await asyncio.wait(
            {task},
            timeout=_FINAL_ANCHOR_TIMEOUT_SECONDS,
        )
        if task in done:
            with suppress(asyncio.CancelledError, Exception):
                task.result()
            return

        # Revoke ownership before detaching. A transport that suppresses cancellation may finish
        # its already-written command later, but it cannot update this or a subsequent run.
        self._cw_generation += 1
        task.cancel()
        task.add_done_callback(_consume_background_task)
        log_event(
            "error",
            "cruise.camerawork.final_anchor_timeout",
            run_id=self._run.id if self._run else None,
            timeout_seconds=_FINAL_ANCHOR_TIMEOUT_SECONDS,
        )

    async def _run_owned_camerawork(
        self,
        config: CameraworkConfig,
        stop_requested: asyncio.Event,
        generation: int,
    ) -> None:
        owner = self._cw_owner_generation.set(generation)
        try:
            await self._run_camerawork(config, stop_requested)
        finally:
            self._cw_owner_generation.reset(owner)

    def _camerawork_owner_is_current(self) -> bool:
        """Direct lifecycle calls are allowed; retired runner tasks are not."""
        owner_context = getattr(self, "_cw_owner_generation", None)
        if owner_context is None:
            return True
        owner = owner_context.get()
        return owner is None or owner == self._cw_generation

    @staticmethod
    def _axis_halves(low: int, high: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """Split an inclusive integer range into non-empty lower and upper halves."""
        upper_start = math.ceil((low + high) / 2.0)
        return (low, upper_start - 1), (upper_start, high)

    def _quadrant_bounds(
        self,
        quadrant: int,
        config: CameraworkConfig,
    ) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return one of the four yaw × pitch regions inside the operator's limits."""
        yaw_low, yaw_high = self._axis_halves(config.yaw_min, config.yaw_max)
        pitch_low, pitch_high = self._axis_halves(config.pitch_min, config.pitch_max)
        bounds = {
            1: (yaw_low, pitch_low),
            2: (yaw_high, pitch_low),
            3: (yaw_low, pitch_high),
            4: (yaw_high, pitch_high),
        }
        return bounds[quadrant]

    def _next_quadrant_target(
        self,
        config: CameraworkConfig,
    ) -> tuple[int, tuple[float, float]]:
        """Pick any quadrant except the immediately previous one, then a pose inside it."""
        choices = [quadrant for quadrant in range(1, 5) if quadrant != self._cw_last_quadrant]
        quadrant = random.choice(choices)
        yaw_bounds, pitch_bounds = self._quadrant_bounds(quadrant, config)
        return quadrant, (
            random.randint(*yaw_bounds),
            random.randint(*pitch_bounds),
        )

    def _camerawork_cycle_durations(
        self,
        config: CameraworkConfig,
    ) -> tuple[float, float]:
        """Return the roam budget and full post-arrival anchor hold for one cycle.

        The requested percentage divides deliberate roam/hold time. Travel back to the anchor is
        excluded because the operator's N seconds begins only after the anchor has been reached.
        """
        if config.anchor_time_percent <= 0:
            return math.inf, 0.0
        anchor_seconds = config.anchor_dwell_seconds * random.uniform(
            *_CW_ANCHOR_DWELL_JITTER,
        )
        if config.anchor_time_percent >= 100:
            return 0.0, anchor_seconds
        quadrant_seconds = anchor_seconds * (
            (100.0 - config.anchor_time_percent) / config.anchor_time_percent
        )
        return quadrant_seconds, anchor_seconds

    def _enter_anchor_phase(self, seconds: float) -> None:
        self._cw_phase = _CW_PHASE_ANCHOR
        self._cw_pending_anchor_seconds = seconds
        # There is no anchor deadline while travelling home. It is established only after the
        # move has completed by heartbeat confirmation or its conservative travel budget.
        self._cw_phase_deadline = math.inf
        self._cw_anchor_commanded = False
        self._cw_anchor_zoomed = False
        # Anchor is a fifth timed state, not a reset of quadrant history. The next sweeping
        # target still comes from one of the three quadrants other than the last one used.

    def _start_camerawork_cycle(
        self,
        config: CameraworkConfig,
        starts_at: float,
    ) -> None:
        quadrant_seconds, anchor_seconds = self._camerawork_cycle_durations(config)
        self._cw_pending_anchor_seconds = anchor_seconds
        if config.anchor_time_percent >= 100:
            self._enter_anchor_phase(anchor_seconds)
            return
        self._cw_phase = _CW_PHASE_QUADRANTS
        self._cw_phase_deadline = math.inf if math.isinf(quadrant_seconds) else (
            starts_at + quadrant_seconds
        )
        self._cw_anchor_commanded = False
        self._cw_anchor_zoomed = False

    def _initialize_camerawork_schedule(self, config: CameraworkConfig) -> None:
        """Start a complete cycle; never enter halfway through a promised anchor hold."""
        now = time.monotonic()
        self._cw_last_quadrant = None
        self._start_camerawork_cycle(config, now)

    def _advance_camerawork_schedule(
        self,
        config: CameraworkConfig,
        transition_at: float,
    ) -> None:
        if self._cw_phase == _CW_PHASE_QUADRANTS:
            self._enter_anchor_phase(self._cw_pending_anchor_seconds)
            return
        if config.anchor_time_percent >= 100 and self._cw_anchor_commanded:
            # At 100% the camera never leaves the anchor. Renew the randomized hold/zoom window
            # in place instead of pretending to enter a new anchor state and resending the same
            # yaw/pitch command at every boundary.
            _quadrant_seconds, anchor_seconds = self._camerawork_cycle_durations(config)
            self._cw_pending_anchor_seconds = anchor_seconds
            self._cw_phase_deadline = transition_at + anchor_seconds
            self._cw_anchor_zoomed = False
            return
        self._start_camerawork_cycle(config, transition_at)

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
            if not self._camerawork_owner_is_current() or self._cancel.is_set():
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
        config: CameraworkConfig,
        *,
        quadrant: int | None = None,
        context: str = "cruise_moving",
    ) -> bool:
        """One slow move to a fresh yaw/pitch target; zoom remains fixed.

        A failed gimbal command must never break the cruise, so anything short of a lost
        connection is swallowed. Fresh yaw/pitch feedback can confirm arrival; otherwise the
        travel ÷ speed budget allows both axes time to move before the next command.
        """
        start_yaw = self._current_yaw(config)
        start_pitch = self._current_pitch(config)
        target_yaw, target_pitch = target
        if not self._camerawork_owner_is_current():
            return False
        command = GimbalMoveRequest(
            yaw_start=start_yaw, yaw_end=target_yaw, yaw_speed=speed,
            pitch_start=start_pitch, pitch_end=target_pitch, pitch_speed=speed,
            zoom_start=self._cw_zoom, zoom_end=self._cw_zoom,
        )
        try:
            if not self._camerawork_owner_is_current():
                return False
            await self.robot.set_gimbal(command, context=context)
        except ConnectionError:
            raise
        except Exception:
            return False
        if not self._camerawork_owner_is_current():
            return False
        # Capture the boundary after the command is written so no pre-command heartbeat can
        # be mistaken for feedback to this target.
        heartbeat_revision = self._heartbeat_revision()
        self._cw_yaw = target_yaw
        self._cw_pitch = target_pitch
        if quadrant is not None:
            self._cw_last_quadrant = quadrant
        travel = max(abs(target_yaw - start_yaw), abs(target_pitch - start_pitch))
        budget = (travel / speed + _CW_LEG_MARGIN) if speed > 0 else _CW_LEG_MARGIN
        await self._await_camerawork_pose(
            target_yaw,
            target_pitch,
            budget,
            after_revision=heartbeat_revision,
        )
        return self._camerawork_owner_is_current() and not self._cancel.is_set()

    async def _run_camerawork(
        self,
        config: CameraworkConfig,
        stop_requested: asyncio.Event,
    ) -> None:
        """Own gimbal targets for the complete recording, across travel and point dwell."""
        if self._cw_phase is None:
            self._initialize_camerawork_schedule(config)
        while (
            self._camerawork_owner_is_current()
            and not self._cancel.is_set()
            and not stop_requested.is_set()
        ):
            now = time.monotonic()
            if now >= self._cw_phase_deadline:
                # A leg that began inside the roam budget always gets its complete physical/
                # estimated arrival wait. Start the next state from *now* instead of catching up
                # across stale deadlines: no anchor hold may be shortened or skipped.
                self._advance_camerawork_schedule(config, now)
                continue

            phase_deadline = self._cw_phase_deadline
            if self._cw_phase == _CW_PHASE_QUADRANTS:
                quadrant, target = self._next_quadrant_target(config)
                speed = random.randint(config.speed_min, config.speed_max)
                moved = await self._camerawork_leg(
                    target,
                    speed,
                    config,
                    quadrant=quadrant,
                    context=(
                        "cruise_stationary_camerawork"
                        if self._cw_base_stationary else "cruise_moving"
                    ),
                )
                if not self._camerawork_owner_is_current():
                    return
                if not moved:
                    await self._wait_for_camerawork_boundary(
                        min(
                            _CW_COMMAND_RETRY_SECONDS,
                            max(0.0, phase_deadline - time.monotonic()),
                        ),
                        stop_requested,
                    )
                continue

            if not self._cw_anchor_commanded:
                moved = await self._camerawork_leg(
                    (config.anchor_yaw, config.anchor_pitch),
                    config.speed_max,
                    config,
                    context=(
                        "cruise_stationary_camerawork"
                        if self._cw_base_stationary else "cruise_moving"
                    ),
                )
                if not self._camerawork_owner_is_current():
                    return
                if moved:
                    self._cw_anchor_commanded = True
                    # This is the only place the N±30% anchor timer starts. The preceding
                    # heartbeat/estimated travel wait is therefore never charged to the hold.
                    self._cw_phase_deadline = (
                        time.monotonic() + self._cw_pending_anchor_seconds
                    )
                    phase_deadline = self._cw_phase_deadline
                else:
                    await self._wait_for_camerawork_boundary(
                        _CW_COMMAND_RETRY_SECONDS,
                        stop_requested,
                    )
                    continue

            zoom_deadline = min(phase_deadline, self._cw_stationary_until)
            if (
                self._cw_base_stationary
                and not self._cw_anchor_zoomed
                and zoom_deadline > time.monotonic()
            ):
                handled = await self._parked_zoom_and_anchor(
                    config,
                    deadline=zoom_deadline,
                )
                if not self._camerawork_owner_is_current():
                    return
                if handled:
                    self._cw_anchor_zoomed = True
                else:
                    await self._wait_for_camerawork_boundary(
                        min(
                            _CW_COMMAND_RETRY_SECONDS,
                            max(0.0, zoom_deadline - time.monotonic()),
                        ),
                        stop_requested,
                    )
                    continue

            remaining = phase_deadline - time.monotonic()
            if remaining > 0:
                await self._wait_for_camerawork_boundary(remaining, stop_requested)

    async def _wait_for_camerawork_boundary(
        self,
        seconds: float,
        stop_requested: asyncio.Event,
    ) -> None:
        """Wait for a phase edge while remaining interruptible by arrival or cancellation."""
        if not self._camerawork_owner_is_current():
            return
        base_state_changed = self._cw_base_state_changed
        local_stop = asyncio.create_task(stop_requested.wait())
        canceled = asyncio.create_task(self._cancel.wait())
        base_changed = asyncio.create_task(base_state_changed.wait())
        try:
            await asyncio.wait(
                {local_stop, canceled, base_changed},
                timeout=max(0.0, seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if base_changed.done():
                base_state_changed.clear()
            for task in (local_stop, canceled, base_changed):
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    def _parked_zoom_target(self, config: CameraworkConfig) -> float:
        """Choose a visible zoom away from the anchor; this is called only after arrival."""
        midpoint = (config.zoom_min + config.zoom_max) / 2.0
        if config.anchor_zoom <= midpoint:
            return round(random.uniform(midpoint, config.zoom_max), 2)
        return round(random.uniform(config.zoom_min, midpoint), 2)

    async def _parked_zoom_and_anchor(
        self,
        config: CameraworkConfig,
        *,
        deadline: float = math.inf,
    ) -> bool:
        """Confirm the anchor and fit a complete zoom-out/return inside the parked window."""
        async with self._cw_stationary_zoom_lock:
            if not self._camerawork_owner_is_current():
                return False
            # The base can become moving while this coroutine is waiting to acquire ownership.
            # Re-check inside the gate so a stale stationary observation cannot emit a zoom.
            if not self._cw_base_stationary:
                return False
            return await self._parked_zoom_and_anchor_locked(config, deadline=deadline)

    async def _parked_zoom_and_anchor_locked(
        self,
        config: CameraworkConfig,
        *,
        deadline: float,
    ) -> bool:
        """Run one stationary-only anchor/zoom cycle while holding the transit gate."""
        if not self._camerawork_owner_is_current() or deadline <= time.monotonic():
            return False
        anchored = await self._return_to_anchor(config, deadline=deadline)
        if not self._camerawork_owner_is_current() or self._cancel.is_set():
            return False
        if not anchored:
            # Keep the last command pointed at the anchor, but do not add zoom motion to a pose
            # that the physical feedback says is still somewhere unsafe.
            return False

        # A partial zoom is worse than no zoom. Reserve one settle interval for each direction;
        # short anchor/dwell windows simply hold the confirmed anchor composition.
        if (
            not self._cw_base_stationary
            or deadline - time.monotonic() < 2 * _CW_ZOOM_SETTLE_SECONDS
        ):
            return True

        # Arrival/dwell may have set the transition event before anchor confirmation finished.
        # Consume that old edge, then re-check the live state so only a *new* departure can
        # interrupt the zoom settle below.
        self._cw_base_state_changed.clear()
        if (
            not self._cw_base_stationary
            or deadline - time.monotonic() < 2 * _CW_ZOOM_SETTLE_SECONDS
        ):
            return True

        zoom_target = self._parked_zoom_target(config)
        try:
            if not self._camerawork_owner_is_current():
                return False
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
                await self._return_to_anchor(config, wait=False)
            return False
        if not self._camerawork_owner_is_current():
            return False
        self._cw_yaw = config.anchor_yaw
        self._cw_pitch = config.anchor_pitch
        self._cw_zoom = zoom_target
        await self._wait_for_camerawork_boundary(
            min(_CW_ZOOM_SETTLE_SECONDS, max(0.0, deadline - time.monotonic())),
            self._cw_runner_stop,
        )
        if not self._camerawork_owner_is_current() or self._cancel.is_set():
            return False
        if not self._cw_base_stationary or deadline <= time.monotonic():
            # Departure or an unexpectedly slow command must not leave zoom away from its anchor.
            with suppress(Exception):
                await self._return_to_anchor(config, wait=False)
            return True
        returned = await self._return_to_anchor(config, deadline=deadline)
        if not self._camerawork_owner_is_current():
            return False
        if not returned:
            with suppress(Exception):
                await self._return_to_anchor(config, wait=False)
        return returned

    async def _return_to_anchor(
        self,
        config: CameraworkConfig,
        *,
        wait: bool = True,
        deadline: float = math.inf,
    ) -> bool:
        """Command the complete resting pose and confirm physical yaw/pitch when available."""
        attempts = 2 if wait else 1
        ever_observed_conflict = False
        for attempt in range(attempts):
            if not self._camerawork_owner_is_current():
                return False
            if wait and deadline <= time.monotonic():
                return False
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
            if not self._camerawork_owner_is_current():
                return False
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
            full_budget = max(0.3, travel / speed + _CW_LEG_MARGIN)
            budget = max(0.0, min(full_budget, deadline - time.monotonic()))
            reached, observed = await self._await_camerawork_pose(
                config.anchor_yaw,
                config.anchor_pitch,
                budget,
                after_revision=heartbeat_revision,
            )
            if not self._camerawork_owner_is_current():
                return False
            if start_zoom != config.anchor_zoom:
                remaining_zoom = _CW_ZOOM_SETTLE_SECONDS - (time.monotonic() - started)
                if remaining_zoom > 0:
                    await self._sleep_or_cancel(
                        min(remaining_zoom, max(0.0, deadline - time.monotonic()))
                    )
            if reached:
                return True
            if observed:
                ever_observed_conflict = True
            if (
                not observed
                and not ever_observed_conflict
                and budget >= full_budget - 1e-6
            ):
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
            or (
                state is not None
                and not state.recording
                and self._recording_status_known()
            )
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
                run.warnings.append(
                    "录制尚未确认停止，或文件未同步到本地；拍摄会话已保留以便重试"
                )

        # Capture lifecycle always wins over cosmetic resting position. Closing (or preserving)
        # the recovery session first prevents a wedged gimbal transport from leaving the UI and
        # recording lifecycle open. A runner that did not stop may still own the adapter gate, so
        # do not compete with it. Even after a clean stop, the one-way final command is bounded.
        if camerawork is not None and recording_is_stopped and camerawork_quiesced:
            await self._return_to_final_anchor(camerawork)

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
            session.recording_clock.update(origin="record_write_estimate", quality="application_estimate")
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
        if any(e["type"] == kind and e.get("visit_index") == index for e in session.recording_events):
            return
        seconds = max(0.0, event["monotonic"] - self._origin_monotonic)
        if segment is not None:
            if kind == "goal_write":
                segment.transit_start_seconds = seconds
            elif kind == "goal_done":
                segment.arrived_at_seconds = seconds
        try:
            self.capture.remember_recording_event(session, {
                "type": kind, "seconds": seconds, "visit_index": index,
                "sequence": len(session.recording_events),
            })
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
