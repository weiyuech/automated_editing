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
    config = _config()
    current = 12.0  # positive yaw is physical left
    targets = [_service()._adaptive_yaw_target(current, config) for _ in range(2000)]
    large_right = [target for target in targets if target < current]
    small_left = [target for target in targets if target > current]

    assert large_right and small_left
    assert 78 <= len(large_right) / 20 <= 82
    assert all(target <= -4 for target in large_right)  # >=60% of the 27° inward room, rounded
    assert all(current < target <= 13 for target in small_left)  # <=30% of 3° outward room
    assert all(config.yaw_min <= target <= config.yaw_max for target in targets)


def test_right_pose_mirrors_to_large_left_or_small_right():
    random.seed(21)
    config = _config()
    current = -12.0
    targets = [_service()._adaptive_yaw_target(current, config) for _ in range(2000)]
    large_left = [target for target in targets if target > current]
    small_right = [target for target in targets if target < current]

    assert large_left and small_right
    assert 78 <= len(large_left) / 20 <= 82
    assert all(target >= 4 for target in large_left)
    assert all(-13 <= target < current for target in small_right)
    assert all(config.yaw_min <= target <= config.yaw_max for target in targets)


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


def test_pingpong_starts_on_the_side_opposite_the_current_pose():
    random.seed(4)
    service = _service()
    config = _config()

    first_from_left, second_from_left = service._pingpong_poses(12, config)
    assert first_from_left[0] < 0 < second_from_left[0]
    first_from_right, second_from_right = service._pingpong_poses(-12, config)
    assert first_from_right[0] > 0 > second_from_right[0]


class _ParkedRobot:
    def __init__(self):
        self.state = RobotState(connected=True, yaw=8.0, pitch=4.0)
        self.commands = []

    def heartbeat_yaw(self):
        return self.state.yaw

    def heartbeat_pitch(self):
        return self.state.pitch

    async def status(self):
        return self.state

    async def set_gimbal(self, command):
        self.commands.append(command)
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
