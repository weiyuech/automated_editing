import asyncio
import math
import random
from contextvars import ContextVar
from types import SimpleNamespace

import pytest

import automated_video_editing_backend.services.cruise as cruise_module
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraworkConfig,
    RobotState,
)
from automated_video_editing_backend.services.cruise import CruiseService, _clamp
from automated_video_editing_backend.services.robot import HardwareRobotAdapter


def _service():
    # Only pure scheduling/target selection is exercised by most tests, so no robot is needed.
    service = CruiseService.__new__(CruiseService)
    service._cancel = asyncio.Event()
    service._cw_last_quadrant = None
    service._cw_yaw = 0.0
    service._cw_pitch = 0.0
    service._cw_zoom = 1.0
    service._cw_phase = None
    service._cw_phase_deadline = 0.0
    service._cw_pending_anchor_seconds = 0.0
    service._cw_anchor_commanded = False
    service._cw_anchor_zoomed = False
    service._cw_base_stationary = True
    service._cw_stationary_until = 0.0
    service._cw_base_state_changed = asyncio.Event()
    service._cw_stationary_zoom_lock = asyncio.Lock()
    service._cw_runner_stop = asyncio.Event()
    service._cw_runner_task = None
    service._cw_generation = 1
    service._cw_owner_generation = ContextVar(
        f"test_camerawork_owner_{id(service)}",
        default=None,
    )
    service._point_dwell_baseline_seconds = 7.5
    return service


def _config(**overrides):
    values = {
        "configured": True,
        "anchor_yaw": 0,
        "anchor_pitch": 0,
        "anchor_zoom": 1,
        "yaw_min": -15,
        "yaw_max": 15,
        "pitch_min": -8,
        "pitch_max": 10,
        "zoom_min": 1,
        "zoom_max": 1.5,
        "speed_min": 2,
        "speed_max": 5,
        "anchor_time_percent": 20,
        "anchor_dwell_seconds": 5,
    }
    values.update(overrides)
    return CameraworkConfig(**values)


@pytest.mark.parametrize(
    ("yaw_min", "yaw_max", "pitch_min", "pitch_max"),
    [(-60, 60, -15, 15), (-10, 50, -40, 10), (20, 60, -60, -20),
     (-5, 5, 3, 4), (0, 1, 0, 1)],
)
def test_every_quadrant_is_nonempty_and_stays_inside_asymmetric_or_narrow_limits(
    yaw_min, yaw_max, pitch_min, pitch_max,
):
    service = _service()
    config = _config(
        yaw_min=yaw_min, yaw_max=yaw_max, anchor_yaw=yaw_min,
        pitch_min=pitch_min, pitch_max=pitch_max, anchor_pitch=pitch_min,
    )
    seen = set()
    for quadrant in range(1, 5):
        (yaw_low, yaw_high), (pitch_low, pitch_high) = service._quadrant_bounds(
            quadrant, config,
        )
        assert yaw_min <= yaw_low <= yaw_high <= yaw_max
        assert pitch_min <= pitch_low <= pitch_high <= pitch_max
        seen.update((yaw, pitch) for yaw in range(yaw_low, yaw_high + 1)
                    for pitch in range(pitch_low, pitch_high + 1))

    assert seen == {
        (yaw, pitch)
        for yaw in range(yaw_min, yaw_max + 1)
        for pitch in range(pitch_min, pitch_max + 1)
    }


def test_next_target_excludes_only_the_previous_quadrant(monkeypatch):
    service = _service()
    service._cw_last_quadrant = 1

    def choose(choices):
        assert choices == [2, 3, 4]
        return 4

    monkeypatch.setattr(random, "choice", choose)
    quadrant, target = service._next_quadrant_target(_config())

    assert quadrant == 4
    (yaw_low, yaw_high), (pitch_low, pitch_high) = service._quadrant_bounds(
        quadrant, _config(),
    )
    assert yaw_low <= target[0] <= yaw_high
    assert pitch_low <= target[1] <= pitch_high


def test_one_four_one_four_sequence_is_valid_but_same_quadrant_twice_is_not(monkeypatch):
    service = _service()
    planned = iter((1, 4, 1, 4))

    def choose(choices):
        quadrant = next(planned)
        assert quadrant in choices
        if service._cw_last_quadrant is not None:
            assert service._cw_last_quadrant not in choices
        return quadrant

    monkeypatch.setattr(random, "choice", choose)
    actual = []
    for _ in range(4):
        quadrant, _target = service._next_quadrant_target(_config())
        actual.append(quadrant)
        service._cw_last_quadrant = quadrant

    assert actual == [1, 4, 1, 4]


@pytest.mark.asyncio
async def test_failed_gimbal_command_does_not_advance_quadrant_history():
    class FailingRobot:
        def heartbeat_yaw(self):
            return 0.0

        def heartbeat_pitch(self):
            return 0.0

        async def set_gimbal(self, *_args, **_kwargs):
            raise ValueError("firmware refused command")

    service = CruiseService(EventHub(), FailingRobot(), object())
    service._cw_last_quadrant = 1

    moved = await service._camerawork_leg(
        (12, 5), 2, _config(), quadrant=4,
    )

    assert moved is False
    assert service._cw_last_quadrant == 1
    assert (service._cw_yaw, service._cw_pitch) == (0.0, 0.0)


def test_twenty_percent_anchor_with_five_seconds_means_twenty_plus_five(monkeypatch):
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)

    quadrant_seconds, anchor_seconds = _service()._camerawork_cycle_durations(
        _config(anchor_time_percent=20, anchor_dwell_seconds=5),
    )

    assert quadrant_seconds == pytest.approx(20.0)
    assert anchor_seconds == pytest.approx(5.0)
    assert anchor_seconds / (quadrant_seconds + anchor_seconds) == pytest.approx(0.20)


@pytest.mark.parametrize(("jitter", "expected"), [(0.70, 3.5), (1.30, 6.5)])
def test_anchor_duration_has_hidden_thirty_percent_jitter(monkeypatch, jitter, expected):
    monkeypatch.setattr(random, "uniform", lambda low, high: jitter)

    quadrant_seconds, anchor_seconds = _service()._camerawork_cycle_durations(
        _config(anchor_time_percent=20, anchor_dwell_seconds=5),
    )

    assert anchor_seconds == pytest.approx(expected)
    assert quadrant_seconds == pytest.approx(expected * 4)


@pytest.mark.parametrize(
    ("percent", "expected_quadrants", "expected_anchor"),
    [(0, math.inf, 0.0), (100, 0.0, 5.0)],
)
def test_zero_and_hundred_percent_have_unambiguous_schedule_edges(
    monkeypatch, percent, expected_quadrants, expected_anchor,
):
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)

    quadrant_seconds, anchor_seconds = _service()._camerawork_cycle_durations(
        _config(anchor_time_percent=percent, anchor_dwell_seconds=5),
    )

    assert quadrant_seconds == expected_quadrants
    assert anchor_seconds == expected_anchor


def test_schedule_always_starts_with_a_complete_roam_budget(monkeypatch):
    service = _service()
    clock = iter((100.0,))
    monkeypatch.setattr(cruise_module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)

    service._initialize_camerawork_schedule(
        _config(anchor_time_percent=20, anchor_dwell_seconds=5),
    )

    assert service._cw_phase == "quadrants"
    assert service._cw_phase_deadline == pytest.approx(120.0)
    assert service._cw_pending_anchor_seconds == pytest.approx(5.0)
    assert service._cw_anchor_commanded is False


@pytest.mark.asyncio
async def test_quadrant_runner_starts_next_leg_immediately_after_full_leg_wait(monkeypatch):
    service = _service()
    config = _config(anchor_time_percent=0)
    targets = iter(((1, (-10.0, -4.0)), (4, (10.0, 4.0))))
    calls = []

    monkeypatch.setattr(service, "_next_quadrant_target", lambda _config: next(targets))

    async def complete_leg(target, speed, _config, **kwargs):
        calls.append((target, speed, kwargs["quadrant"]))
        if len(calls) == 2:
            service._cw_runner_stop.set()
        return True

    monkeypatch.setattr(service, "_camerawork_leg", complete_leg)

    await service._run_camerawork(config, service._cw_runner_stop)

    assert [call[0] for call in calls] == [(-10.0, -4.0), (10.0, 4.0)]
    assert [call[2] for call in calls] == [1, 4]


@pytest.mark.asyncio
async def test_anchor_hold_timer_starts_only_after_anchor_arrival_wait(monkeypatch):
    service = _service()
    config = _config(anchor_time_percent=100, anchor_dwell_seconds=5)
    service._cw_phase = "anchor"
    service._cw_phase_deadline = math.inf
    service._cw_pending_anchor_seconds = 5.0
    service._cw_base_stationary = False
    now = [100.0]
    waits = []
    moves = []

    monkeypatch.setattr(cruise_module.time, "monotonic", lambda: now[0])

    async def arrive_after_feedback_or_budget(*_args, **_kwargs):
        moves.append("anchor")
        now[0] = 103.0
        return True

    async def record_hold(seconds, _stop_requested):
        waits.append(seconds)
        service._cw_runner_stop.set()

    monkeypatch.setattr(service, "_camerawork_leg", arrive_after_feedback_or_budget)
    monkeypatch.setattr(service, "_wait_for_camerawork_boundary", record_hold)

    await service._run_camerawork(config, service._cw_runner_stop)

    assert service._cw_anchor_commanded is True
    assert service._cw_phase_deadline == pytest.approx(108.0)
    assert moves == ["anchor"]
    assert waits == [pytest.approx(5.0)]


@pytest.mark.asyncio
async def test_hundred_percent_renews_holds_without_resending_anchor(monkeypatch):
    service = _service()
    config = _config(anchor_time_percent=100, anchor_dwell_seconds=5)
    service._cw_base_stationary = False
    now = [100.0]
    moves = []
    waits = []

    monkeypatch.setattr(cruise_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)

    async def initial_anchor_only(target, _speed, _config, **_kwargs):
        moves.append(target)
        return True

    async def finish_hold(seconds, _stop_requested):
        waits.append(seconds)
        now[0] += seconds
        if len(waits) == 3:
            service._cw_runner_stop.set()

    monkeypatch.setattr(service, "_camerawork_leg", initial_anchor_only)
    monkeypatch.setattr(service, "_wait_for_camerawork_boundary", finish_hold)

    await service._run_camerawork(config, service._cw_runner_stop)

    assert moves == [(0, 0)]
    assert waits == [pytest.approx(5.0)] * 3
    assert service._cw_anchor_commanded is True
    assert service._cw_phase_deadline == pytest.approx(115.0)


@pytest.mark.asyncio
async def test_slow_roam_leg_finishes_then_anchor_gets_a_full_post_arrival_hold(monkeypatch):
    service = _service()
    config = _config(anchor_time_percent=20, anchor_dwell_seconds=5)
    service._cw_base_stationary = False
    now = [100.0]
    moves = []
    waits = []

    monkeypatch.setattr(cruise_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(random, "uniform", lambda low, high: 1.0)
    monkeypatch.setattr(
        service,
        "_next_quadrant_target",
        lambda _config: (1, (-15.0, -8.0)),
    )

    async def finish_each_leg(target, _speed, _config, **_kwargs):
        moves.append((service._cw_phase, target, _kwargs.get("quadrant")))
        # The roam leg is allowed to finish past its 120s soft boundary. Returning home then
        # consumes another three seconds before the five-second hold clock may begin.
        now[0] = 125.0 if len(moves) == 1 else 128.0
        return True

    async def record_hold(seconds, _stop_requested):
        waits.append(seconds)
        service._cw_runner_stop.set()

    monkeypatch.setattr(service, "_camerawork_leg", finish_each_leg)
    monkeypatch.setattr(service, "_wait_for_camerawork_boundary", record_hold)

    await service._run_camerawork(config, service._cw_runner_stop)

    assert moves == [
        ("quadrants", (-15.0, -8.0), 1),
        ("anchor", (0, 0), None),
    ]
    assert service._cw_phase_deadline == pytest.approx(133.0)
    assert waits == [pytest.approx(5.0)]


class _ParkedRobot:
    def __init__(self):
        self.state = RobotState(connected=True, yaw=8.0, pitch=4.0)
        self.commands = []
        self.contexts = []

    def heartbeat_yaw(self):
        return self.state.yaw

    def heartbeat_pitch(self):
        return self.state.pitch

    async def status(self):
        return self.state

    async def set_gimbal(self, command, *, context="manual"):
        self.commands.append(command)
        self.contexts.append(context)
        self.state.yaw = command.yaw_end
        self.state.pitch = command.pitch_end
        return self.state


@pytest.mark.asyncio
async def test_moving_quadrant_leg_keeps_zoom_fixed(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())
    service._cw_zoom = 1.37
    budgets = []

    async def reached(_yaw, _pitch, budget, **_kwargs):
        budgets.append(budget)
        return True, True

    monkeypatch.setattr(service, "_await_camerawork_pose", reached)
    moved = await service._camerawork_leg(
        (-12, -4), 2, _config(), quadrant=1, context="cruise_moving",
    )

    assert moved is True
    assert robot.contexts == ["cruise_moving"]
    assert robot.commands[0].zoom_start == pytest.approx(1.37)
    assert robot.commands[0].zoom_end == pytest.approx(1.37)
    assert service._cw_zoom == pytest.approx(1.37)
    assert service._cw_last_quadrant == 1
    assert budgets == [pytest.approx(10.6)]


@pytest.mark.asyncio
async def test_cancel_during_leg_does_not_begin_a_followup_phase(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())

    async def canceled_while_waiting(*_args, **_kwargs):
        service._cancel.set()
        return False, False

    monkeypatch.setattr(service, "_await_camerawork_pose", canceled_while_waiting)

    assert await service._camerawork_leg((0, 0), 2, _config()) is False


@pytest.mark.asyncio
async def test_parked_phase_holds_yaw_pitch_while_zooming_then_returns_full_anchor(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())
    service._cw_yaw = 8.0
    service._cw_pitch = 4.0
    service._cw_zoom = 1.0

    async def no_wait(*_args, **_kwargs):
        return None

    async def reached(*_args, **_kwargs):
        return True, True

    monkeypatch.setattr(service, "_wait_for_camerawork_boundary", no_wait)
    monkeypatch.setattr(service, "_sleep_or_cancel", no_wait)
    monkeypatch.setattr(service, "_await_camerawork_pose", reached)
    config = _config(anchor_yaw=0, anchor_pitch=0, anchor_zoom=1)
    anchored = await service._parked_zoom_and_anchor(config)

    arrive_home, zoom, final_home = robot.commands
    assert arrive_home.yaw_end == config.anchor_yaw
    assert arrive_home.pitch_end == config.anchor_pitch
    assert zoom.yaw_start == zoom.yaw_end == config.anchor_yaw
    assert zoom.pitch_start == zoom.pitch_end == config.anchor_pitch
    assert config.zoom_min <= zoom.zoom_end <= config.zoom_max
    assert final_home.yaw_end == config.anchor_yaw
    assert final_home.pitch_end == config.anchor_pitch
    assert final_home.zoom_end == config.anchor_zoom
    assert robot.contexts == [
        "cruise_stationary_anchor",
        "cruise_stationary_zoom",
        "cruise_stationary_anchor",
    ]
    assert anchored is True


@pytest.mark.asyncio
async def test_anchor_return_retries_once_when_physical_feedback_misses(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())
    service._cw_yaw = 8.0
    service._cw_pitch = 4.0
    service._cw_zoom = 1.0
    results = iter(((False, True), (True, True)))

    async def feedback(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(service, "_await_camerawork_pose", feedback)
    reached = await service._return_to_anchor(_config())

    assert reached is True
    assert len(robot.commands) == 2


@pytest.mark.asyncio
async def test_anchor_return_never_turns_a_known_miss_into_no_feedback_success(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())
    results = iter(((False, True), (False, False)))

    async def feedback(*_args, **_kwargs):
        return next(results)

    monkeypatch.setattr(service, "_await_camerawork_pose", feedback)

    assert await service._return_to_anchor(_config()) is False
    assert len(robot.commands) == 2


@pytest.mark.asyncio
async def test_anchor_return_uses_one_timed_fallback_when_pose_feedback_is_unavailable(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())

    async def unavailable(*_args, **_kwargs):
        return False, False

    monkeypatch.setattr(service, "_await_camerawork_pose", unavailable)
    reached = await service._return_to_anchor(_config())

    assert reached is True
    assert len(robot.commands) == 1


@pytest.mark.asyncio
async def test_cached_split_axes_before_a_command_cannot_confirm_a_new_target():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    # Some firmware may split the two axes across packets. Together these leave a plausible
    # cached pose. It is a valid logical sample, but both packets arrived before the command
    # boundary represented by ``after_revision``.
    adapter._apply_protocol_state({"gimbal": {"yaw": 0}})
    adapter._apply_protocol_state({"gimbal": {"pitch": 0}})
    assert adapter.heartbeat_yaw() == 0
    assert adapter.heartbeat_pitch() == 0
    assert adapter.heartbeat_revision() == 1

    service = CruiseService(EventHub(), adapter, object())
    reached, observed = await service._await_camerawork_pose(
        0,
        0,
        budget=0.6,
        after_revision=adapter.heartbeat_revision(),
    )

    assert (reached, observed) == (False, False)


@pytest.mark.asyncio
async def test_post_command_split_axes_form_complete_samples_without_using_old_values(
    monkeypatch,
):
    import automated_video_editing_backend.services.cruise as cruise_module

    monkeypatch.setattr(cruise_module, "_CW_POSE_POLL_SECONDS", 0.01)
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter._apply_protocol_state({"gimbal": {"yaw": 9}})
    adapter._apply_protocol_state({"gimbal": {"pitch": 6}})
    baseline = adapter.heartbeat_revision()
    service = CruiseService(EventHub(), adapter, object())

    waiter = asyncio.create_task(service._await_camerawork_pose(
        0,
        0,
        budget=0.5,
        after_revision=baseline,
    ))
    await asyncio.sleep(0.02)
    adapter._apply_protocol_state({"gimbal": {"yaw": 4}})
    adapter._apply_protocol_state({"gimbal": {"pitch": 3}})  # complete, but off target
    await asyncio.sleep(0.03)
    adapter._apply_protocol_state({"gimbal": {"yaw": 0}})
    adapter._apply_protocol_state({"gimbal": {"pitch": 0}})  # first stable sample
    await asyncio.sleep(0.03)
    adapter._apply_protocol_state({"gimbal": {"yaw": 0}})
    adapter._apply_protocol_state({"gimbal": {"pitch": 0}})  # second stable sample

    assert await waiter == (True, True)


@pytest.mark.asyncio
async def test_parked_zoom_is_skipped_when_physical_anchor_cannot_be_confirmed(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())

    async def missed(_config, *, wait=True, deadline=math.inf):
        assert wait is True
        assert deadline == math.inf
        return False

    monkeypatch.setattr(service, "_return_to_anchor", missed)

    assert await service._parked_zoom_and_anchor(_config()) is False
    assert robot.commands == []


@pytest.mark.asyncio
async def test_short_parked_window_holds_anchor_without_starting_an_incomplete_zoom(
    monkeypatch,
):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())
    anchor_calls = []

    async def anchored(_config, *, wait=True, deadline=math.inf):
        anchor_calls.append((wait, deadline))
        return True

    monkeypatch.setattr(service, "_return_to_anchor", anchored)
    deadline = asyncio.get_running_loop().time() + 0.05

    assert await service._parked_zoom_and_anchor(_config(), deadline=deadline) is True
    assert anchor_calls == [(True, deadline)]
    assert robot.commands == []


@pytest.mark.asyncio
async def test_transit_waits_until_the_stationary_zoom_cycle_releases_ownership(monkeypatch):
    service = _service()
    service._cw_base_stationary = True
    zoom_started = asyncio.Event()
    release_zoom = asyncio.Event()

    async def controlled_zoom(_config, *, deadline):
        assert deadline > asyncio.get_running_loop().time()
        assert service._cw_base_stationary is True
        zoom_started.set()
        await release_zoom.wait()
        assert service._cw_base_stationary is True
        return True

    monkeypatch.setattr(service, "_parked_zoom_and_anchor_locked", controlled_zoom)
    zoom = asyncio.create_task(service._parked_zoom_and_anchor(
        _config(),
        deadline=asyncio.get_running_loop().time() + 1,
    ))
    await zoom_started.wait()
    transit = asyncio.create_task(service._begin_camerawork_transit())
    await asyncio.sleep(0)

    assert transit.done() is False
    assert service._cw_base_stationary is True

    release_zoom.set()
    assert await zoom is True
    assert await transit is True
    assert service._cw_base_stationary is False


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_zoom_gate_skips_goal_and_dispatch_event(monkeypatch):
    service = _service()
    service._origin_monotonic = asyncio.get_running_loop().time()
    goal_calls = []
    published = []

    async def set_goal(command):
        goal_calls.append(command)
        return {"goal_check": True}

    async def publish(event, _run, _segment):
        published.append(event)

    service.robot = SimpleNamespace(set_goal=set_goal)
    monkeypatch.setattr(service, "_publish_segment", publish)
    await service._cw_stationary_zoom_lock.acquire()
    segment = SimpleNamespace(status="pending", departed_at_seconds=None)
    task = asyncio.create_task(service._run_segment(
        SimpleNamespace(),
        SimpleNamespace(),
        segment,
        _config(),
    ))
    await asyncio.sleep(0)

    service._cancel.set()
    service._cw_stationary_zoom_lock.release()
    await task

    assert segment.status == "skipped"
    assert service._cw_base_stationary is True
    assert goal_calls == []
    assert published == []


@pytest.mark.asyncio
async def test_zoom_cycle_rechecks_stationary_state_inside_the_transit_gate(monkeypatch):
    service = _service()
    service._cw_base_stationary = False
    entered = False

    async def should_not_run(*_args, **_kwargs):
        nonlocal entered
        entered = True
        return True

    monkeypatch.setattr(service, "_parked_zoom_and_anchor_locked", should_not_run)

    assert await service._parked_zoom_and_anchor(_config()) is False
    assert entered is False


@pytest.mark.asyncio
async def test_anchor_feedback_wait_cannot_overrun_its_phase_deadline():
    robot = _ParkedRobot()
    robot.state.yaw = 15
    robot.state.pitch = 10
    service = CruiseService(EventHub(), robot, object())
    started = asyncio.get_running_loop().time()
    deadline = started + 0.05

    assert await service._return_to_anchor(_config(), deadline=deadline) is False

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.15
    assert len(robot.commands) == 1


@pytest.mark.asyncio
async def test_arrival_state_change_does_not_cancel_or_retarget_the_active_sweep(monkeypatch):
    service = _service()
    config = _config(anchor_time_percent=0)
    service._initialize_camerawork_schedule(config)
    service._set_camerawork_stationary(False)
    entered = asyncio.Event()
    release = asyncio.Event()
    commands = []

    async def slow_leg(target, speed, _config, **kwargs):
        commands.append((target, speed, kwargs["context"]))
        entered.set()
        await release.wait()
        return True

    monkeypatch.setattr(service, "_camerawork_leg", slow_leg)
    runner = asyncio.create_task(service._run_camerawork(config, service._cw_runner_stop))
    await entered.wait()

    service._set_camerawork_stationary(True, until=asyncio.get_running_loop().time() + 1)
    await asyncio.sleep(0)
    assert len(commands) == 1

    service._cw_runner_stop.set()
    release.set()
    await runner
    assert len(commands) == 1


@pytest.mark.asyncio
async def test_stop_runner_cooperatively_retires_an_inflight_gimbal_send(monkeypatch):
    command_entered = asyncio.Event()
    release_command = asyncio.Event()
    cancellation_seen = asyncio.Event()

    class CancellationSensitiveRobot:
        def heartbeat_yaw(self):
            return None

        def heartbeat_pitch(self):
            return None

        def heartbeat_revision(self):
            return None

        async def set_gimbal(self, _command, *, context="manual"):
            del context
            command_entered.set()
            try:
                await release_command.wait()
            except asyncio.CancelledError:
                # HardwareRobotAdapter closes its websocket at this exact boundary because a
                # cancelled send may have written a partial frame.
                cancellation_seen.set()
                raise
            return RobotState(connected=True)

    monkeypatch.setattr(cruise_module, "_CW_RUNNER_STOP_TIMEOUT_SECONDS", 0.2)
    service = CruiseService(EventHub(), CancellationSensitiveRobot(), object())
    config = _config(anchor_time_percent=0)
    service._initialize_camerawork_schedule(config)
    generation = service._cw_generation
    task = asyncio.create_task(
        service._run_owned_camerawork(config, service._cw_runner_stop, generation)
    )
    service._cw_runner_task = task

    await command_entered.wait()
    stopping = asyncio.create_task(service._stop_camerawork_runner())
    await asyncio.sleep(0)

    assert stopping.done() is False
    assert cancellation_seen.is_set() is False

    release_command.set()
    assert await stopping is True
    assert task.done() is True
    assert cancellation_seen.is_set() is False


@pytest.mark.asyncio
async def test_retired_owned_runner_cannot_command_or_mutate_a_new_generation(monkeypatch):
    command_entered = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_command = asyncio.Event()

    class CancellationResistantRobot:
        def __init__(self):
            self.commands = []

        def heartbeat_yaw(self):
            return None

        def heartbeat_pitch(self):
            return None

        def heartbeat_revision(self):
            return None

        async def set_gimbal(self, command, *, context="manual"):
            self.commands.append((command, context))
            if len(self.commands) == 1:
                command_entered.set()
                try:
                    await release_command.wait()
                except asyncio.CancelledError:
                    # Model a transport/dependency that suppresses cancellation and completes
                    # later. The retired owner must stop at the post-await generation check.
                    cancellation_seen.set()
                    await release_command.wait()
            return RobotState(connected=True)

    monkeypatch.setattr(cruise_module, "_CW_RUNNER_STOP_TIMEOUT_SECONDS", 0.01)
    robot = CancellationResistantRobot()
    service = CruiseService(EventHub(), robot, object())
    config = _config(anchor_time_percent=0)
    service._initialize_camerawork_schedule(config)
    old_generation = service._cw_generation
    old_stop = service._cw_runner_stop
    old_task = asyncio.create_task(
        service._run_owned_camerawork(config, old_stop, old_generation)
    )
    service._cw_runner_task = old_task

    try:
        await command_entered.wait()
        quiesced = await service._stop_camerawork_runner()

        assert quiesced is False
        assert cancellation_seen.is_set() is False
        assert old_task.done() is False

        service._reset_camerawork_runtime()
        new_event = service._cw_base_state_changed
        new_event.set()
        expected_state = {
            "generation": service._cw_generation,
            "phase": "new-run-phase",
            "deadline": 12345.0,
            "yaw": 71.0,
            "pitch": -6.0,
            "zoom": 1.35,
            "quadrant": 4,
        }
        service._cw_phase = expected_state["phase"]
        service._cw_phase_deadline = expected_state["deadline"]
        service._cw_yaw = expected_state["yaw"]
        service._cw_pitch = expected_state["pitch"]
        service._cw_zoom = expected_state["zoom"]
        service._cw_last_quadrant = expected_state["quadrant"]

        release_command.set()
        await asyncio.wait_for(old_task, timeout=0.2)

        assert len(robot.commands) == 1
        assert service._cw_generation == expected_state["generation"]
        assert service._cw_phase == expected_state["phase"]
        assert service._cw_phase_deadline == expected_state["deadline"]
        assert service._cw_yaw == expected_state["yaw"]
        assert service._cw_pitch == expected_state["pitch"]
        assert service._cw_zoom == expected_state["zoom"]
        assert service._cw_last_quadrant == expected_state["quadrant"]
        assert service._cw_base_state_changed is new_event
        assert new_event.is_set() is True
    finally:
        release_command.set()
        if not old_task.done():
            await asyncio.wait_for(old_task, timeout=0.2)


@pytest.mark.asyncio
async def test_failed_anchor_command_retries_before_marking_the_window_handled(monkeypatch):
    service = _service()
    config = _config(anchor_time_percent=100)
    service._cw_phase = "anchor"
    service._cw_phase_deadline = asyncio.get_running_loop().time() + 1
    service._cw_base_stationary = False
    attempts = 0

    async def flaky_anchor(*_args, **_kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            service._cw_runner_stop.set()
            return True
        return False

    async def no_retry_delay(*_args, **_kwargs):
        return None

    monkeypatch.setattr(service, "_camerawork_leg", flaky_anchor)
    monkeypatch.setattr(service, "_wait_for_camerawork_boundary", no_retry_delay)

    await service._run_camerawork(config, service._cw_runner_stop)

    assert attempts == 2
    assert service._cw_anchor_commanded is True


@pytest.mark.asyncio
async def test_recording_point_dwell_is_internal_and_updates_the_route_wide_runner(
    monkeypatch,
):
    service = _service()
    service._point_dwell_baseline_seconds = 0.01
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(service, "_sleep_or_cancel", record_wait)
    monkeypatch.setattr(random, "uniform", lambda low, high: high)

    await service._dwell(record=True, camerawork=_config())

    assert service._cw_base_stationary is True
    assert service._cw_stationary_until > 0
    assert waits == [pytest.approx(0.013, abs=0.001)]


@pytest.mark.asyncio
async def test_non_recording_trial_has_no_internal_point_dwell(monkeypatch):
    service = _service()
    waits = []

    async def record_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(service, "_sleep_or_cancel", record_wait)

    await service._dwell(record=False, camerawork=None)

    assert waits == []


def test_clamp_keeps_a_start_pose_within_the_configured_range():
    assert _clamp(-135.0, -90.0, 90.0) == -90.0
    assert _clamp(135.0, -90.0, 90.0) == 90.0
    assert _clamp(25.0, -60.0, 15.0) == 15.0
