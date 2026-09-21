"""Regression replays for timing seen on the physical robot.

These tests deliberately do not use the cruise test suite's instant ``FakeAdapter``.  Each
command crosses ``HardwareRobotAdapter``'s real request/reply ownership, while the socket and
robot replies are released independently so the ordering matches the field log.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress

import pytest
import websockets
from websockets.exceptions import ConnectionClosedOK

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraworkConfig,
    CruisePoint,
    CruiseRequest,
    CruiseRun,
    CruiseSegment,
    GimbalMoveRequest,
    MediaItem,
    RobotGoalCommand,
)
from automated_video_editing_backend.services import cruise as cruise_module
from automated_video_editing_backend.services import robot as robot_module
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.camera_program import camera_program
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.robot import HardwareRobotAdapter, RobotService


def _connected_adapter() -> HardwareRobotAdapter:
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def connected():
        return adapter.state

    # A protocol replay owns the socket explicitly.  Starting the reconnect loop would add DNS
    # and wall-clock races that are unrelated to the ordering under test.
    adapter.connect = connected
    adapter.status = connected
    return adapter


def _gimbal_move(yaw_end: float) -> GimbalMoveRequest:
    return GimbalMoveRequest(
        yaw_start=0,
        yaw_end=yaw_end,
        yaw_speed=3,
        pitch_start=0,
        pitch_end=0,
        pitch_speed=3,
        zoom_start=1,
        zoom_end=1,
    )


@pytest.mark.asyncio
async def test_accepted_goal_rejects_repeated_identityless_done_until_going_transition():
    """Bare ``done`` stays ambiguous until this goal proves a non-terminal transition.

    The field robot sometimes accepts a goal and never emits ``going``.  Its navigation block
    also omits path/point identity.  Even repeated terminal frames can be the previous point's
    cached status, so the cruise-level timeout—not guesswork—must own that failure path.
    """

    adapter = _connected_adapter()
    write_started = asyncio.Event()
    release_ack = asyncio.Event()

    class GoalSocket:
        async def send(self, message: str) -> None:
            payload = json.loads(message)
            assert payload == {
                "set_goal": {"path_name": "route-a", "goal_id": 2, "goal_object": None}
            }
            write_started.set()
            await release_ack.wait()
            await adapter._handle_message(
                json.dumps(
                    {
                        "robot_goal": {
                            "path_file": "route-a",
                            "goal_id": 2,
                            "goal_check": "true",
                        }
                    }
                )
            )

    adapter._socket = GoalSocket()
    command = asyncio.create_task(
        adapter.set_goal(RobotGoalCommand(path_name="route-a", goal_id=2))
    )
    await asyncio.wait_for(write_started.wait(), timeout=0.2)

    # The previous point's terminal status can race with this command before its ACK.
    await adapter._handle_message(json.dumps({"naviagtion": {"goal_status": "done"}}))
    assert adapter._arrival_event.is_set() is False

    release_ack.set()
    reply = await asyncio.wait_for(command, timeout=0.2)
    assert reply["goal_check"] == "true"

    # Repetition alone doesn't establish ownership: firmware can cache the old point forever.
    for _ in range(3):
        await adapter._handle_message(json.dumps({"naviagtion": {"goal_status": "done"}}))
        assert adapter._arrival_event.is_set() is False

    # A fresh non-terminal transition does prove that the next terminal report is this goal's.
    await adapter._handle_message(json.dumps({"naviagtion": {"goal_status": "going"}}))
    assert adapter._arrival_event.is_set() is False
    await adapter._handle_message(json.dumps({"naviagtion": {"goal_status": "done"}}))
    assert await adapter.wait_for_arrival(timeout_s=0.2) == "done"


@pytest.mark.asyncio
async def test_cruise_reuses_one_manually_selected_map_without_switching_again():
    """A post-ACK ready heartbeat lets cruise reuse the UI's manual map selection."""

    adapter = _connected_adapter()
    await adapter._handle_message(
        json.dumps(
            {
                "system": {"status": "ready"},
                "map": {"mode": "localization", "name": "old-map", "status": "ready"},
                "naviagtion": {"status": "ready"},
            }
        )
    )
    writes: list[dict] = []
    heartbeat_tasks: list[asyncio.Task] = []

    class MapSocket:
        async def send(self, message: str) -> None:
            payload = json.loads(message)
            writes.append(payload)
            assert payload == {"set_switch_map": "map-a"}
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

            async def report_ready_after_ack_boundary() -> None:
                await asyncio.sleep(0.01)
                await adapter._handle_message(
                    json.dumps(
                        {
                            "system": {"status": "ready"},
                            "map": {
                                "mode": "localization",
                                "name": "map-a",
                                "status": "ready",
                            },
                            "naviagtion": {"status": "ready"},
                        }
                    )
                )

            heartbeat_tasks.append(asyncio.create_task(report_ready_after_ack_boundary()))

    adapter._socket = MapSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    manual = await robot.switch_map("map-a")
    assert manual["ok"] is True
    state = await robot.switch_map_and_confirm("map-a", timeout_s=0.2)
    await asyncio.gather(*heartbeat_tasks)

    assert writes == [{"set_switch_map": "map-a"}]
    assert state is adapter.state
    assert state.map_name == "map-a"
    assert state.map_mode == "localization"
    assert state.map_status == "ready"
    assert state.navigation_status == "ready"


@pytest.mark.asyncio
async def test_real_websocket_partial_map_readiness_blocks_cruise_and_forces_fresh_retry(
    tmp_path,
):
    """Split readiness cannot poison a manual switch for this or the next cruise attempt."""

    commands: list[dict] = []
    server_errors: list[BaseException] = []
    partial_batch_sent = asyncio.Event()

    async def robot_endpoint(connection) -> None:
        async def send(payload: dict) -> None:
            await connection.send(json.dumps(payload))

        try:
            # Establish one genuinely coherent, but obsolete, readiness report first.
            await send(
                {
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "old-map",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                    "gimbal": {
                        "record_status": "idle",
                        "yaw": 0,
                        "pitch": 0,
                        "mode": 1,
                    },
                }
            )
            async for raw in connection:
                payload = json.loads(raw)
                commands.append(payload)
                if "get_map_list" in payload:
                    await send({"robot_map_list": ["map-a"]})
                elif "get_path_list" in payload:
                    await send({"robot_path_list": ["route-a"]})
                elif "set_switch_map" in payload:
                    await send({"robot_switch_map": "true"})
                    # These three frames make the aggregate RobotState look drive-ready, but no
                    # single physical heartbeat proves system + map + navigation together.
                    await send(
                        {
                            "system": {"status": "ready"},
                            "map": {
                                "mode": "localization",
                                "name": "map-a",
                                "status": "ready",
                            },
                        }
                    )
                    await send({"naviagtion": {"status": "ready"}})
                    await send({"gimbal": {"yaw": 4, "pitch": -1, "mode": 1}})
                    partial_batch_sent.set()
                elif "video_record" in payload or "set_goal" in payload:
                    raise AssertionError(
                        "recording/navigation must not start without coherent map readiness"
                    )
        except asyncio.CancelledError:
            raise
        except ConnectionClosedOK:
            pass
        except AssertionError as exc:
            server_errors.append(exc)

    async with websockets.serve(robot_endpoint, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        events = EventHub()
        adapter = HardwareRobotAdapter(events, f"ws://127.0.0.1:{port}")
        robot = RobotService(events, adapter=adapter)

        manual_switch = await asyncio.wait_for(robot.switch_map("map-a"), timeout=0.5)
        assert manual_switch["ok"] is True
        await asyncio.wait_for(partial_batch_sent.wait(), timeout=0.5)

        # This is the field failure's deceptive state: independently cached values all read
        # ready, and the newest general heartbeat is only gimbal telemetry.
        state = await robot.status()
        assert state.map_name == "map-a"
        assert state.map_mode == "localization"
        assert state.map_status == "ready"
        assert state.navigation_status == "ready"
        assert state.diagnostics.last_heartbeat is not None
        assert set(state.diagnostics.last_heartbeat.payload) == {"gimbal"}

        original_switch_map_and_confirm = robot.switch_map_and_confirm

        async def confirm_quickly(map_name: str):
            return await original_switch_map_and_confirm(map_name, timeout_s=0.06)

        robot.switch_map_and_confirm = confirm_quickly
        capture = CaptureService(events, path=tmp_path / "capture-sessions.json")
        cruise = CruiseService(events, robot, capture, lambda: CameraworkConfig())
        request = CruiseRequest(
            title="partial map readiness replay",
            map_name="map-a",
            points=[CruisePoint(path_name="route-a", goal_id=1)],
            record=True,
            auto_camerawork=False,
        )

        try:
            # Attempt one reuses the accepted manual switch but must reject its split reports.
            with pytest.raises(TimeoutError, match="未在 0.06 秒内确认可巡游"):
                await asyncio.wait_for(cruise.start(request), timeout=0.5)
            assert sum("set_switch_map" in payload for payload in commands) == 1

            # A failed confirmation must discard the accepted-switch token. The retry sends a
            # fresh switch and fails closed again rather than launching on poisoned state.
            with pytest.raises(TimeoutError, match="未在 0.06 秒内确认可巡游"):
                await asyncio.wait_for(cruise.start(request), timeout=0.5)
            assert sum("set_switch_map" in payload for payload in commands) == 2
        finally:
            with suppress(Exception):
                await adapter.disconnect()

    assert server_errors == []
    assert not any("video_record" in payload for payload in commands)
    assert not any("set_goal" in payload for payload in commands)
    assert capture.active_session() is None


@pytest.mark.asyncio
async def test_blocked_websocket_send_uses_an_internal_deadline(monkeypatch):
    """A TCP connection that never returns from ``send`` cannot freeze cruise/finalization."""

    adapter = _connected_adapter()
    send_entered = asyncio.Event()
    send_cancelled = asyncio.Event()

    class BlockedSocket:
        async def send(self, _message: str) -> None:
            send_entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                send_cancelled.set()

    adapter._socket = BlockedSocket()
    monkeypatch.setattr(robot_module, "_WEBSOCKET_SEND_TIMEOUT_SECONDS", 0.03)

    command = asyncio.create_task(adapter.set_gimbal(_gimbal_move(20)))
    await asyncio.wait_for(send_entered.wait(), timeout=0.2)
    try:
        with pytest.raises(ConnectionError, match=r"发送.*超过"):
            # The outer limit detects an unbounded implementation.  The asserted message must
            # come from production, so an outer asyncio timeout cannot accidentally pass.
            await asyncio.wait_for(asyncio.shield(command), timeout=0.2)
        assert command.done()
        assert send_cancelled.is_set()
    finally:
        if not command.done():
            command.cancel()
            with suppress(asyncio.CancelledError):
                await command


@pytest.mark.asyncio
async def test_gimbal_busy_holds_later_commands_until_the_robot_reports_ok():
    """Replay the production ``busy, ok`` pattern without piling commands onto the robot."""

    adapter = _connected_adapter()
    sent: asyncio.Queue[dict] = asyncio.Queue()

    class GimbalSocket:
        async def send(self, message: str) -> None:
            payload = json.loads(message)
            assert "gimbal_control" in payload
            await sent.put(payload)

    adapter._socket = GimbalSocket()

    first = asyncio.create_task(adapter.set_gimbal(_gimbal_move(20)))
    first_payload = await asyncio.wait_for(sent.get(), timeout=0.2)
    assert first_payload["gimbal_control"]["yaw_end"] == 20
    await asyncio.wait_for(first, timeout=0.2)

    # ``busy`` means the physical command is still executing even though its websocket write
    # has returned.  The next caller must remain queued.
    await adapter._handle_message(
        json.dumps({"robot_gimbal_control": {"status": "busy"}})
    )

    second_started = asyncio.Event()

    async def issue_second():
        second_started.set()
        return await adapter.set_gimbal(_gimbal_move(-20))

    second = asyncio.create_task(issue_second())
    await second_started.wait()
    await asyncio.sleep(0)
    assert sent.empty()
    assert second.done() is False

    # Only the terminal OK releases the physical command and lets command two reach the wire.
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    second_payload = await asyncio.wait_for(sent.get(), timeout=0.2)
    assert second_payload["gimbal_control"]["yaw_end"] == -20
    await asyncio.wait_for(second, timeout=0.2)

    # The same gate must remain effective after the second write; otherwise a long sequence of
    # automatic targets can still pile up after the first successful serialization.
    await adapter._handle_message(
        json.dumps({"robot_gimbal_control": {"status": "busy"}})
    )
    third = asyncio.create_task(adapter.set_gimbal(_gimbal_move(35)))
    await asyncio.sleep(0)
    assert sent.empty()
    assert third.done() is False
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": " OK "}}))
    third_payload = await asyncio.wait_for(sent.get(), timeout=0.2)
    assert third_payload["gimbal_control"]["yaw_end"] == 35
    await asyncio.wait_for(third, timeout=0.2)


@pytest.mark.asyncio
async def test_missing_gimbal_terminal_reply_recovers_only_after_motion_budget(
    monkeypatch,
):
    """A lost OK must not pile commands, but it also cannot wedge the camera forever."""

    adapter = _connected_adapter()
    sent: list[dict] = []

    class SilentGimbalSocket:
        async def send(self, message: str) -> None:
            sent.append(json.loads(message))

    adapter._socket = SilentGimbalSocket()
    monkeypatch.setattr(robot_module, "_GIMBAL_PREVIOUS_COMMAND_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_gimbal_command_budget_seconds", lambda _payload: 0.04)

    await adapter.set_gimbal(_gimbal_move(20))

    with pytest.raises(TimeoutError, match="仍在执行"):
        await adapter.set_gimbal(_gimbal_move(-20))
    assert len(sent) == 1

    await asyncio.sleep(0.05)
    await adapter.set_gimbal(_gimbal_move(-20))
    assert [payload["gimbal_control"]["yaw_end"] for payload in sent] == [20, -20]


@pytest.mark.asyncio
async def test_late_old_gimbal_ok_cannot_release_a_newer_command(monkeypatch):
    """After estimated recovery, id-less replies stay ambiguous until reconnect."""

    from types import SimpleNamespace

    # Advance the robot clock explicitly: Windows scheduling can consume an entire
    # short travel budget while asyncio is waiting for the gate timeout.
    now = 100.0
    monkeypatch.setattr(robot_module, "time", SimpleNamespace(monotonic=lambda: now))
    adapter = _connected_adapter()
    sent: list[dict] = []

    class GimbalSocket:
        async def send(self, message: str) -> None:
            sent.append(json.loads(message))

    adapter._socket = GimbalSocket()
    monkeypatch.setattr(robot_module, "_GIMBAL_PREVIOUS_COMMAND_WAIT_SECONDS", 0.005)
    monkeypatch.setattr(robot_module, "_gimbal_command_budget_seconds", lambda _payload: 0.03)

    await adapter.set_gimbal(_gimbal_move(20))
    now += 0.04
    await adapter.set_gimbal(_gimbal_move(-20))

    # This could be A's very late OK or B's immediate OK; the protocol carries no id. It must
    # not open the gate while B's physical travel budget is still active.
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    with pytest.raises(TimeoutError, match="仍在执行"):
        await adapter.set_gimbal(_gimbal_move(35))
    assert [payload["gimbal_control"]["yaw_end"] for payload in sent] == [20, -20]

    now += 0.04
    await adapter.set_gimbal(_gimbal_move(35))
    assert [payload["gimbal_control"]["yaw_end"] for payload in sent] == [20, -20, 35]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [4, 8])
@pytest.mark.parametrize("overshoot", [False, True])
async def test_fixed_program_continues_after_pose_arrival_with_ambiguous_ok(
    tmp_path, monkeypatch, mode, overshoot
):
    """Replay the 09-19 failure through both cruise and the real hardware gate."""
    adapter = _connected_adapter()
    adapter._gimbal_terminal_replies_trusted = False
    sent = []
    wire_starts = []
    replies = []
    monkeypatch.setattr(cruise_module, "_CW_POSE_POLL_SECONDS", 0.001)
    monkeypatch.setattr(robot_module, "_GIMBAL_PREVIOUS_COMMAND_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_gimbal_command_budget_seconds", lambda _: 120)

    async def pose(yaw, pitch, zoom=1):
        await adapter._handle_message(json.dumps({"gimbal": {"yaw": yaw, "pitch": pitch, "zoom": zoom}}))

    async def feedback(command):
        await asyncio.sleep(0.005)
        await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
        assert not adapter._gimbal_ready.is_set()
        # Actual completion is much faster than the conservative travel estimate.
        yaw = command["yaw_end"]
        pitch = command["pitch_end"]
        if overshoot:
            yaw += -1.2 if yaw < 0 else 1.2
            pitch += -1.2 if pitch < 0 else 1.2
        else:
            yaw += 1 if yaw < 0 else -1
            pitch += 1 if pitch < 0 else -1
        for _ in range(3):
            await pose(yaw, pitch, command["zoom_end"])
            await asyncio.sleep(0.005)

    class Socket:
        async def send(self, message):
            command = json.loads(message)["gimbal_control"]
            sent.append((command["yaw_end"], command["pitch_end"]))
            wire_starts.append((command["yaw_start"], command["pitch_start"]))
            replies.append(asyncio.create_task(feedback(command)))

    adapter._socket = Socket()
    await pose(0, 0)
    events = EventHub()
    service = CruiseService(
        events, RobotService(events, adapter=adapter),
        CaptureService(events, path=tmp_path / "capture.json"),
    )
    config = CameraworkConfig(point_mode=mode, yaw_min=-90, yaw_max=90, pitch_min=-60, pitch_max=15)
    segment = CruiseSegment(index=0, path_name="route", goal_id=1)
    try:
        await asyncio.wait_for(
            service._run_camera_program(CruiseRun(segments=[segment]), segment, config, None),
            2,
        )
    finally:
        await asyncio.gather(*replies)
    pieces = camera_program(config)
    assert sent == [target for piece in pieces for target in piece.poses[1:]]
    assert len(segment.shots) == (8 if mode == 4 else 12)
    assert all(shot["status"] == "complete" for shot in segment.shots)
    if overshoot:
        assert any(yaw == -91.2 for yaw, _ in wire_starts)
        assert any(yaw == 91.2 for yaw, _ in wire_starts)
        assert any(pitch == -61.2 for _, pitch in wire_starts)
        assert any(pitch == 16.2 for _, pitch in wire_starts)


@pytest.mark.asyncio
async def test_fixed_pose_gate_requires_fresh_both_axes_and_ignores_late_ok(monkeypatch):
    adapter = _connected_adapter()
    adapter._gimbal_terminal_replies_trusted = False
    monkeypatch.setattr(robot_module, "_gimbal_command_budget_seconds", lambda _: 120)

    class Socket:
        async def send(self, message):
            pass

    adapter._socket = Socket()

    async def pose(**axes):
        await adapter._handle_message(json.dumps({"gimbal": axes}))

    # A partial sample from before the command must not contribute to arrival.
    await pose(yaw=89)
    await adapter.set_gimbal(_gimbal_move(90), context="cruise_fixed_piece")
    await pose(pitch=0)
    assert adapter._gimbal_pose_samples == 0
    await pose(yaw=89)
    assert adapter._gimbal_pose_samples == 1
    await pose(yaw=89)
    assert not adapter._gimbal_ready.is_set()
    await pose(pitch=5.01)
    assert adapter._gimbal_pose_samples == 0
    await pose(yaw=85, pitch=5)
    assert not adapter._gimbal_ready.is_set()
    await pose(yaw=89.3, pitch=0.1)
    assert adapter._gimbal_ready.is_set()

    await adapter.set_gimbal(_gimbal_move(-90), context="cruise_fixed_piece")
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    await pose(yaw=89.3, pitch=0.1)
    await pose(yaw=89.3, pitch=0.1)
    assert not adapter._gimbal_ready.is_set()
    await pose(yaw=-88, pitch=0)
    await pose(yaw=-89, pitch=0)
    assert adapter._gimbal_ready.is_set()
    adapter._clear_heartbeat_diagnostics()
    assert adapter._gimbal_pose_target is None
    assert adapter._gimbal_pose_samples == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("base,target,actual_base,actual_target", [(1.2, 1.5, 1.2, 1.5), (1, 3.5, 0.99, 3.52)])
async def test_zoom_pieces_wait_for_actual_zoom_and_restore_custom_base(
    tmp_path, monkeypatch, base, target, actual_base, actual_target
):
    adapter = _connected_adapter()
    adapter._gimbal_terminal_replies_trusted = False
    commands = asyncio.Queue()

    class Socket:
        async def send(self, message):
            await commands.put(json.loads(message)["gimbal_control"])

    adapter._socket = Socket()
    events = EventHub()
    service = CruiseService(events, RobotService(events, adapter=adapter),
                            CaptureService(events, path=tmp_path / "capture.json"))
    monkeypatch.setattr(cruise_module, "_CW_POSE_POLL_SECONDS", 0.001)
    config = CameraworkConfig(anchor_zoom=base, zoom_target=target)

    async def sample(zoom=None):
        gimbal = {"yaw": 0, "pitch": 0}
        if zoom is not None:
            gimbal["zoom"] = zoom
        await adapter._handle_message(json.dumps({"gimbal": gimbal}))
        await asyncio.sleep(0.005)

    await sample(actual_base)
    service._cw_zoom = base
    for start, end, actual_end in [(actual_base, target, actual_target), (actual_target, base, actual_base)]:
        move = asyncio.create_task(service._move_to_pose((0, 0), config, target_zoom=end))
        try:
            command = await asyncio.wait_for(commands.get(), 0.5)
            assert (command["zoom_start"], command["zoom_end"]) == (start, end)
            await sample()
            await sample()
            assert not move.done() and not adapter._gimbal_ready.is_set()
            await sample(start)
            await sample(start)
            assert not move.done() and not adapter._gimbal_ready.is_set()
            await sample(actual_end)
            assert not move.done()
            await sample(actual_end)
            await asyncio.wait_for(move, 0.5)
            assert adapter._gimbal_ready.is_set()
            assert service._cw_zoom == end
        finally:
            if not move.done():
                move.cancel()
                with suppress(asyncio.CancelledError):
                    await move


@pytest.mark.asyncio
async def test_position_feedback_does_not_complete_a_zoom_change():
    adapter = _connected_adapter()
    adapter._gimbal_terminal_replies_trusted = False

    class Socket:
        async def send(self, message):
            pass

    adapter._socket = Socket()
    move = _gimbal_move(20).model_copy(update={"zoom_end": 2})
    await adapter.set_gimbal(move, context="cruise_fixed_piece")
    for _ in range(2):
        await adapter._handle_message(json.dumps({"gimbal": {"yaw": 20, "pitch": 0}}))
    assert not adapter._gimbal_ready.is_set()


@pytest.mark.asyncio
async def test_cancelled_gimbal_while_queued_does_not_leave_a_phantom_command():
    """Cancellation before the socket-write boundary must release camera ownership."""

    adapter = _connected_adapter()

    class NeverWriteSocket:
        async def send(self, _message: str) -> None:
            raise AssertionError("queued command must never reach the socket")

    adapter._socket = NeverWriteSocket()
    await adapter._send_lock.acquire()
    command = asyncio.create_task(adapter.set_gimbal(_gimbal_move(20)))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
            if adapter._gimbal_inflight:
                break
        assert adapter._gimbal_inflight is True

        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command

        assert adapter._gimbal_inflight is False
        assert adapter._gimbal_release_deadline == 0
        assert adapter._gimbal_ready.is_set()
    finally:
        adapter._send_lock.release()
        if not command.done():
            command.cancel()
            with suppress(asyncio.CancelledError):
                await command


@pytest.mark.asyncio
async def test_gimbal_write_after_reconnect_rearms_physical_command_gate(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    sent: list[dict] = []

    class GimbalSocket:
        async def send(self, message: str) -> None:
            sent.append(json.loads(message))

    socket = GimbalSocket()

    async def reconnect_during_preflight():
        # This is the production race: _send_gimbal already claimed the old gate, then connect()
        # installs a replacement transport and clears old heartbeat/gimbal ownership.
        adapter._clear_heartbeat_diagnostics()
        adapter.state.connected = True
        adapter.state.connection_status = "connected"
        adapter._socket = socket
        return adapter.state

    adapter.connect = reconnect_during_preflight
    monkeypatch.setattr(robot_module, "_GIMBAL_PREVIOUS_COMMAND_WAIT_SECONDS", 0.005)
    monkeypatch.setattr(robot_module, "_gimbal_command_budget_seconds", lambda _payload: 0.05)

    await adapter.set_gimbal(_gimbal_move(20))
    assert adapter._gimbal_inflight is True
    assert not adapter._gimbal_ready.is_set()

    with pytest.raises(TimeoutError, match="仍在执行"):
        await adapter.set_gimbal(_gimbal_move(-20))
    assert [payload["gimbal_control"]["yaw_end"] for payload in sent] == [20]

    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    await adapter.set_gimbal(_gimbal_move(-20))
    assert [payload["gimbal_control"]["yaw_end"] for payload in sent] == [20, -20]


@pytest.mark.asyncio
async def test_real_websocket_cruise_times_out_bare_done_but_stops_and_saves_once(
    monkeypatch,
    tmp_path,
):
    """An ambiguous final point must not strand the real recording lifecycle."""

    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(robot_module, "_GIMBAL_PREVIOUS_COMMAND_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(cruise_module, "_ARRIVAL_TIMEOUT_SECONDS", 0.12)
    monkeypatch.setattr(cruise_module, "_CW_POSE_POLL_SECONDS", 0.005)

    commands: list[dict] = []
    goal_ids: list[int] = []
    gimbal_replies: list[str] = []
    premature_gimbal_commands: list[dict] = []
    server_errors: list[BaseException] = []
    background_replies: list[asyncio.Task] = []
    final_bare_done_sent = asyncio.Event()
    map_a_heartbeat_sent = asyncio.Event()
    bare_done_count = 0
    stop_followed_final_bare_done = False
    recording = False
    gimbal_busy = False
    media_url = "http://robot.local/media/cruise-e2e.mp4"

    def spawn_reply(coroutine) -> None:
        task = asyncio.create_task(coroutine)
        background_replies.append(task)

    async def robot_endpoint(connection) -> None:
        nonlocal bare_done_count, stop_followed_final_bare_done
        nonlocal gimbal_busy, recording
        send_lock = asyncio.Lock()

        async def send(payload: dict) -> None:
            async with send_lock:
                await connection.send(json.dumps(payload))

        async def send_pose(yaw: float, pitch: float, zoom: float = 1) -> None:
            await send({"gimbal": {"yaw": yaw, "pitch": pitch, "zoom": zoom, "mode": 1}})

        async def finish_gimbal(yaw: float, pitch: float, zoom: float) -> None:
            nonlocal gimbal_busy
            await asyncio.sleep(0.01)
            await send({"robot_gimbal_control": {"status": "ok"}})
            gimbal_replies.append("ok")
            gimbal_busy = False
            # Two separately received samples exercise the same physical-pose confirmation used
            # in production.  They intentionally arrive after the command write boundary.
            await asyncio.sleep(0.008)
            await send_pose(yaw, pitch, zoom)
            await asyncio.sleep(0.008)
            await send_pose(yaw, pitch, zoom)

        async def finish_goal(goal_id: int) -> None:
            nonlocal bare_done_count
            # Let the client apply robot_goal before status begins.  This mirrors the packet gap
            # in the field log and keeps every final bare status strictly post-ACK.
            await asyncio.sleep(0.03)
            if goal_id < 3:
                for status in ("going", "done"):
                    await send(
                        {
                            "naviagtion": {"status": "ready", "goal_status": status},
                            "task": {
                                "path_file": "route-a",
                                "goal_id": goal_id,
                                "goal_status": status,
                            },
                        }
                    )
                    await asyncio.sleep(0.008)
                return

            # The real incident's final point omitted both task identity and the short going
            # transition. Repetition stays ambiguous and must end at the bounded point timeout.
            for _ in range(3):
                await send({"naviagtion": {"status": "ready", "goal_status": "done"}})
                bare_done_count += 1
                await asyncio.sleep(0.01)
            final_bare_done_sent.set()

        async def finish_map_switch() -> None:
            # robot_switch_map only acknowledges the file.  This complete physical report must
            # be a later websocket message so RobotService can prove the accepted UI switch.
            # Send it exactly once: field firmware is not required to repeat the full frame on a
            # timer. Cruise must reuse this post-command, same-connection proof without issuing a
            # second disruptive map switch or waiting for a heartbeat cadence that may not exist.
            await asyncio.sleep(0.02)
            await send(
                {
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "map-a",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                }
            )
            map_a_heartbeat_sent.set()

        try:
            await send(
                {
                    "system": {"status": "ready", "battery": 90},
                    "map": {
                        "mode": "localization",
                        "name": "old-map",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                    "gimbal": {
                        "record_status": "idle",
                        "yaw": 0,
                        "pitch": 0,
                        "mode": 1,
                    },
                }
            )
            async for raw in connection:
                payload = json.loads(raw)
                commands.append(payload)
                if "get_map_list" in payload:
                    await send({"robot_map_list": ["map-a"]})
                elif "get_path_list" in payload:
                    await send({"robot_path_list": ["route-a"]})
                elif "set_switch_map" in payload:
                    await send({"robot_switch_map": "true"})
                    spawn_reply(finish_map_switch())
                elif payload.get("video_record", {}).get("start") == 0:
                    # Cross the primary deadline but remain inside the late-reply grace window.
                    await asyncio.sleep(0.03)
                    recording = True
                    await send(
                        {
                                "robot_video_record": {
                                    "start": 0,
                                    "status": "ok",
                                    # Matches the field log: Start succeeds without exposing the
                                    # final file. Stop is the sole owner of the downloadable URL.
                                    "url": "",
                                }
                        }
                    )
                elif "set_goal" in payload:
                    requested = payload["set_goal"]
                    goal_id = int(requested["goal_id"])
                    goal_ids.append(goal_id)
                    await send(
                        {
                            "robot_goal": {
                                "path_file": requested["path_name"],
                                "goal_id": goal_id,
                                "goal_object": requested.get("goal_object"),
                                "goal_check": "true",
                            }
                        }
                    )
                    spawn_reply(finish_goal(goal_id))
                elif "gimbal_control" in payload:
                    if gimbal_busy:
                        premature_gimbal_commands.append(payload)
                    gimbal_busy = True
                    target = payload["gimbal_control"]
                    await send({"robot_gimbal_control": {"status": "busy"}})
                    gimbal_replies.append("busy")
                    spawn_reply(
                        finish_gimbal(float(target["yaw_end"]), float(target["pitch_end"]), float(target["zoom_end"]))
                    )
                elif payload.get("video_record", {}).get("stop") == 0:
                    stop_followed_final_bare_done = final_bare_done_sent.is_set()
                    recording = False
                    await send(
                        {
                            "robot_video_record": {
                                "stop": 0,
                                "status": "ok",
                                "url": media_url,
                            }
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            server_errors.append(exc)

    downloads: list[str] = []

    class FileMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            downloads.append(url)
            target = tmp_path / "cruise-e2e.mp4"
            target.write_bytes(b"field-protocol-video")
            return MediaItem(path=str(target), kind="video", metadata=metadata or {})

    async with websockets.serve(robot_endpoint, "127.0.0.1", 0, ping_interval=None) as server:
        port = server.sockets[0].getsockname()[1]
        events = EventHub()
        adapter = HardwareRobotAdapter(events, f"ws://127.0.0.1:{port}")
        robot = RobotService(events, adapter=adapter, media=FileMedia())
        robot.resolve_media_url = lambda url: url

        # Reproduce the UI sequence: the operator selects a predefined map before opening the
        # cruise.  Wait for the post-ACK physical heartbeat, not merely the scalar ACK.
        manual_switch = await asyncio.wait_for(robot.switch_map("map-a"), timeout=1.0)
        assert manual_switch["ok"] is True
        await asyncio.wait_for(map_a_heartbeat_sent.wait(), timeout=0.5)

        async def wait_for_client_map_proof():
            while True:
                state = await robot.status()
                heartbeat = state.diagnostics.last_heartbeat
                payload = heartbeat.payload if heartbeat is not None else {}
                reported_map = payload.get("map") if isinstance(payload.get("map"), dict) else {}
                navigation = (
                    payload.get("naviagtion")
                    if isinstance(payload.get("naviagtion"), dict)
                    else {}
                )
                if (
                    state.map_name == "map-a"
                    and reported_map.get("name") == "map-a"
                    and reported_map.get("mode") == "localization"
                    and reported_map.get("status") == "ready"
                    and navigation.get("status") == "ready"
                ):
                    return state
                await asyncio.sleep(0.005)

        await asyncio.wait_for(wait_for_client_map_proof(), timeout=0.5)
        manual_switch_count = sum("set_switch_map" in payload for payload in commands)
        assert manual_switch_count == 1

        capture = CaptureService(events, path=tmp_path / "capture-sessions.json")
        config = CameraworkConfig(
            configured=True,
            anchor_yaw=0,
            anchor_pitch=0,
            anchor_zoom=1,
            yaw_min=-10,
            yaw_max=10,
            pitch_min=-5,
            pitch_max=5,
            zoom_min=1,
            zoom_max=1.5,
            speed_min=5,
            speed_max=5,
            anchor_time_percent=100,
            anchor_dwell_seconds=0.5,
        )
        cruise = CruiseService(events, robot, capture, lambda: config)
        cruise._point_dwell_baseline_seconds = 0.005
        request = CruiseRequest(
            title="real websocket replay",
            map_name="map-a",
            points=[
                CruisePoint(path_name="route-a", goal_id=1),
                CruisePoint(path_name="route-a", goal_id=2),
                CruisePoint(path_name="route-a", goal_id=3),
            ],
            record=True,
            auto_camerawork=True,
        )

        try:
            run = await asyncio.wait_for(cruise.start(request), timeout=1.0)
            await asyncio.wait_for(cruise._task, timeout=3.0)
            # The product deliberately does not let a cosmetic resting command own capture
            # completion. Keep the simulated robot alive long enough to finish that final
            # command so serialization assertions still cover its terminal reply.
            if background_replies:
                await asyncio.gather(*list(background_replies))
        finally:
            if cruise.is_running:
                await cruise.cancel()
            with suppress(Exception):
                await adapter.disconnect()

    if background_replies:
        results = await asyncio.gather(*background_replies, return_exceptions=True)
        server_errors.extend(
            result
            for result in results
            if isinstance(result, BaseException)
            # The cruise is already finalized here. Its one-way resting-anchor command may
            # still prompt a final telemetry frame while test teardown cleanly closes the socket.
            and not isinstance(result, ConnectionClosedOK)
        )

    assert server_errors == []
    assert recording is False
    assert run.status == "succeeded", run.error
    assert [segment.status for segment in run.segments] == ["arrived", "arrived", "failed"]
    assert run.segments[2].error == "Robot reported 'timeout' for this point"
    assert goal_ids == [1, 2, 3]
    assert bare_done_count == 3
    assert final_bare_done_sent.is_set()
    assert stop_followed_final_bare_done is True
    assert sum(payload.get("video_record", {}).get("start") == 0 for payload in commands) == 1
    assert sum(payload.get("video_record", {}).get("stop") == 0 for payload in commands) == 1
    assert sum("set_switch_map" in payload for payload in commands) == manual_switch_count == 1
    assert premature_gimbal_commands == []
    assert gimbal_replies and gimbal_replies == [
        status for _ in range(len(gimbal_replies) // 2) for status in ("busy", "ok")
    ]
    assert downloads == [media_url]
    assert run.media_local_path is not None
    assert (tmp_path / "cruise-e2e.mp4").is_file()
    assert capture.active_session() is None


@pytest.mark.asyncio
@pytest.mark.parametrize("succeeds_on", [1, 2, 3, None])
async def test_cruise_corrects_only_failed_pose_up_to_three_writes(tmp_path, monkeypatch, succeeds_on):
    """A completed-but-inaccurate move is retried inside the same shot, from actual pose."""
    adapter = _connected_adapter()
    sent = []

    class Socket:
        async def send(self, message):
            sent.append(json.loads(message)["gimbal_control"])

    adapter._socket = Socket()
    events = EventHub()
    service = CruiseService(events, RobotService(events, adapter=adapter),
                            CaptureService(events, path=tmp_path / "capture.json"))

    async def pose(yaw):
        await adapter._handle_message(json.dumps({"gimbal": {"yaw": yaw, "pitch": 0, "zoom": 1}}))

    await pose(0)

    async def finish_attempt(*args, **kwargs):
        await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
        reached = len(sent) == succeeds_on
        await pose(20 if reached else len(sent) * 2)
        return reached, True

    monkeypatch.setattr(service, "_await_camerawork_pose", finish_attempt)
    if succeeds_on is None:
        with pytest.raises(ValueError, match="已发送 3 次"):
            await service._move_to_pose((20, 0), CameraworkConfig())
    else:
        await service._move_to_pose((20, 0), CameraworkConfig())
    assert len(sent) == (succeeds_on or 3)
    assert [c["yaw_start"] for c in sent] == [0, 2, 4][:len(sent)]
    assert {c["yaw_end"] for c in sent} == {20}


@pytest.mark.asyncio
async def test_retry_orders_feedback_even_when_monotonic_clock_does_not_advance(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(robot_module, "time", SimpleNamespace(monotonic=lambda: 100.0))
    adapter = _connected_adapter()
    sent = []

    class Socket:
        async def send(self, message):
            sent.append(json.loads(message)["gimbal_control"])

    adapter._socket = Socket()
    sample = json.dumps({"gimbal": {"yaw": 2, "pitch": 0, "zoom": 1}})
    owner = await adapter.send_cruise_gimbal(_gimbal_move(20), context="cruise_fixed_piece")
    await adapter._handle_message(sample)
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    with pytest.raises(ValueError, match="新鲜角度反馈"):
        adapter._check_gimbal_retry(owner, sent[0])
    await adapter._handle_message(sample)
    adapter._check_gimbal_retry(owner, sent[0])
    assert len(sent) == 1  # Checking eligibility never sends a correction itself.


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["busy", "fail", "unknown", "no_reply", "ambiguous", "stale", "reconnect"])
async def test_cruise_does_not_retry_without_definite_completion(tmp_path, monkeypatch, unsafe):
    adapter = _connected_adapter()
    sent = []

    class Socket:
        async def send(self, message):
            sent.append(json.loads(message))

    adapter._socket = Socket()
    events = EventHub()
    service = CruiseService(events, RobotService(events, adapter=adapter),
                            CaptureService(events, path=tmp_path / "capture.json"))
    sample = json.dumps({"gimbal": {"yaw": 0, "pitch": 0, "zoom": 1}})
    await adapter._handle_message(sample)

    async def timed_out(*args, **kwargs):
        if unsafe == "ambiguous":
            adapter._gimbal_terminal_replies_trusted = False
        if unsafe != "no_reply":
            status = unsafe if unsafe in {"busy", "fail", "unknown"} else "ok"
            await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": status}}))
        if unsafe != "stale":
            await adapter._handle_message(sample)
        if unsafe == "reconnect":
            adapter._connection_epoch += 1
            adapter._clear_heartbeat_diagnostics()
            await adapter._handle_message(sample)
        return False, True

    monkeypatch.setattr(service, "_await_camerawork_pose", timed_out)
    with pytest.raises(ValueError, match="未补发"):
        await service._move_to_pose((20, 0), CameraworkConfig())
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("interruption", ["busy", "reconnect", "cancel", "superseded"])
async def test_retry_revalidates_owner_at_socket_write(interruption):
    adapter = _connected_adapter()
    sent = []
    cancelled = asyncio.Event()

    class Socket:
        async def send(self, message):
            sent.append(json.loads(message))

    adapter._socket = Socket()
    command = _gimbal_move(20)
    owner = await adapter.send_cruise_gimbal(command, context="cruise_fixed_piece")
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    await adapter._handle_message(json.dumps({"gimbal": {"yaw": 2, "pitch": 0, "zoom": 1}}))
    await adapter._send_lock.acquire()
    retry = asyncio.create_task(adapter.send_cruise_gimbal(
        command, context="cruise_fixed_piece", retry_owner=owner, cancelled=cancelled.is_set,
    ))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        if interruption == "busy":
            await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "busy"}}))
        elif interruption == "reconnect":
            adapter._connection_epoch += 1
        elif interruption == "superseded":
            adapter._gimbal_command_revision += 1
        else:
            cancelled.set()
        adapter._send_lock.release()
        with pytest.raises(asyncio.CancelledError if interruption == "cancel" else ValueError):
            await retry
        assert len(sent) == 1
    finally:
        if adapter._send_lock.locked():
            adapter._send_lock.release()
        if not retry.done():
            retry.cancel()
            with suppress(asyncio.CancelledError):
                await retry


@pytest.mark.asyncio
@pytest.mark.parametrize("queued_yaw", [4, 15])
async def test_retry_uses_latest_start_and_does_not_correct_within_five_degrees(queued_yaw):
    adapter = _connected_adapter()
    sent = []

    class Socket:
        async def send(self, message):
            sent.append(json.loads(message)["gimbal_control"])

    adapter._socket = Socket()
    command = _gimbal_move(20)
    owner = await adapter.send_cruise_gimbal(command, context="cruise_fixed_piece")
    await adapter._handle_message(json.dumps({"robot_gimbal_control": {"status": "ok"}}))
    await adapter._handle_message(json.dumps({"gimbal": {"yaw": 2, "pitch": 0, "zoom": 1}}))
    await adapter._send_lock.acquire()
    retry = asyncio.create_task(adapter.send_cruise_gimbal(
        command, context="cruise_fixed_piece", retry_owner=owner,
    ))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        await adapter._handle_message(json.dumps({"gimbal": {"yaw": queued_yaw, "pitch": 0, "zoom": 1}}))
        adapter._send_lock.release()
        if queued_yaw == 15:
            with pytest.raises(ValueError, match="已在容差内"):
                await retry
            assert len(sent) == 1
        else:
            await retry
            assert len(sent) == 2
            assert sent[-1]["yaw_start"] == queued_yaw
    finally:
        if adapter._send_lock.locked():
            adapter._send_lock.release()
        if not retry.done():
            retry.cancel()
            with suppress(asyncio.CancelledError):
                await retry
