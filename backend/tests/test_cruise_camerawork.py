import asyncio
import collections
import random
from types import SimpleNamespace

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraworkConfig,
    GimbalScanConfig,
    RobotState,
)
from automated_video_editing_backend.services.cruise import CruiseService, _clamp
from automated_video_editing_backend.services.robot import HardwareRobotAdapter


def _service():
    # Only pure target selection is exercised by most tests, so no robot is needed.
    return CruiseService.__new__(CruiseService)


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
    }
    values.update(overrides)
    return CameraworkConfig(**values)


def test_camerawork_modes_replace_the_old_hold_with_the_anchor_at_fifty_thirty_twenty():
    random.seed(1)
    counts = collections.Counter(_service()._pick_camerawork_mode() for _ in range(20000))

    assert set(counts) == {"wander", "pingpong", "anchor"}
    assert 47 <= counts["wander"] / 200 <= 53
    assert 27 <= counts["pingpong"] / 200 <= 33
    assert 17 <= counts["anchor"] / 200 <= 23


@pytest.mark.asyncio
async def test_anchor_mode_is_one_return_move_then_the_planner_can_choose_again(monkeypatch):
    service = _service()
    service._cancel = asyncio.Event()
    config = _config(anchor_yaw=2, anchor_pitch=-1)
    stop = asyncio.Event()
    targets = []

    monkeypatch.setattr(service, "_pick_camerawork_mode", lambda: "anchor")
    monkeypatch.setattr(service, "_current_yaw", lambda _config: 12.0)
    monkeypatch.setattr(service, "_current_pitch", lambda _config: 4.0)

    async def record_one(target, _speed, _deadline, _config):
        targets.append(target)
        stop.set()

    monkeypatch.setattr(service, "_camerawork_leg", record_one)
    await service._run_camerawork(
        deadline=10**12,
        config=config,
        stop_requested=stop,
    )

    assert targets == [(config.anchor_yaw, config.anchor_pitch)]


def test_left_pose_means_large_right_or_small_left():
    random.seed(12)
    config = _config(yaw_min=-60, yaw_max=60)
    current = 30.0  # positive yaw is physical left; both sides have more than 10° room
    targets = [_service()._adaptive_yaw_target(current, config) for _ in range(2000)]
    large_right = [target for target in targets if target < current]
    small_left = [target for target in targets if target > current]

    assert large_right and small_left
    assert 78 <= len(large_right) / 20 <= 82
    assert all(-51 <= target <= -24 for target in large_right)  # 60–90% of 90° room
    assert all(33 <= target <= 39 for target in small_left)  # 10–30% of 30° room
    assert all(config.yaw_min <= target <= config.yaw_max for target in targets)


def test_right_pose_mirrors_to_large_left_or_small_right():
    random.seed(21)
    config = _config(yaw_min=-60, yaw_max=60)
    current = -30.0
    targets = [_service()._adaptive_yaw_target(current, config) for _ in range(2000)]
    large_left = [target for target in targets if target > current]
    small_right = [target for target in targets if target < current]

    assert large_left and small_right
    assert 78 <= len(large_left) / 20 <= 82
    assert all(24 <= target <= 51 for target in large_left)
    assert all(-39 <= target <= -33 for target in small_right)
    assert all(config.yaw_min <= target <= config.yaw_max for target in targets)


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("outward_room", [9, 10, 11])
def test_adaptive_turns_inward_at_ten_degrees_without_changing_probability_elsewhere(
    monkeypatch, side, outward_room,
):
    # This draw normally selects the small outward move. At <=10° remaining it must
    # instead turn inward, including on the exact 10° boundary, on either physical side.
    monkeypatch.setattr(random, "random", lambda: 0.99)
    config = _config(yaw_min=-60, yaw_max=60)
    current = side * (60 - outward_room)
    target = _service()._adaptive_yaw_target(current, config)

    assert (side * (target - current) < 0) == (outward_room <= 10)
    assert config.yaw_min <= target <= config.yaw_max


@pytest.mark.parametrize("side", [-1, 1])
@pytest.mark.parametrize("inward_room", [10, 11])
def test_adaptive_requires_more_than_ten_degrees_for_opposite_branch(
    monkeypatch, side, inward_room,
):
    monkeypatch.setattr(random, "random", lambda: 0.0)
    low, high = (20, 60) if side > 0 else (-60, -20)
    config = _config(yaw_min=low, yaw_max=high, anchor_yaw=side * 40)
    current = side * (20 + inward_room)
    target = _service()._adaptive_yaw_target(current, config)

    assert (side * (target - current) < 0) == (inward_room > 10)
    assert low <= target <= high


@pytest.mark.parametrize(
    ("low", "high", "draw", "expect_right"),
    [(-10, 60, 0.0, False), (-11, 60, 0.0, True),
     (-60, 10, 0.99, True), (-60, 11, 0.99, False)],
)
def test_center_direction_uses_the_same_ten_degree_room_check(
    monkeypatch, low, high, draw, expect_right,
):
    monkeypatch.setattr(random, "random", lambda: draw)
    target = _service()._adaptive_yaw_target(0, _config(yaw_min=low, yaw_max=high))

    assert (target < 0) == expect_right
    assert low <= target <= high


@pytest.mark.parametrize(
    ("current", "yaw_min", "yaw_max", "opposite"),
    [
        (5.0, -10, 50, lambda target: target < 0),
        (-5.0, -50, 10, lambda target: target > 0),
    ],
)
def test_asymmetric_range_uses_protocol_zero_for_left_right_bias(
    current, yaw_min, yaw_max, opposite,
):
    random.seed(31)
    service = _service()
    config = _config(yaw_min=yaw_min, yaw_max=yaw_max, anchor_yaw=0)
    targets = [service._adaptive_yaw_target(current, config) for _ in range(2000)]
    opposite_targets = [target for target in targets if opposite(target)]

    # +yaw is physically left and -yaw is physically right regardless of an asymmetric range.
    # The broad move crosses the real zero about 80% of the time; the other move remains a
    # smaller continuation on the current physical side. Every result remains operator-bounded.
    assert 78 <= len(opposite_targets) / 20 <= 82
    assert all(yaw_min <= target <= yaw_max for target in targets)


def test_wander_poses_respect_every_operator_range():
    random.seed(3)
    service = _service()
    config = _config(yaw_min=-7, yaw_max=18, anchor_yaw=2, pitch_min=-4, pitch_max=6)
    current = 2.0
    for _ in range(500):
        yaw, pitch = service._camerawork_pose(current, config)
        assert config.yaw_min <= yaw <= config.yaw_max
        assert config.pitch_min <= pitch <= config.pitch_max
        current = yaw


def _assert_separated_pose(start, target, config):
    yaw_mid = (config.yaw_min + config.yaw_max) / 2
    pitch_mid = (config.pitch_min + config.pitch_max) / 2
    assert (start[0] >= yaw_mid, start[1] >= pitch_mid) != (
        target[0] >= yaw_mid, target[1] >= pitch_mid,
    )
    assert config.yaw_min <= target[0] <= config.yaw_max
    assert config.pitch_min <= target[1] <= config.pitch_max


@pytest.mark.parametrize(
    ("yaw_min", "yaw_max", "pitch_min", "pitch_max"),
    [(-60, 60, -15, 15), (-10, 50, -40, 10), (20, 60, -60, -20),
     (-5, 5, 3, 4), (0, 1, 0, 1)],
)
def test_random_targets_change_user_range_quadrant_and_stay_inside_bounds(
    yaw_min, yaw_max, pitch_min, pitch_max,
):
    random.seed(93)
    service = _service()
    config = _config(
        yaw_min=yaw_min, yaw_max=yaw_max, anchor_yaw=yaw_min,
        pitch_min=pitch_min, pitch_max=pitch_max, anchor_pitch=pitch_min,
    )
    for start in [
        (yaw_min, pitch_min), (yaw_max, pitch_max),
        ((yaw_min + yaw_max) / 2, (pitch_min + pitch_max) / 2),
    ]:
        for _ in range(100):
            proposed = service._camerawork_pose(start[0], config)
            target = service._separate_camerawork_target(proposed, *start, config)
            assert target[0] == proposed[0], "separation must preserve the original yaw choice"
            _assert_separated_pose(start, target, config)
            start = target


@pytest.mark.parametrize("proposed", [(1, 1), (1, -1), (-1, 0)])
def test_crossing_either_center_line_is_enough_even_for_a_small_move(proposed):
    config = _config(yaw_min=-60, yaw_max=60, pitch_min=-15, pitch_max=15)
    target = _service()._separate_camerawork_target(proposed, -1, -1, config)

    assert target == proposed
    _assert_separated_pose((-1, -1), target, config)


def test_small_outward_yaw_keeps_its_direction_and_changes_pitch_half():
    config = _config(yaw_min=-60, yaw_max=60, pitch_min=-15, pitch_max=15)
    target = _service()._separate_camerawork_target((36, 12), 30, 10, config)

    assert target[0] == 36
    assert target[1] < 0
    _assert_separated_pose((30, 10), target, config)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["wander", "pingpong", "anchor"])
async def test_random_and_anchor_fallback_legs_apply_separation_before_sending(monkeypatch, mode):
    service = _service()
    service._cancel = asyncio.Event()
    config = _config(yaw_min=-60, yaw_max=60, pitch_min=-15, pitch_max=15)
    stop = asyncio.Event()
    # The second ping-pong leg must compare against fresh feedback, not its first target.
    starts = [(0, 0), (1, 1)]
    sent = []
    monkeypatch.setattr(service, "_pick_camerawork_mode", lambda: mode)
    monkeypatch.setattr(service, "_current_yaw", lambda _config: starts[len(sent)][0])
    monkeypatch.setattr(service, "_current_pitch", lambda _config: starts[len(sent)][1])
    monkeypatch.setattr(service, "_camerawork_pose", lambda *_args: (1, 1))
    monkeypatch.setattr(service, "_pingpong_poses", lambda *_args: ((1, 1), (1, 1)))

    async def record(target, _speed, _deadline, _config):
        _assert_separated_pose(starts[len(sent)], target, config)
        sent.append(target)
        if len(sent) == (2 if mode == "pingpong" else 1):
            stop.set()

    monkeypatch.setattr(service, "_camerawork_leg", record)
    await service._run_camerawork(10**12, config, stop)
    assert len(sent) == (2 if mode == "pingpong" else 1)


@pytest.mark.asyncio
async def test_explicit_anchor_bypasses_random_target_separation(monkeypatch):
    service = _service()
    service._cancel = asyncio.Event()
    config = _config(yaw_min=-60, yaw_max=60, pitch_min=-15, pitch_max=15)
    stop = asyncio.Event()
    sent = []
    monkeypatch.setattr(service, "_pick_camerawork_mode", lambda: "anchor")
    monkeypatch.setattr(service, "_current_yaw", lambda _config: 5)
    monkeypatch.setattr(service, "_current_pitch", lambda _config: 4)

    async def record(target, _speed, _deadline, _config):
        sent.append(target)
        stop.set()

    monkeypatch.setattr(service, "_camerawork_leg", record)
    await service._run_camerawork(10**12, config, stop)
    assert sent == [(0, 0)]


def test_pingpong_starts_on_the_side_opposite_the_current_pose():
    random.seed(4)
    service = _service()
    config = _config()

    first_from_left, second_from_left = service._pingpong_poses(12, config)
    assert first_from_left[0] < 0 < second_from_left[0]
    first_from_right, second_from_right = service._pingpong_poses(-12, config)
    assert first_from_right[0] > 0 > second_from_right[0]


def test_pingpong_uses_physical_sides_inside_asymmetric_ranges():
    random.seed(8)
    service = _service()

    first, second = service._pingpong_poses(5, _config(yaw_min=-10, yaw_max=50))
    assert -10 <= first[0] <= 0 < second[0] <= 50

    first, second = service._pingpong_poses(-5, _config(yaw_min=-50, yaw_max=10))
    assert -50 <= second[0] < 0 <= first[0] <= 10


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
async def test_parked_phase_holds_yaw_pitch_while_zooming_then_returns_full_anchor(monkeypatch):
    robot = _ParkedRobot()
    service = CruiseService(EventHub(), robot, object())
    service._cw_yaw = 8.0
    service._cw_pitch = 4.0
    service._cw_zoom = 1.0

    async def no_wait(_seconds):
        return False

    async def reached(*_args, **_kwargs):
        return True, True

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

    async def missed(_config, *, wait=True):
        assert wait is True
        return False

    monkeypatch.setattr(service, "_return_to_anchor", missed)

    assert await service._parked_zoom_and_anchor(_config()) is False
    assert robot.commands == []


@pytest.mark.asyncio
async def test_auto_dwell_always_leaves_a_visible_anchor_shot(monkeypatch):
    service = _service()
    service._cancel = asyncio.Event()
    waits = []

    async def parked(_config):
        return True

    async def record_wait(seconds):
        waits.append(seconds)
        return False

    monkeypatch.setattr(service, "_parked_zoom_and_anchor", parked)
    monkeypatch.setattr(service, "_sleep_or_cancel", record_wait)
    request = SimpleNamespace(
        dwell_min_seconds=0.0,
        dwell_max_seconds=0.0,
        gimbal_scan=GimbalScanConfig(),
    )
    segment = SimpleNamespace(scanned=False)

    await service._dwell(request, segment, _config())

    assert segment.scanned is True
    assert waits == [pytest.approx(1.0)]


def test_clamp_keeps_a_start_pose_within_the_configured_range():
    assert _clamp(-135.0, -90.0, 90.0) == -90.0
    assert _clamp(135.0, -90.0, 90.0) == 90.0
    assert _clamp(25.0, -60.0, 15.0) == 15.0
