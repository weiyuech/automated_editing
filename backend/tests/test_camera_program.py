import asyncio
from types import SimpleNamespace

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
from automated_video_editing_backend.services import cruise as cruise_module
from automated_video_editing_backend.services.cruise import CruiseService


def test_exact_cardinal_and_corner_program_uses_asymmetric_operator_bounds():
    config = CameraworkConfig(yaw_min=-20, yaw_max=50, pitch_min=-10, pitch_max=5, point_mode=8)
    pieces = camera_program(config)
    assert [p.zooms for p in pieces[:2]] == [(1, 2), (2, 1)]
    assert all(p.poses == ((0, 0), (0, 0)) for p in pieces[:2])
    assert [p.poses for p in pieces[2:8]] == [
        ((0, 0), (50, 0)),
        ((50, 0), (-20, 0)),
        ((-20, 0), (0, 0)),
        ((0, 0), (0, -10)),
        ((0, -10), (0, 5)),
        ((0, 5), (0, 0)),
    ]
    assert [p.poses for p in pieces[8:]] == [
        ((0, 0), (50, -10), (0, 0)),
        ((0, 0), (-20, -10), (0, 0)),
        ((0, 0), (-20, 5), (0, 0)),
        ((0, 0), (50, 5), (0, 0)),
    ]
    assert [p.id for p in camera_program(config, ["lower-left", "origin-left"])] == [
        "zoom-outbound", "zoom-return", "origin-left",
        "lower-left",
    ]
    assert [p.id for p in camera_program(config, [])] == ["zoom-outbound", "zoom-return"]
    with pytest.raises(ValueError):
        camera_program(CameraworkConfig(point_mode=4), ["lower-left"])


def test_custom_zoom_profile_round_trip_and_limits():
    config = CameraworkConfig(anchor_zoom=1.2, zoom_target=1.5, point_mode=8)
    loaded = CameraworkConfig.model_validate_json(config.model_dump_json())
    pieces = camera_program(loaded, [])
    assert [p.zooms for p in pieces] == [(1.2, 1.5), (1.5, 1.2)]
    assert CameraworkConfig(anchor_zoom=2).zoom_target == 1
    for invalid in (0.9, 3.6, 1.2):
        with pytest.raises(ValueError):
            CameraworkConfig(anchor_zoom=1.2, zoom_target=invalid)


class Robot:
    def __init__(self):
        self.pose = (0, 0)
        self.zoom = 1.0
        self.revision = 0
        self.goals = []

    def heartbeat_yaw(self):
        return self.pose[0]

    def heartbeat_pitch(self):
        return self.pose[1]

    def heartbeat_revision(self):
        return self.revision

    def heartbeat_zoom(self):
        return self.zoom

    def heartbeat_zoom_revision(self):
        return self.revision

    async def set_goal(self, command):
        self.goals.append(command.goal_id)
        return {"goal_check": "true"}

    async def wait_for_arrival(self, timeout):
        return "done"

    async def set_gimbal(self, command, **kwargs):
        pass


@pytest.mark.asyncio
@pytest.mark.parametrize("anchor,target", [(1.0, 2.0), (2.0, 1.0), (1.2, 1.8)])
async def test_reposition_before_zoom_return_preserves_its_starting_zoom(
    tmp_path, monkeypatch, anchor, target,
):
    robot = Robot()
    robot.zoom = anchor
    service = CruiseService(EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "c.json"))
    config = CameraworkConfig(anchor_zoom=anchor, zoom_target=target)
    segment = CruiseSegment(index=1, path_name="r", goal_id=2)
    run = CruiseRun(segments=[segment])
    calls = []

    async def move(pose, config, *, target_zoom=None, **options):
        desired = config.anchor_zoom if target_zoom is None else target_zoom
        calls.append((segment.shots[-1]["id"], robot.zoom, desired, options))
        robot.pose, robot.zoom = pose, desired

    async def publish(event, *_):
        if event == "CRUISE_SHOT_FINISHED" and segment.shots[-1]["id"] == "zoom-outbound":
            # Pose feedback changes after successful arrival, before the next shot.
            robot.pose = (config.angle_tolerance_degrees + 1, 0)

    monkeypatch.setattr(service, "_move_to_pose", move)
    monkeypatch.setattr(service, "_publish_segment", publish)
    await service._run_camera_program(run, segment, config, [])

    assert [(name, start, end) for name, start, end, _ in calls] == [
        ("zoom-outbound", anchor, target),
        ("prepare-zoom-return", target, target),
        ("zoom-return", target, anchor),
    ]
    assert calls[1][3]["max_sends"] == 6
    assert [s["kind"] for s in segment.shots] == ["shot", "preparation", "shot"]
    assert segment.shots[1]["zoom_start"] == segment.shots[1]["zoom_end"] == target
    assert segment.shots[2]["motion_axes"] == ["zoom"]


@pytest.mark.asyncio
async def test_selected_return_to_origin_is_a_shot_not_a_warmup(tmp_path, monkeypatch):
    robot = Robot()
    service = CruiseService(EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "c.json"))
    segment = CruiseSegment(index=1, path_name="r", goal_id=2)
    run = CruiseRun(segments=[segment])
    calls = []

    async def move(pose, config, *, target_zoom=None, **options):
        calls.append((segment.shots[-1]["id"], pose, options))
        robot.pose = pose
        robot.zoom = config.anchor_zoom if target_zoom is None else target_zoom

    monkeypatch.setattr(service, "_move_to_pose", move)
    await service._run_camera_program(run, segment, CameraworkConfig(), ["right-origin"])
    assert [s["id"] for s in segment.shots] == [
        "zoom-outbound", "zoom-return", "prepare-right-origin", "right-origin",
    ]
    assert segment.shots[-1]["kind"] == "shot"
    assert segment.shots[-1]["motion_axes"] == ["yaw"]
    assert calls[-1] == ("right-origin", (0, 0), {})


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

    async def move(target, config, *, target_zoom=None):
        calls.append((robot.goals[:], target))
        if target_zoom == config.zoom_target and robot.goals == [1]:
            entered.set()
            await release.wait()
        robot.pose = target
        if target_zoom is not None:
            robot.zoom = target_zoom

    monkeypatch.setattr(cruise, "_move_to_pose", move)
    task = asyncio.create_task(cruise._execute(request, run, CameraworkConfig(configured=True)))
    await asyncio.wait_for(entered.wait(), 1)
    assert robot.goals == [1]
    assert run.segments[0].departed_at_seconds is None
    release.set()
    await asyncio.wait_for(task, 1)
    assert robot.goals == [1, 2]
    assert [s["id"] for s in run.segments[0].shots] == [
        "zoom-outbound", "zoom-return", "origin-left",
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

    async def fail(target, config, *, target_zoom=None):
        if target_zoom is not None:
            robot.zoom = target_zoom
        if target != (0, 0):
            raise ValueError("缺少到位反馈")

    monkeypatch.setattr(cruise, "_move_to_pose", fail)
    await cruise._execute(request, run, CameraworkConfig(configured=True))
    assert robot.goals == [1]
    assert run.status == "failed"
    assert run.segments[0].shots[2]["status"] == "incomplete"
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
    reached, observed = await service._await_camerawork_pose(
        target_yaw,
        0,
        0,
        after_revision=7,
        target_zoom=1,
        after_zoom_revision=7,
        monitor_failsafe_seconds=0.03,
    )
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
        ((0, 0), (5, -5), True),
        ((0, 0), (-5, 5), True),
        ((0, 0), (5.01, 0), False),
        ((0, 0), (0, -5.01), False),
        ((50, -10), (45, -5), True),
        ((50, -10), (44.99, -10), False),
        ((50, -10), (50, -15.01), False),
        ((50, 0), (45, 5), True),
        ((50, 0), (50, 5.01), False),
    ],
)
async def test_all_targets_allow_five_degrees_per_axis(
    tmp_path, monkeypatch, target, pose, expected
):
    robot = Robot()
    robot.pose = pose
    samples = 0

    def fresh_revision():
        nonlocal samples
        samples += 1
        robot.revision = samples
        return samples

    monkeypatch.setattr(robot, "heartbeat_revision", fresh_revision)
    monkeypatch.setattr(
        "automated_video_editing_backend.services.cruise._CW_POSE_POLL_SECONDS", 0.001
    )
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    reached, observed = await service._await_camerawork_pose(
        *target,
        0,
        after_revision=0,
        target_zoom=1,
        after_zoom_revision=0,
        # Windows' default asyncio timer granularity can be about 15.6 ms. Give the
        # three-heartbeat stability window enough wall-clock time on every runner.
        monitor_failsafe_seconds=0.25,
    )
    assert (reached, observed) == (expected, True)
    assert samples >= 3


@pytest.mark.asyncio
async def test_pose_inside_grace_must_stop_moving_before_next_command(tmp_path, monkeypatch):
    robot = Robot()
    readings = iter([(8, 0), (4, 0), (0.5, 0), (0.3, 0), (0.2, 0)])
    robot.revision = 0
    original_sleep = asyncio.sleep

    async def next_sample(_):
        robot.pose = next(readings)
        robot.revision += 1
        await original_sleep(0)

    service = CruiseService(EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json"))
    monkeypatch.setattr("automated_video_editing_backend.services.cruise._CW_POSE_POLL_SECONDS", 0.001)
    monkeypatch.setattr("automated_video_editing_backend.services.cruise.asyncio.sleep", next_sample)
    reached, observed = await service._await_camerawork_pose(
        0,
        0,
        0,
        after_revision=0,
        target_zoom=1,
        after_zoom_revision=0,
        monitor_failsafe_seconds=0.1,
    )
    assert (reached, observed) == (True, True)
    assert robot.revision == 5  # Being inside tolerance was insufficient until three stable samples.


@pytest.mark.asyncio
async def test_stability_samples_begin_only_after_angle_estimate(tmp_path, monkeypatch):
    robot = Robot()
    clock = 0.0

    async def advance(delay):
        nonlocal clock
        clock += max(delay, 0.2)
        robot.revision += 1

    monkeypatch.setattr(cruise_module, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(cruise_module.asyncio, "sleep", advance)
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    reached, observed = await service._await_camerawork_pose(
        0,
        0,
        1.0,
        after_revision=0,
        target_zoom=1,
        after_zoom_revision=0,
        monitor_failsafe_seconds=2,
    )
    assert (reached, observed) == (True, True)
    # Five samples arrived before 1.0 s. They were observed but none was allowed
    # into the three-sample completion window.
    assert clock >= 1.4
    assert robot.revision >= 7


@pytest.mark.asyncio
async def test_settled_outside_tolerance_gets_three_seconds_then_reports_differences(
    tmp_path, monkeypatch
):
    robot = Robot()
    robot.pose = (20, -8)
    robot.zoom = 1.4
    clock = 0.0

    async def advance(delay):
        nonlocal clock
        clock += max(delay, 0.5)
        robot.revision += 1

    monkeypatch.setattr(cruise_module, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(cruise_module.asyncio, "sleep", advance)
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    with pytest.raises(ValueError, match=r"水平 20.00°.*俯仰 8.00°.*倍率 0.40×.*第 1 轮观察结束"):
        await service._await_camerawork_pose(
            0,
            0,
            0,
            after_revision=0,
            target_zoom=1,
            after_zoom_revision=0,
            angle_tolerance=5,
            zoom_tolerance=0.1,
            monitor_failsafe_seconds=10,
        )
    assert clock >= 4.0


@pytest.mark.asyncio
async def test_motion_during_extra_observation_can_settle_inside_custom_tolerances(
    tmp_path, monkeypatch
):
    robot = Robot()
    samples = iter([
        (10, 0, 1.3), (10, 0, 1.3), (10, 0, 1.3),
        (7, 0, 1.2), (3.8, 0, 1.09), (3.7, 0, 1.08), (3.6, 0, 1.07),
    ])
    clock = 0.0

    async def advance(delay):
        nonlocal clock
        clock += max(delay, 0.2)
        yaw, pitch, zoom = next(samples)
        robot.pose = (yaw, pitch)
        robot.zoom = zoom
        robot.revision += 1

    monkeypatch.setattr(cruise_module, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(cruise_module.asyncio, "sleep", advance)
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    reached, observed = await service._await_camerawork_pose(
        0,
        0,
        0,
        after_revision=0,
        target_zoom=1,
        after_zoom_revision=0,
        angle_tolerance=4,
        zoom_tolerance=0.1,
        monitor_failsafe_seconds=10,
    )
    assert (reached, observed) == (True, True)
    assert robot.pose == (3.6, 0)
    assert robot.zoom == 1.07


@pytest.mark.asyncio
async def test_moving_outside_tolerance_gets_at_most_three_observation_windows(
    tmp_path, monkeypatch
):
    robot = Robot()
    clock = 0.0
    yaw = 10.0

    async def advance(delay):
        nonlocal clock, yaw
        clock += max(delay, 0.5)
        # Open the first window with a settled miss, then keep moving enough that
        # every later three-sample range is above the internal stability limit.
        if robot.revision >= 3:
            yaw += 1
        robot.pose = (yaw, 0)
        robot.revision += 1

    monkeypatch.setattr(cruise_module, "time", SimpleNamespace(monotonic=lambda: clock))
    monkeypatch.setattr(cruise_module.asyncio, "sleep", advance)
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    with pytest.raises(ValueError, match="连续 3 轮观察仍未停稳"):
        await service._await_camerawork_pose(
            0,
            0,
            0,
            after_revision=0,
            target_zoom=1,
            after_zoom_revision=0,
            angle_tolerance=5,
            zoom_tolerance=0.1,
            monitor_failsafe_seconds=20,
        )
    assert clock >= 10.5


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [4, 8])
async def test_full_program_keeps_order_without_extra_preparation_within_pose_grace(
    tmp_path, monkeypatch, mode
):
    robot = Robot()
    robot.pose = (5, -5)
    service = CruiseService(
        EventHub(), robot, CaptureService(EventHub(), path=tmp_path / "capture.json")
    )
    config = CameraworkConfig(
        yaw_min=-20, yaw_max=50, pitch_min=-10, pitch_max=5, point_mode=mode
    )
    segment = CruiseSegment(index=0, path_name="r", goal_id=1)
    run = CruiseRun(segments=[segment])
    targets = []

    async def move(target, config, *, target_zoom=None):
        targets.append(target)
        robot.pose = (target[0] - 5, target[1] + 5)
        if target_zoom is not None:
            robot.zoom = target_zoom

    monkeypatch.setattr(service, "_move_to_pose", move)
    await service._run_camera_program(run, segment, config, None)
    pieces = camera_program(config)
    assert len(segment.shots) == (8 if mode == 4 else 12)
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

    async def cancel_at_shot(target, config, *, target_zoom=None):
        if target != (0, 0):
            service._cancel.set()
            raise asyncio.CancelledError

    monkeypatch.setattr(service, "_move_to_pose", cancel_at_shot)
    with pytest.raises(asyncio.CancelledError):
        await service._execute(request, run, CameraworkConfig(configured=True))
    assert robot.goals == [1]
    assert run.status == "canceled"
    assert capture.active_session() is None
