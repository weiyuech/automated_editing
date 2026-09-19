import asyncio

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraworkConfig,
    CruisePoint,
    CruiseRequest,
    CruiseRun,
    CruiseSegment,
)
from automated_video_editing_backend.services.camera_program import camera_program
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService


def test_exact_cardinal_and_corner_program_uses_asymmetric_operator_bounds():
    config = CameraworkConfig(yaw_min=-20, yaw_max=50, pitch_min=-10, pitch_max=5, point_mode=8)
    pieces = camera_program(config)
    assert [p.poses for p in pieces[:6]] == [
        ((0, 0), (50, 0)),
        ((50, 0), (-20, 0)),
        ((-20, 0), (0, 0)),
        ((0, 0), (0, -10)),
        ((0, -10), (0, 5)),
        ((0, 5), (0, 0)),
    ]
    assert [p.poses for p in pieces[6:]] == [
        ((0, 0), (50, -10), (0, 0)),
        ((0, 0), (-20, -10), (0, 0)),
        ((0, 0), (-20, 5), (0, 0)),
        ((0, 0), (50, 5), (0, 0)),
    ]
    assert [p.id for p in camera_program(config, ["lower-left", "origin-left"])] == [
        "origin-left",
        "lower-left",
    ]
    assert camera_program(config, []) == []
    with pytest.raises(ValueError):
        camera_program(CameraworkConfig(point_mode=4), ["lower-left"])


class Robot:
    def __init__(self):
        self.pose = (0, 0)
        self.revision = 0
        self.goals = []

    def heartbeat_yaw(self):
        return self.pose[0]

    def heartbeat_pitch(self):
        return self.pose[1]

    def heartbeat_revision(self):
        return self.revision

    async def set_goal(self, command):
        self.goals.append(command.goal_id)
        return {"goal_check": "true"}

    async def wait_for_arrival(self, timeout):
        return "done"

    async def set_gimbal(self, command, **kwargs):
        pass


@pytest.mark.asyncio
async def test_next_chassis_goal_waits_for_entire_selected_camera_program(tmp_path, monkeypatch):
    robot = Robot()
    capture = CaptureService(EventHub(), path=tmp_path / "captures.json")
    cruise = CruiseService(EventHub(), robot, capture)
    run = CruiseRun(
        segments=[CruiseSegment(index=i, path_name="route", goal_id=i + 1) for i in range(2)]
    )
    request = CruiseRequest(
        map_name="map",
        record=False,
        auto_camerawork=True,
        points=[
            CruisePoint(path_name="route", goal_id=1, piece_ids=["origin-left", "right-origin"]),
            CruisePoint(path_name="route", goal_id=2, piece_ids=[]),
        ],
    )
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def move(target, config):
        calls.append((robot.goals[:], target))
        if target == (60, 0):
            entered.set()
            await release.wait()
        robot.pose = target

    monkeypatch.setattr(cruise, "_move_to_pose", move)
    task = asyncio.create_task(cruise._execute(request, run, CameraworkConfig(configured=True)))
    await asyncio.wait_for(entered.wait(), 1)
    assert robot.goals == [1]
    assert run.segments[0].departed_at_seconds is None
    release.set()
    await asyncio.wait_for(task, 1)
    assert robot.goals == [1, 2]
    assert [s["id"] for s in run.segments[0].shots] == [
        "origin-left",
        "prepare-right-origin",
        "right-origin",
    ]
    assert all(s["status"] == "complete" and s["end"] >= s["start"] for s in run.segments[0].shots)
    assert run.status == "succeeded"
    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_unconfirmed_shot_blocks_next_navigation_and_still_finalizes_session(
    tmp_path, monkeypatch
):
    robot = Robot()
    capture = CaptureService(EventHub(), path=tmp_path / "captures.json")
    cruise = CruiseService(EventHub(), robot, capture)
    request = CruiseRequest(
        map_name="map",
        record=False,
        auto_camerawork=True,
        points=[CruisePoint(path_name="r", goal_id=i, piece_ids=["origin-left"]) for i in (1, 2)],
    )
    run = CruiseRun(
        segments=[CruiseSegment(index=i, path_name="r", goal_id=i + 1) for i in range(2)]
    )

    async def fail(target, config):
        if target != (0, 0):
            raise ValueError("缺少到位反馈")

    monkeypatch.setattr(cruise, "_move_to_pose", fail)
    await cruise._execute(request, run, CameraworkConfig(configured=True))
    assert robot.goals == [1]
    assert run.status == "failed"
    assert run.segments[0].shots[0]["status"] == "incomplete"
    assert capture.active_session() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target_yaw", [0, 10])
async def test_cached_pose_and_elapsed_time_cannot_confirm_a_move(tmp_path, monkeypatch, target_yaw):
    robot = Robot()
    robot.pose = (target_yaw, 0)
    robot.revision = 7
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    reached, observed = await service._await_camerawork_pose(target_yaw, 0, 0.03, after_revision=7)
    assert (reached, observed) == (False, False)
    robot.pose = (0, 0)

    async def no_feedback(*args, **kwargs):
        return False, False

    monkeypatch.setattr(service, "_await_camerawork_pose", no_feedback)
    with pytest.raises(ValueError, match="停止后续镜头和导航"):
        await service._move_to_pose((10, 0), CameraworkConfig())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target,pose,expected",
    [
        ((0, 0), (2, -2), True),
        ((0, 0), (-2, 2), True),
        ((0, 0), (2.01, 0), False),
        ((0, 0), (0, -2.01), False),
        ((50, -10), (48, -8), True),
        ((50, -10), (47.99, -10), False),
        ((50, -10), (50, -12.01), False),
        ((50, 0), (48, 2), True),
        ((50, 0), (50, 2.01), False),
    ],
)
async def test_all_targets_allow_two_degrees_per_axis(
    tmp_path, monkeypatch, target, pose, expected
):
    robot = Robot()
    robot.pose = pose
    samples = 0

    def fresh_revision():
        nonlocal samples
        samples += 1
        return samples

    monkeypatch.setattr(robot, "heartbeat_revision", fresh_revision)
    monkeypatch.setattr(
        "automated_video_editing_backend.services.cruise._CW_POSE_POLL_SECONDS", 0.001
    )
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    reached, observed = await service._await_camerawork_pose(*target, 0.03, after_revision=0)
    assert (reached, observed) == (expected, True)
    assert samples >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [4, 8])
async def test_full_program_keeps_order_without_extra_preparation_within_pose_grace(
    tmp_path, monkeypatch, mode
):
    robot = Robot()
    robot.pose = (2, -2)
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    config = CameraworkConfig(
        yaw_min=-20, yaw_max=50, pitch_min=-10, pitch_max=5, point_mode=mode
    )
    segment = CruiseSegment(index=0, path_name="r", goal_id=1)
    run = CruiseRun(segments=[segment])
    targets = []

    async def move(target, config):
        targets.append(target)
        robot.pose = (target[0] - 2, target[1] + 2)

    monkeypatch.setattr(service, "_move_to_pose", move)
    await service._run_camera_program(run, segment, config, None)
    pieces = camera_program(config)
    assert len(segment.shots) == (6 if mode == 4 else 10)
    assert [shot["id"] for shot in segment.shots] == [piece.id for piece in pieces]
    assert targets == [target for piece in pieces for target in piece.poses[1:]]
    assert all(shot["status"] == "complete" for shot in segment.shots)


@pytest.mark.asyncio
async def test_cancel_during_piece_never_dispatches_the_next_goal(tmp_path, monkeypatch):
    robot = Robot()
    capture = CaptureService(EventHub(), path=tmp_path / "captures.json")
    service = CruiseService(EventHub(), robot, capture)
    request = CruiseRequest(
        map_name="map",
        record=False,
        auto_camerawork=True,
        points=[CruisePoint(path_name="r", goal_id=i, piece_ids=["origin-left"]) for i in (1, 2)],
    )
    run = CruiseRun(
        segments=[CruiseSegment(index=i, path_name="r", goal_id=i + 1) for i in range(2)]
    )

    async def cancel_at_shot(target, config):
        if target != (0, 0):
            service._cancel.set()
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "_move_to_pose", cancel_at_shot)
    with pytest.raises(asyncio.CancelledError):
        await service._execute(request, run, CameraworkConfig(configured=True))
    assert robot.goals == [1]
    assert run.status == "canceled"
    assert capture.active_session() is None
