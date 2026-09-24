import asyncio
import json
import time

import pytest

from automated_video_editing_backend.core.diagnostics import safe_url
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CameraAngle,
    GimbalCommandDiagnostic,
    GimbalMoveRequest,
    GoalCommandAttemptDiagnostic,
    MediaItem,
    MoveCommand,
    RobotGoalCommand,
    RobotMode,
    RobotState,
)
from automated_video_editing_backend.services import robot as robot_module
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.media import resolve_robot_media_url
from automated_video_editing_backend.services.robot import (
    HardwareRobotAdapter,
    RobotService,
    refuse_if_unfit_to_drive,
)


@pytest.mark.asyncio
async def test_robot_without_hardware_url_starts_disconnected():
    robot = RobotService(EventHub())
    state = await robot.status()

    assert state.adapter == RobotMode.REAL
    assert state.connected is False
    assert state.connection_status == "disconnected"
    assert state.error == "Robot websocket URL is not configured"

    with pytest.raises(ValueError):
        await robot.move(MoveCommand(direction="forward"))


@pytest.mark.asyncio
async def test_hardware_adapter_uses_documented_websocket_protocol():
    received: list[dict] = []

    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_connect():
        return adapter.state

    adapter.connect = fake_connect

    class FakeRobotSocket:
        async def send(self, message):
            payload = json.loads(message)
            received.append(payload)
            if "get_map_list" in payload:
                await adapter._handle_message(json.dumps({"robot_map_list": ["map1"]}))
            elif "set_switch_map" in payload:
                await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))
            elif "get_path_list" in payload:
                await adapter._handle_message(json.dumps({"robot_path_list": ["path1"]}))
            elif "set_goal" in payload:
                requested = payload["set_goal"]
                await adapter._handle_message(json.dumps({
                    "robot_goal": {
                        "path_file": requested["path_name"],
                        "goal_id": requested["goal_id"],
                        "goal_object": requested["goal_object"],
                        "goal_check": "true",
                    }
                }))
            elif "video_record" in payload:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        **payload["video_record"],
                        "status": " OK ",
                        "url": "robot://video.mp4",
                    }
                }))
            elif "take_photo" in payload:
                await adapter._handle_message(json.dumps({
                    "robot_take_photo": {"status": "Ok", "url": "robot://photo.jpg"}
                }))

        async def close(self):
            return None

    adapter._socket = FakeRobotSocket()
    await adapter._handle_message(json.dumps({
        "system": {"status": "ready", "battery": 85},
        "map": {"mode": "localization", "name": "map1", "status": "ready"},
        "naviagtion": {"status": "ready", "goal_status": "going"},
            "task": {
                "path_file": "path1",
                "goal_id": 1,
                "goal_object": "car",
                "goal_status": "done",
                "object_status": "faild",
            },
            "gimbal": {"record_status": "idle", "yaw": 45, "pitch": 10, "mode": 1},
    }))

    try:
        assert await adapter.map_list() == ["map1"]
        assert await adapter.switch_map("map1") == {
            "map_name": "map1",
            "ok": True,
            "raw": "true",
        }
        assert await adapter.path_list("map1") == ["path1"]
        goal = await adapter.set_goal(
            RobotGoalCommand(path_name="path1", goal_id=3, goal_object="car")
        )
        assert goal["goal_check"] == "true"
        camera_command = adapter.state.diagnostics.last_gimbal_command
        assert camera_command is not None
        assert camera_command.context == "goal_object_alignment"
        assert camera_command.base_motion_intent == "moving"
        assert camera_command.payload == {
            "set_goal": {"path_name": "path1", "goal_id": 3, "goal_object": "car"}
        }
        navigation_goal = await adapter.set_goal(
            RobotGoalCommand(path_name="path1", goal_id=4, goal_object="")
        )
        assert navigation_goal["goal_check"] == "true"
        navigation_command = adapter.state.diagnostics.last_goal_command
        assert navigation_command is not None
        assert navigation_command.context == "cruise_navigation_goal"
        assert navigation_command.payload == {
            "set_goal": {"path_name": "path1", "goal_id": 4, "goal_object": ""}
        }
        assert adapter.state.diagnostics.last_gimbal_command is None
        state = await adapter.start_recording()
        assert state.recording is True
        photo = await adapter.capture_photo()
        assert photo["url"] == "robot://photo.jpg"
        state = await adapter.stop_recording()
        assert state.recording is False

        assert {"get_map_list": "all"} in received
        assert {"set_switch_map": "map1"} in received
        assert {"get_path_list": "map1"} in received
        assert {
            "set_goal": {"path_name": "path1", "goal_id": 3, "goal_object": "car"}
        } in received
        assert {
            "set_goal": {"path_name": "path1", "goal_id": 4, "goal_object": ""}
        } in received
        assert {"video_record": {"start": 0, "resolution": 4}} in received
        assert {"video_record": {"stop": 0}} in received
        assert {"take_photo": {"counter": 1, "gap": 0}} in received
        assert adapter.state.object_status == "failed"
        assert adapter.state.battery == 85
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_heartbeat_diagnostics_keep_raw_ownership_and_sample_pose_repeats(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._pending_goal = RobotGoalCommand(
        path_name="route-a",
        goal_id=7,
        goal_object=None,
    )
    emitted = []
    monkeypatch.setattr(
        robot_module,
        "log_event",
        lambda level, event, **fields: emitted.append((level, event, fields)),
    )

    base = {
        "system": {"status": "ready"},
        "map": {"name": "map-a", "mode": "localization", "status": "ready"},
        "naviagtion": {"status": "ready", "goal_status": "going"},
        "task": {"path_file": "route-a", "goal_id": 7, "goal_status": "going"},
    }
    await adapter._handle_message(json.dumps({
        **base,
        "gimbal": {"record_status": "recording", "yaw": 1.0, "pitch": -2.0},
    }))
    await adapter._handle_message(json.dumps({
        **base,
        "gimbal": {"record_status": "recording", "yaw": 1.5, "pitch": -1.5},
    }))
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 1.5, "pitch": -1.5},
    }))

    heartbeat_events = [
        fields for _level, event, fields in emitted if event == "robot.heartbeat.received"
    ]
    assert len(heartbeat_events) == 2
    first, transition = heartbeat_events
    assert first["connection_epoch"] == adapter.connection_generation()
    assert first["pending_goal"] == {
        "path_name": "route-a",
        "goal_id": 7,
        "goal_object": None,
        "write_confirmed": False,
    }
    assert first["payload"]["task"] == base["task"]
    assert first["payload"]["gimbal"]["yaw"] == 1.0
    assert first["received_monotonic_seconds"] > 0
    assert transition["payload"] == {
        "gimbal": {"record_status": "idle", "yaw": 1.5, "pitch": -1.5}
    }
    assert transition["suppressed_since_previous"] == 1


@pytest.mark.parametrize("reported", [None, "", "unknown", "stopping"])
def test_invalid_record_status_never_claims_the_camera_is_idle(reported):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.recording = True
    adapter._recording_status_known = False
    adapter._recording_stop_required = True
    adapter._recording_reply_recovery = (7, "stop")

    adapter._apply_protocol_state({
        "gimbal": {"record_status": reported, "yaw": 12, "pitch": -3}
    })

    assert adapter.state.recording is True
    assert adapter.recording_status_known() is False
    assert adapter._recording_stop_required is True
    assert adapter._recording_reply_recovery == (7, "stop")
    # Bad recording state must not discard valid pose data from the same heartbeat.
    assert adapter.heartbeat_yaw() == 12
    assert adapter.heartbeat_pitch() == -3


@pytest.mark.asyncio
async def test_rejected_map_switch_never_relabels_the_active_map():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {"mode": "localization", "name": "old-map", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })

    async def connected():
        return adapter.state

    class RejectingSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({"robot_switch_map": "false"}))

    adapter.connect = connected
    adapter._socket = RejectingSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="拒绝切换"):
        await robot.switch_map_and_confirm("new-map", timeout_s=0.02)

    assert adapter.state.map_name == "old-map"


@pytest.mark.asyncio
async def test_cruise_map_switch_waits_for_a_fresh_complete_ready_heartbeat():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {"mode": "localization", "name": "old-map", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })
    reports = []

    async def connected():
        return adapter.state

    class SwitchingSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

            async def report():
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                reports.append("partial")
                await adapter._handle_message(json.dumps({"gimbal": {"yaw": 2, "pitch": 1}}))
                await asyncio.sleep(0.01)
                reports.append("ready")
                await adapter._handle_message(json.dumps({
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "new-map",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                }))

            asyncio.create_task(report())

    adapter.connect = connected
    # This is a protocol-state unit test, not a transport integration test.  Stub status too:
    # HardwareRobotAdapter.status() deliberately starts its real reconnect loop, which can race
    # the synthetic heartbeats below and make the result depend on DNS timing for robot.local.
    adapter.status = connected
    adapter._socket = SwitchingSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    state = await robot.switch_map_and_confirm("new-map", timeout_s=0.2)

    assert reports == ["partial", "ready"]
    assert state.map_name == "new-map"
    assert state.map_mode == "localization"
    assert state.map_status == "ready"


@pytest.mark.asyncio
async def test_complete_map_proof_survives_a_later_gimbal_only_heartbeat(
    monkeypatch,
):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def connected():
        return adapter.state

    report_tasks = []

    class SwitchingSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

            async def report():
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                await adapter._handle_message(json.dumps({
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "new-map",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                }))
                # This is the real cadence that previously erased the usable map proof before
                # the 100 ms confirmation poll observed it.
                await adapter._handle_message(json.dumps({
                    "gimbal": {"yaw": 12, "pitch": -3}
                }))

            report_tasks.append(asyncio.create_task(report()))

    adapter.connect = connected
    adapter.status = connected
    adapter._socket = SwitchingSocket()
    robot = RobotService(EventHub(), adapter=adapter)
    monkeypatch.setattr(robot_module, "_MAP_SWITCH_CONFIRM_POLL_SECONDS", 0.01)

    state = await robot.switch_map_and_confirm("new-map", timeout_s=0.2)
    await asyncio.gather(*report_tasks)

    assert state.map_name == "new-map"
    assert state.diagnostics.last_heartbeat is not None
    assert set(state.diagnostics.last_heartbeat.payload) == {"gimbal"}
    complete = adapter.complete_map_heartbeat()
    assert complete is not None
    assert complete.payload["map"]["name"] == "new-map"


@pytest.mark.asyncio
async def test_map_ack_bundled_with_complete_target_heartbeat_is_not_missed(monkeypatch):
    # Windows can return the same wall-clock timestamp for the command boundary and an
    # immediate bundled heartbeat. Physical frame order, not clock resolution, proves freshness.
    frozen_wall_clock = robot_module.utc_now()
    monkeypatch.setattr(robot_module, "utc_now", lambda: frozen_wall_clock)
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def connected():
        return adapter.state

    class ImmediateSwitchSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_switch_map": "true",
                "system": {"status": "ready"},
                "map": {
                    "mode": "localization",
                    "name": "new-map",
                    "status": "ready",
                },
                "naviagtion": {"status": "ready"},
            }))

    adapter.connect = connected
    adapter.status = connected
    adapter._socket = ImmediateSwitchSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    state = await robot.switch_map_and_confirm("new-map", timeout_s=0.03)

    assert state.map_name == "new-map"


@pytest.mark.asyncio
async def test_same_timestamp_pre_switch_map_heartbeat_remains_stale(monkeypatch):
    frozen_wall_clock = robot_module.utc_now()
    monkeypatch.setattr(robot_module, "utc_now", lambda: frozen_wall_clock)
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {
            "mode": "localization",
            "name": "new-map",
            "status": "ready",
        },
        "naviagtion": {"status": "ready"},
    })

    async def connected():
        return adapter.state

    class AckOnlySocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

    adapter.connect = connected
    adapter.status = connected
    adapter._socket = AckOnlySocket()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(TimeoutError, match="尚未收到新的地图心跳"):
        await robot.switch_map_and_confirm("new-map", timeout_s=0.01)


@pytest.mark.asyncio
async def test_map_boundary_follows_reconnect_at_physical_write(monkeypatch):
    frozen_wall_clock = robot_module.utc_now()
    monkeypatch.setattr(robot_module, "utc_now", lambda: frozen_wall_clock)
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._connection_epoch = 3
    for yaw in range(4):
        adapter._apply_protocol_state({"gimbal": {"yaw": yaw, "pitch": 0}})

    reconnected = False

    async def reconnect_before_write():
        nonlocal reconnected
        if not reconnected:
            reconnected = True
            adapter._connection_epoch += 1
            adapter._clear_heartbeat_diagnostics()
            adapter.state.connected = True
            adapter.state.connection_status = "connected"
        return adapter.state

    class ImmediateSwitchSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_switch_map": "true",
                "system": {"status": "ready"},
                "map": {
                    "mode": "localization",
                    "name": "new-map",
                    "status": "ready",
                },
                "naviagtion": {"status": "ready"},
            }))

    adapter.connect = reconnect_before_write
    adapter.status = reconnect_before_write
    adapter._socket = ImmediateSwitchSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    state = await robot.switch_map_and_confirm("new-map", timeout_s=0.03)

    assert state.map_name == "new-map"
    assert adapter.map_switch_write_boundary() == (4, -1)


@pytest.mark.asyncio
async def test_map_switch_does_not_compose_ready_state_across_split_heartbeats(
    monkeypatch,
):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {"mode": "localization", "name": "old-map", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })
    partial_reports_sent = asyncio.Event()
    release_complete_report = asyncio.Event()
    report_tasks = []

    async def connected():
        return adapter.state

    class SwitchingSocket:
        async def send(self, message):
            assert json.loads(message) == {"set_switch_map": "new-map"}
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

            async def report():
                # Both reports are newer than the switch ACK, but neither is independently a
                # complete readiness report.  In particular, navigation-ready must not inherit
                # the map mode/status from the old map or the name-only report before it.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                await adapter._handle_message(json.dumps({"map": {"name": "new-map"}}))
                await adapter._handle_message(json.dumps({
                    "naviagtion": {"status": "ready"}
                }))
                partial_reports_sent.set()
                await release_complete_report.wait()
                await adapter._handle_message(json.dumps({
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "new-map",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                }))

            report_tasks.append(asyncio.create_task(report()))

    adapter.connect = connected
    adapter.status = connected
    adapter._socket = SwitchingSocket()
    robot = RobotService(EventHub(), adapter=adapter)
    monkeypatch.setattr(robot_module, "_MAP_SWITCH_CONFIRM_POLL_SECONDS", 0.005)

    switch_task = asyncio.create_task(
        robot.switch_map_and_confirm("new-map", timeout_s=0.2)
    )
    await asyncio.wait_for(partial_reports_sent.wait(), timeout=0.1)
    await asyncio.sleep(0.02)
    assert not switch_task.done()

    release_complete_report.set()
    state = await asyncio.wait_for(switch_task, timeout=0.1)
    await asyncio.gather(*report_tasks)

    assert state.map_name == "new-map"
    assert state.map_mode == "localization"
    assert state.map_status == "ready"


@pytest.mark.asyncio
async def test_pending_manual_switch_to_another_map_cannot_reuse_reported_target(
    monkeypatch,
):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {"mode": "localization", "name": "map-a", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })
    requested_maps = []
    report_tasks = []

    async def connected():
        return adapter.state

    class SwitchingSocket:
        async def send(self, message):
            requested_map = json.loads(message)["set_switch_map"]
            requested_maps.append(requested_map)
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))
            if requested_map != "map-a":
                return

            async def report_ready_map_a():
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                await adapter._handle_message(json.dumps({
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "map-a",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                }))

            report_tasks.append(asyncio.create_task(report_ready_map_a()))

    adapter.connect = connected
    adapter.status = connected
    adapter._socket = SwitchingSocket()
    robot = RobotService(EventHub(), adapter=adapter)
    monkeypatch.setattr(robot_module, "_MAP_SWITCH_CONFIRM_POLL_SECONDS", 0.005)

    await robot.switch_map("map-b")
    assert adapter.state.map_name == "map-a"

    state = await robot.switch_map_and_confirm("map-a", timeout_s=0.1)
    await asyncio.gather(*report_tasks)

    assert requested_maps == ["map-b", "map-a"]
    assert state.map_name == "map-a"


@pytest.mark.asyncio
async def test_timed_out_reused_map_switch_is_not_reused_by_the_next_retry():
    class NeverReadyAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, connection_status="connected")
            self.switches = []

        async def status(self):
            return self.state

        async def switch_map(self, map_name):
            self.switches.append(map_name)
            return {"map_name": map_name, "ok": True, "raw": "true"}

    adapter = NeverReadyAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    await robot.switch_map("map-a")
    with pytest.raises(TimeoutError, match="未在"):
        await robot.switch_map_and_confirm("map-a", timeout_s=0.001)
    assert adapter.switches == ["map-a"]

    with pytest.raises(TimeoutError, match="未在"):
        await robot.switch_map_and_confirm("map-a", timeout_s=0.001)
    assert adapter.switches == ["map-a", "map-a"]


@pytest.mark.asyncio
async def test_manual_map_switch_is_not_reused_after_connection_generation_changes():
    class ReconnectingAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, connection_status="connected")
            self.switches = []
            self.generation = 1

        async def status(self):
            return self.state

        async def switch_map(self, map_name):
            self.switches.append(map_name)
            return {"map_name": map_name, "ok": True, "raw": "true"}

        def connection_generation(self):
            return self.generation

    adapter = ReconnectingAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    await robot.switch_map("map-a")
    adapter.generation += 1
    with pytest.raises(TimeoutError, match="未在"):
        await robot.switch_map_and_confirm("map-a", timeout_s=0.001)

    assert adapter.switches == ["map-a", "map-a"]


@pytest.mark.asyncio
async def test_reused_manual_map_switch_accepts_its_single_post_ack_readiness_proof():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    switches = []

    async def connected():
        return adapter.state

    class SwitchingSocket:
        async def send(self, message):
            switches.append(json.loads(message)["set_switch_map"])
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

    adapter.connect = connected
    adapter.status = connected
    adapter._socket = SwitchingSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    await robot.switch_map("map-a")
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {"mode": "localization", "name": "map-a", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })
    complete = adapter.complete_map_heartbeat()
    assert complete is not None
    assert robot._pending_map_switch_after is not None

    state = await robot.switch_map_and_confirm("map-a", timeout_s=0.1)

    assert state.map_name == "map-a"
    assert switches == ["map-a"]


@pytest.mark.asyncio
async def test_disconnected_state_cannot_reuse_a_ready_map_heartbeat_as_confirmation():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def connected():
        return adapter.state

    class DisconnectingSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({"robot_switch_map": "true"}))

            async def report_then_disconnect():
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                await adapter._handle_message(json.dumps({
                    "system": {"status": "ready"},
                    "map": {
                        "mode": "localization",
                        "name": "new-map",
                        "status": "ready",
                    },
                    "naviagtion": {"status": "ready"},
                }))
                adapter.state.connected = False
                adapter.state.connection_status = "reconnecting"

            asyncio.create_task(report_then_disconnect())

    adapter.connect = connected
    # Keep the real reconnect loop out of this synthetic state-transition test.  The explicit
    # state mutation below is the connection loss that switch_map_and_confirm must observe.
    adapter.status = connected
    adapter._socket = DisconnectingSocket()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(TimeoutError, match="连接已断开"):
        await robot.switch_map_and_confirm("new-map", timeout_s=0.03)


class _GoalAttemptEventHub(EventHub):
    def __init__(self):
        super().__init__()
        self.robot_states = []

    async def publish(self, event_type, data=None):
        if event_type == "ROBOT_STATE":
            self.robot_states.append(data or {})
        await super().publish(event_type, data)


def _goal_attempt_adapter(events=None):
    adapter = HardwareRobotAdapter(events or EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_connect():
        return adapter.state

    adapter.connect = fake_connect
    return adapter


async def _accept_diagnostic_goal(adapter, command):
    class EchoingSocket:
        async def send(self, message):
            requested = json.loads(message)["set_goal"]
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": requested["path_name"],
                    "goal_id": requested["goal_id"],
                    "goal_object": requested["goal_object"],
                    "goal_check": "true",
                }
            }))

    adapter._socket = EchoingSocket()
    return await adapter.set_goal(command)


def _published_goal_attempt_outcomes(events):
    outcomes = []
    for state in events.robot_states:
        attempt = state.get("diagnostics", {}).get("last_goal_attempt")
        if attempt:
            outcomes.append(attempt.get("outcome"))
    return outcomes


@pytest.mark.asyncio
async def test_goal_attempt_is_observable_after_write_while_awaiting_reply_then_accepted():
    events = _GoalAttemptEventHub()
    adapter = _goal_attempt_adapter(events)
    written = asyncio.Event()
    sent_payload = None

    class DelayedReplySocket:
        async def send(self, message):
            nonlocal sent_payload
            sent_payload = json.loads(message)
            written.set()

    adapter._socket = DelayedReplySocket()
    command = RobotGoalCommand(path_name="path-a", goal_id=1)
    request = asyncio.create_task(adapter.set_goal(command))
    try:
        await written.wait()
        await asyncio.sleep(0)
        attempt = adapter.state.diagnostics.last_goal_attempt
        assert attempt is not None
        assert attempt.context == "cruise_navigation_goal"
        assert attempt.base_motion_intent == "moving"
        assert attempt.payload == {
            "set_goal": {"path_name": "path-a", "goal_id": 1, "goal_object": None}
        }
        assert attempt.outcome == "awaiting_reply"
        assert attempt.completed_at is None
        assert attempt.error is None
        assert "awaiting_reply" in _published_goal_attempt_outcomes(events)

        await adapter._handle_message(json.dumps({
            "robot_goal": {
                "path_file": "path-a",
                "goal_id": 1,
                "goal_object": None,
                "goal_check": "true",
            }
        }))
        result = await request
    finally:
        if not request.done():
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": "path-a", "goal_id": 1,
                    "goal_object": None, "goal_check": "true",
                }
            }))
            try:
                await request
            except Exception:
                pass

    assert sent_payload == {
        "set_goal": {"path_name": "path-a", "goal_id": 1, "goal_object": None}
    }
    assert result["goal_check"] == "true"
    accepted = adapter.state.diagnostics.last_goal_attempt
    assert accepted is not None
    assert accepted.attempt_id == attempt.attempt_id
    assert accepted.outcome == "accepted"
    assert accepted.completed_at is not None
    assert accepted.error is None
    assert adapter.state.diagnostics.last_goal_command is not None
    assert adapter.state.diagnostics.last_goal_command.payload == accepted.payload
    assert "accepted" in _published_goal_attempt_outcomes(events)


@pytest.mark.asyncio
async def test_rejected_goal_attempt_is_visible_without_replacing_last_accepted_goal():
    events = _GoalAttemptEventHub()
    adapter = _goal_attempt_adapter(events)
    await _accept_diagnostic_goal(
        adapter,
        RobotGoalCommand(path_name="path-a", goal_id=1),
    )
    previous_attempt = adapter.state.diagnostics.last_goal_attempt
    previous_command = adapter.state.diagnostics.last_goal_command

    class RejectingSocket:
        async def send(self, message):
            requested = json.loads(message)["set_goal"]
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": requested["path_name"],
                    "goal_id": requested["goal_id"],
                    "goal_object": requested["goal_object"],
                    "goal_check": "false",
                    "message": "point not found",
                }
            }))

    adapter._socket = RejectingSocket()
    result = await adapter.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))

    rejected = adapter.state.diagnostics.last_goal_attempt
    assert previous_attempt is not None
    assert rejected is not None
    assert rejected.attempt_id != previous_attempt.attempt_id
    assert rejected.payload["set_goal"]["goal_id"] == 2
    assert rejected.outcome == "rejected"
    assert rejected.completed_at is not None
    assert rejected.error == "point not found"
    assert result["goal_check"] == "false"
    assert adapter.state.diagnostics.last_goal_command == previous_command
    assert adapter.state.diagnostics.last_goal_command.payload["set_goal"]["goal_id"] == 1
    assert _published_goal_attempt_outcomes(events)[-1] == "rejected"


@pytest.mark.asyncio
async def test_timed_out_goal_attempt_is_visible_without_replacing_last_accepted_goal():
    events = _GoalAttemptEventHub()
    adapter = _goal_attempt_adapter(events)
    await _accept_diagnostic_goal(
        adapter,
        RobotGoalCommand(path_name="path-a", goal_id=1),
    )
    previous_command = adapter.state.diagnostics.last_goal_command

    class SilentSocket:
        async def send(self, _message):
            return None

    adapter._socket = SilentSocket()
    original_request = adapter._request

    async def quick_request(payload, response_key, timeout_s=5.0):
        del timeout_s
        return await original_request(payload, response_key, timeout_s=0.01)

    adapter._request = quick_request
    with pytest.raises(TimeoutError, match="等待机器人回复超时"):
        await adapter.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))

    timed_out = adapter.state.diagnostics.last_goal_attempt
    assert timed_out is not None
    assert timed_out.payload["set_goal"]["goal_id"] == 2
    assert timed_out.outcome == "reply_timeout"
    assert timed_out.completed_at is not None
    assert "等待机器人回复超时" in str(timed_out.error)
    assert adapter.state.diagnostics.last_goal_command == previous_command
    assert adapter.state.diagnostics.last_goal_command.payload["set_goal"]["goal_id"] == 1
    assert _published_goal_attempt_outcomes(events)[-1] == "reply_timeout"


@pytest.mark.asyncio
async def test_mismatched_goal_reply_marks_attempt_without_replacing_last_accepted_goal():
    events = _GoalAttemptEventHub()
    adapter = _goal_attempt_adapter(events)
    await _accept_diagnostic_goal(
        adapter,
        RobotGoalCommand(path_name="path-a", goal_id=1),
    )
    previous_command = adapter.state.diagnostics.last_goal_command

    class PreviousGoalReplySocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": "path-a", "goal_id": 1,
                    "goal_object": None, "goal_check": "true",
                }
            }))

    adapter._socket = PreviousGoalReplySocket()
    with pytest.raises(ValueError, match="目标点与本次指令不一致"):
        await adapter.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))

    mismatch = adapter.state.diagnostics.last_goal_attempt
    assert mismatch is not None
    assert mismatch.payload["set_goal"]["goal_id"] == 2
    assert mismatch.outcome == "reply_mismatch"
    assert mismatch.completed_at is not None
    assert "目标点与本次指令不一致" in str(mismatch.error)
    assert adapter.state.diagnostics.last_goal_command == previous_command
    assert adapter.state.diagnostics.last_goal_command.payload["set_goal"]["goal_id"] == 1
    assert _published_goal_attempt_outcomes(events)[-1] == "reply_mismatch"


@pytest.mark.asyncio
async def test_socket_send_failure_never_creates_or_overwrites_a_goal_attempt():
    class FailingSocket:
        async def send(self, _message):
            raise OSError("socket write failed")

    fresh_events = _GoalAttemptEventHub()
    fresh = _goal_attempt_adapter(fresh_events)
    fresh._socket = FailingSocket()
    with pytest.raises(OSError, match="socket write failed"):
        await fresh.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))
    assert fresh.state.diagnostics.last_goal_attempt is None
    assert fresh.state.diagnostics.last_goal_command is None
    assert _published_goal_attempt_outcomes(fresh_events) == []

    existing_events = _GoalAttemptEventHub()
    existing = _goal_attempt_adapter(existing_events)
    await _accept_diagnostic_goal(
        existing,
        RobotGoalCommand(path_name="path-a", goal_id=1),
    )
    previous_attempt = existing.state.diagnostics.last_goal_attempt
    previous_command = existing.state.diagnostics.last_goal_command
    existing._socket = FailingSocket()
    with pytest.raises(OSError, match="socket write failed"):
        await existing.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))

    assert existing.state.diagnostics.last_goal_attempt == previous_attempt
    assert existing.state.diagnostics.last_goal_attempt.outcome == "accepted"
    assert existing.state.diagnostics.last_goal_command == previous_command
    assert existing.state.diagnostics.last_goal_command.payload["set_goal"]["goal_id"] == 1


@pytest.mark.asyncio
async def test_connection_failure_after_goal_write_marks_attempt_as_reply_error():
    events = _GoalAttemptEventHub()
    adapter = _goal_attempt_adapter(events)
    written = asyncio.Event()

    class WrittenSocket:
        async def send(self, _message):
            written.set()

    adapter._socket = WrittenSocket()
    request = asyncio.create_task(
        adapter.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))
    )
    await written.wait()
    await asyncio.sleep(0)

    awaiting = adapter.state.diagnostics.last_goal_attempt
    assert awaiting is not None
    assert awaiting.outcome == "awaiting_reply"
    adapter._fail_pending(ConnectionError("connection lost after write"))

    with pytest.raises(ConnectionError, match="connection lost after write"):
        await request

    failed = adapter.state.diagnostics.last_goal_attempt
    assert failed is not None
    assert failed.attempt_id == awaiting.attempt_id
    assert failed.payload["set_goal"]["goal_id"] == 2
    assert failed.outcome == "reply_error"
    assert failed.completed_at is not None
    assert failed.error == "connection lost after write"
    assert adapter.state.diagnostics.last_goal_command is None
    assert _published_goal_attempt_outcomes(events)[-1] == "reply_error"


@pytest.mark.asyncio
async def test_rejected_object_goal_does_not_supersede_previous_gimbal_target():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    previous = GimbalCommandDiagnostic(
        context="manual",
        payload={"gimbal_control": {"yaw_end": 5, "pitch_end": -2}},
    )
    adapter.state.diagnostics.last_gimbal_command = previous

    async def fake_connect():
        return adapter.state

    class RejectingSocket:
        async def send(self, message):
            payload = json.loads(message)
            assert payload["set_goal"]["goal_object"] == "car"
            # A socket write is not acceptance, so the visible target is unchanged here.
            assert adapter.state.diagnostics.last_gimbal_command is previous
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": "path1",
                    "goal_id": 3,
                    "goal_object": "car",
                    "goal_check": "false",
                }
            }))

    adapter.connect = fake_connect
    adapter._socket = RejectingSocket()

    result = await adapter.set_goal(
        RobotGoalCommand(path_name="path1", goal_id=3, goal_object="car")
    )

    assert result["goal_check"] == "false"
    assert adapter.state.diagnostics.last_gimbal_command is previous
    assert adapter._pending_goal_attempt is None


@pytest.mark.asyncio
async def test_navigation_only_goal_preserves_numeric_gimbal_target():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    previous = GimbalCommandDiagnostic(
        context="cruise_moving",
        payload={"gimbal_control": {"yaw_end": 12, "pitch_end": -4}},
    )
    adapter.state.diagnostics.last_gimbal_command = previous

    async def fake_connect():
        return adapter.state

    class AcceptingSocket:
        async def send(self, message):
            payload = json.loads(message)
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    **payload["set_goal"],
                    "goal_check": "true",
                }
            }))

    adapter.connect = fake_connect
    adapter._socket = AcceptingSocket()

    await adapter.set_goal(RobotGoalCommand(path_name="path2", goal_id=8))

    assert adapter.state.diagnostics.last_gimbal_command is previous
    goal = adapter.state.diagnostics.last_goal_command
    assert goal is not None
    assert goal.context == "cruise_navigation_goal"
    assert goal.payload["set_goal"]["goal_id"] == 8


@pytest.mark.asyncio
async def test_navigation_only_goal_tolerates_retained_object_identity_in_reply_and_heartbeat():
    adapter = _goal_attempt_adapter()

    class RetainedObjectSocket:
        async def send(self, message):
            payload = json.loads(message)
            assert payload["set_goal"] == {
                "path_name": "path-a",
                "goal_id": 1,
                "goal_object": None,
            }
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": "path-a",
                    "goal_id": 1,
                    "goal_object": "car",
                    "goal_check": "true",
                }
            }))

    adapter._socket = RetainedObjectSocket()
    response = await adapter.set_goal(RobotGoalCommand(path_name="path-a", goal_id=1))

    assert response["goal_check"] == "true"
    await adapter._handle_message(json.dumps({
        "task": {
            "path_file": "path-a",
            "goal_id": 1,
            "goal_object": "car",
            "goal_status": "done",
            "object_status": "failed",
        }
    }))
    assert await adapter.wait_for_arrival(timeout_s=0.05) == "done"


@pytest.mark.asyncio
async def test_concurrent_goal_calls_serialize_arming_through_acknowledgement():
    adapter = _goal_attempt_adapter()
    a_written = asyncio.Event()
    b_written = asyncio.Event()
    sent_goal_ids = []

    class ControlledSocket:
        async def send(self, message):
            goal_id = json.loads(message)["set_goal"]["goal_id"]
            sent_goal_ids.append(goal_id)
            (a_written if goal_id == 1 else b_written).set()

    adapter._socket = ControlledSocket()
    goal_a = RobotGoalCommand(path_name="path-a", goal_id=1)
    goal_b = RobotGoalCommand(path_name="path-b", goal_id=2)
    request_a = asyncio.create_task(adapter.set_goal(goal_a))
    await a_written.wait()
    request_b = asyncio.create_task(adapter.set_goal(goal_b))
    await asyncio.sleep(0)

    sent_before_a_ack = list(sent_goal_ids)
    pending_before_a_ack = adapter._pending_goal
    await adapter._handle_message(json.dumps({"task": {"goal_status": "going"}}))
    await adapter._handle_message(json.dumps({"task": {"goal_status": "done"}}))
    await adapter._handle_message(json.dumps({
        "robot_goal": {
            "path_file": "path-a",
            "goal_id": 1,
            "goal_object": None,
            "goal_check": "true",
        }
    }))
    response_a = await request_a
    await asyncio.wait_for(b_written.wait(), timeout=0.05)
    pending_after_b_write = adapter._pending_goal
    arrival_after_b_write = adapter._arrival_event.is_set()
    await adapter._handle_message(json.dumps({
        "robot_goal": {
            "path_file": "path-b",
            "goal_id": 2,
            "goal_object": None,
            "goal_check": "true",
        }
    }))
    response_b = await request_b

    assert sent_before_a_ack == [1]
    assert pending_before_a_ack == goal_a
    assert response_a["goal_check"] == "true"
    assert pending_after_b_write == goal_b
    assert arrival_after_b_write is False
    assert response_b["goal_check"] == "true"


@pytest.mark.parametrize("navigation_key", ["navigation", "naviagtion"])
@pytest.mark.asyncio
async def test_navigation_only_goal_status_can_finish_current_arrival(navigation_key):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    goal = RobotGoalCommand(path_name="path-a", goal_id=1)
    adapter._arm_goal_tracking(goal)
    adapter._capture_goal_written = True

    identity = {"path_file": "path-a", "goal_id": 1}
    adapter._apply_protocol_state({
        navigation_key: {"goal_status": "going"},
        "task": identity,
    })
    adapter._apply_protocol_state({
        navigation_key: {"goal_status": "done"},
        "task": identity,
    })

    assert adapter._arrival_event.is_set() is True
    assert await adapter.wait_for_arrival(timeout_s=0.05) == "done"


@pytest.mark.asyncio
async def test_delayed_mismatched_goal_ack_cannot_accept_the_current_command():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_connect():
        return adapter.state

    class DelayedPreviousGoalSocket:
        async def send(self, message):
            payload = json.loads(message)
            assert payload["set_goal"] == {
                "path_name": "path-b", "goal_id": 2, "goal_object": None,
            }
            # This is a late acknowledgement for A, delivered while B owns the generic
            # ``robot_goal`` response slot.
            await adapter._handle_message(json.dumps({
                "robot_goal": {
                    "path_file": "path-a",
                    "goal_id": 1,
                    "goal_object": None,
                    "goal_check": "true",
                }
            }))

    adapter.connect = fake_connect
    adapter._socket = DelayedPreviousGoalSocket()

    with pytest.raises(ValueError):
        await adapter.set_goal(RobotGoalCommand(path_name="path-b", goal_id=2))

    assert adapter.state.diagnostics.last_goal_command is None
    assert adapter._pending_goal_attempt is None
    assert adapter._pending_goal is None


@pytest.mark.asyncio
async def test_done_heartbeat_for_previous_identity_cannot_finish_pending_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    current = RobotGoalCommand(path_name="path-b", goal_id=2)
    adapter._arm_goal_tracking(current)
    adapter._capture_goal_written = True

    # B has genuinely started, so the ordinary stale-initial-done guard is already cleared.
    adapter._apply_protocol_state({
        "task": {"path_file": "path-b", "goal_id": 2, "goal_status": "going"}
    })
    adapter._apply_protocol_state({
        "task": {"path_file": "path-a", "goal_id": 1, "goal_status": "done"}
    })

    assert adapter._pending_goal == current
    assert adapter._arrival_event.is_set() is False

    adapter._apply_protocol_state({
        "task": {"path_file": "path-b", "goal_id": 2, "goal_status": "done"}
    })
    assert await adapter.wait_for_arrival(timeout_s=0.05) == "done"


@pytest.mark.asyncio
async def test_start_recording_reconciles_a_late_reply_without_resending(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.2)
    adapter = _goal_attempt_adapter()
    sent: list[dict] = []
    reply_tasks: list[asyncio.Task] = []

    class DelayedReplySocket:
        async def send(self, message):
            sent.append(json.loads(message))

            async def reply_after_primary_deadline():
                await asyncio.sleep(0.03)
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "start": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_LATE.mp4",
                    }
                }))

            reply_tasks.append(asyncio.create_task(reply_after_primary_deadline()))

    adapter._socket = DelayedReplySocket()
    state = await adapter.start_recording()
    await asyncio.gather(*reply_tasks)

    assert sent == [{"video_record": {"start": 0, "resolution": 4}}]
    assert state.recording is True
    assert state.media_url == "http://camera.local/REC_LATE.mp4"
    assert adapter._pending == {}
    assert adapter._pending_reply_matchers == {}


@pytest.mark.asyncio
async def test_recording_reply_times_out_only_after_grace_and_sends_once(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    sent: list[dict] = []

    class SilentSocket:
        async def send(self, message):
            sent.append(json.loads(message))

    adapter._socket = SilentSocket()

    with pytest.raises(TimeoutError, match="等待机器人回复超时"):
        await adapter.start_recording()

    assert sent == [{"video_record": {"start": 0, "resolution": 4}}]
    assert adapter._pending == {}
    assert adapter._pending_reply_matchers == {}


@pytest.mark.asyncio
async def test_unresolved_start_blocks_retry_and_endpoint_change_until_late_reply(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    sent: list[dict] = []

    class RecoverySocket:
        async def send(self, message):
            payload = json.loads(message)
            sent.append(payload)
            if payload.get("video_record", {}).get("stop") == 0:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_FIRST.mp4",
                    }
                }))

    adapter._socket = RecoverySocket()
    with pytest.raises(TimeoutError, match="等待机器人回复超时"):
        await adapter.start_recording()

    recovery = adapter._recording_reply_recovery
    assert recovery is not None
    with pytest.raises(ValueError, match="上一条录制指令仍在确认中"):
        await adapter.start_recording()
    with pytest.raises(ValueError, match="暂时不能更改机器人连接"):
        await adapter.configure_websocket_url("ws://replacement.local:8765")
    with pytest.raises(
        ValueError,
        match="机器人仍在录制|暂时不能断开机器人连接",
    ):
        await adapter.disconnect()
    assert adapter._recording_reply_recovery == recovery
    assert adapter.websocket_url == "ws://robot.local:8765"

    # An opposite-state heartbeat is only a snapshot: Start may still be queued in firmware.
    # It must not release ownership or permit a same-action retry whose ACK is indistinguishable.
    adapter._apply_protocol_state({"gimbal": {"record_status": "idle"}})
    assert adapter._recording_reply_recovery == recovery
    assert adapter.recording_status_known() is False
    with pytest.raises(ValueError, match="上一条录制指令仍在确认中"):
        await adapter.start_recording()

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_FIRST.mp4",
        }
    }))
    compensation = adapter._recording_compensation_task
    assert compensation is not None
    await compensation

    assert [next(iter(item["video_record"])) for item in sent] == ["start", "stop"]
    assert adapter.state.recording is False


@pytest.mark.asyncio
async def test_finalize_supersedes_start_without_ack_with_exactly_one_stop(monkeypatch):
    """A written Start is unsafe even when neither its ACK nor a heartbeat arrives."""

    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    events = EventHub()
    adapter = _goal_attempt_adapter(events)
    sent: list[dict] = []

    class StartSilentStopReplySocket:
        async def send(self, message):
            payload = json.loads(message)
            sent.append(payload)
            if payload.get("video_record", {}).get("stop") == 0:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_FINAL.mp4",
                    }
                }))

    adapter._socket = StartSilentStopReplySocket()
    robot = RobotService(events, adapter=adapter)

    with pytest.raises(TimeoutError, match="等待机器人回复超时"):
        await adapter.start_recording()

    assert adapter.recording_stop_required() is True
    assert adapter.state.recording is False
    finalized = await robot.finalize_capture_recording()

    actions = [next(iter(item["video_record"])) for item in sent]
    assert actions == ["start", "stop"]
    assert finalized.recording is False
    assert adapter.recording_stop_required() is False
    assert adapter._recording_reply_recovery is None


@pytest.mark.asyncio
async def test_ambiguous_stop_can_be_retried_without_permanent_recovery_lock(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True
    adapter._recording_stop_required = True
    stop_writes = 0

    class SecondStopRepliesSocket:
        async def send(self, message):
            nonlocal stop_writes
            payload = json.loads(message)
            assert payload == {"video_record": {"stop": 0}}
            stop_writes += 1
            if stop_writes == 2:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_RETRY.mp4",
                    }
                }))

    adapter._socket = SecondStopRepliesSocket()

    with pytest.raises(TimeoutError, match="等待机器人回复超时"):
        await adapter.stop_recording()
    assert adapter._recording_reply_recovery is not None
    assert adapter.recording_stop_required() is True

    stopped = await adapter.stop_recording()

    assert stop_writes == 2
    assert stopped.recording is False
    assert adapter._recording_reply_recovery is None
    assert adapter.recording_stop_required() is False


@pytest.mark.asyncio
async def test_written_start_latch_blocks_transport_reconfiguration_while_state_is_unknown():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = False
    adapter._recording_status_known = False
    adapter._recording_stop_required = True

    with pytest.raises(ValueError, match="录制进行中"):
        await adapter.configure_websocket_url("ws://replacement.local:8765")
    with pytest.raises(ValueError, match="仍在录制"):
        await adapter.disconnect()

    assert adapter.websocket_url == "ws://robot.local:8765"


@pytest.mark.asyncio
async def test_disconnect_is_refused_while_start_has_reached_the_socket():
    adapter = _goal_attempt_adapter()
    start_sent = asyncio.Event()

    class PendingSocket:
        async def send(self, _message):
            start_sent.set()

    adapter._socket = PendingSocket()
    starting = asyncio.create_task(adapter.start_recording())
    await start_sent.wait()

    with pytest.raises(ValueError, match="暂时不能断开机器人连接"):
        await adapter.disconnect()

    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert adapter._recording_reply_recovery is not None


def test_opposite_heartbeat_does_not_release_an_ambiguous_stop():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_command_revision = 7
    adapter._recording_reply_recovery = (7, "stop")

    adapter._apply_protocol_state({"gimbal": {"record_status": "recording"}})
    assert adapter._recording_reply_recovery == (7, "stop")
    assert adapter.recording_status_known() is False

    adapter._apply_protocol_state({"gimbal": {"record_status": "idle"}})
    assert adapter._recording_reply_recovery is None
    assert adapter.recording_status_known() is True


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_start", [False, True])
async def test_lifecycle_shutdown_orders_a_final_stop_before_transport_close(pending_start):
    adapter = _goal_attempt_adapter()
    adapter.state.recording = not pending_start
    adapter._recording_media_url = "http://camera.local/REC_SHUTDOWN.mp4"
    if pending_start:
        adapter._recording_command_revision = 3
        adapter._recording_reply_recovery = (3, "start")
    events = []

    class ShutdownSocket:
        async def send(self, message):
            payload = json.loads(message)
            events.append(("send", payload))
            await adapter._handle_message(json.dumps({
                "robot_video_record": {
                    "stop": 0,
                    "status": "ok",
                    "url": "http://camera.local/REC_SHUTDOWN.mp4",
                }
            }))

        async def close(self):
            events.append(("close", None))

    adapter._socket = ShutdownSocket()

    state = await adapter.shutdown()

    assert events == [
        ("send", {"video_record": {"stop": 0}}),
        ("close", None),
    ]
    assert state.connected is False
    assert state.recording is False
    assert state.media_url == "http://camera.local/REC_SHUTDOWN.mp4"
    assert adapter._recording_reply_recovery is None


@pytest.mark.asyncio
async def test_lifecycle_shutdown_closes_transport_when_stop_cannot_be_confirmed(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    closed = False

    class SilentSocket:
        async def send(self, _message):
            return None

        async def close(self):
            nonlocal closed
            closed = True

    adapter._socket = SilentSocket()

    state = await adapter.shutdown()

    assert closed is True
    assert state.connected is False
    assert state.recording is True
    assert "未能确认停止录制" in str(state.error)
    assert adapter._recording_reply_recovery is None


@pytest.mark.asyncio
async def test_lifecycle_shutdown_deadline_closes_a_socket_with_a_stalled_stop_write(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_SHUTDOWN_TIMEOUT_SECONDS", 0.05)
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    never_sent = asyncio.Event()
    closed = False

    class StalledSocket:
        async def send(self, _message):
            await never_sent.wait()

        async def close(self):
            nonlocal closed
            closed = True

    adapter._socket = StalledSocket()

    state = await adapter.shutdown()

    assert closed is True
    assert state.connected is False
    assert state.recording is True
    assert "安全停止超过" in str(state.error)
    assert adapter.shutdown_recording_confirmed_idle() is False


@pytest.mark.asyncio
async def test_recovered_capture_shutdown_requests_idle_before_the_first_heartbeat():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = False
    adapter._recording_status_known = False
    sent = []

    class RecoverySocket:
        async def send(self, message):
            sent.append(json.loads(message))
            await adapter._handle_message(json.dumps({
                "robot_video_record": {
                    "stop": 0,
                    "status": "ok",
                    "url": "http://camera.local/REC_RECOVERED_EXIT.mp4",
                }
            }))

        async def close(self):
            return None

    adapter._socket = RecoverySocket()

    state = await adapter.shutdown(require_idle=True)

    assert sent == [{"video_record": {"stop": 0}}]
    assert state.recording is False
    assert adapter.shutdown_recording_confirmed_idle() is True


@pytest.mark.asyncio
async def test_service_shutdown_persists_the_stop_url_before_process_exit():
    class LifecycleAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=True)
            self.shutdown_calls = 0

        async def status(self):
            return self.state

        def recording_operation_pending(self):
            return False

        async def shutdown(self):
            self.shutdown_calls += 1
            self.state.connected = False
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_EXIT.mp4"
            return self.state

    adapter = LifecycleAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=None)
    persisted = []

    state = await robot.shutdown(on_media_url=persisted.append)

    assert adapter.shutdown_calls == 1
    assert persisted == ["http://camera.local/REC_EXIT.mp4"]
    assert state.recording is False
    assert state.connected is False
    assert state.media_url == "http://camera.local/REC_EXIT.mp4"
    assert state.media_sync_error == "媒体服务不可用，无法保存机器人文件"


@pytest.mark.asyncio
async def test_service_shutdown_never_downloads_media_until_stop_is_confirmed():
    class UnconfirmedAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://camera.local/REC_PARTIAL.mp4",
                error="关闭前未能确认停止录制",
            )

        async def status(self):
            return self.state

        def recording_operation_pending(self):
            return False

        async def shutdown(self):
            self.state.connected = False
            return self.state

        def shutdown_recording_confirmed_idle(self):
            return False

    class MustNotDownload:
        async def download_url(self, *_args, **_kwargs):
            raise AssertionError("an in-progress recording must not be downloaded")

    adapter = UnconfirmedAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=MustNotDownload())
    persisted = []

    state = await robot.shutdown(on_media_url=persisted.append)

    assert persisted == ["http://camera.local/REC_PARTIAL.mp4"]
    assert state.media_local_path is None
    assert state.recording is True
    assert state.media_sync_error == "关闭前未能确认停止录制"
    assert robot.shutdown_recording_confirmed_idle() is False


@pytest.mark.asyncio
async def test_service_shutdown_without_sync_never_reuses_a_photo_path_or_downloads(tmp_path):
    photo = tmp_path / "PHOTO.jpg"
    photo.write_bytes(b"photo")

    class LifecycleAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://camera.local/PHOTO.jpg",
                media_local_path=str(photo),
            )

        async def status(self):
            return self.state

        def recording_operation_pending(self):
            return False

        async def shutdown(self):
            self.state.connected = False
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_EXIT.mp4"
            return self.state

        def shutdown_recording_confirmed_idle(self):
            return True

    class MustNotDownload:
        async def download_url(self, *_args, **_kwargs):
            raise AssertionError("application exit must defer the video transfer")

    persisted = []
    robot = RobotService(
        EventHub(),
        adapter=LifecycleAdapter(),
        media=MustNotDownload(),
    )

    state = await robot.shutdown(
        on_media_url=persisted.append,
        sync_media=False,
    )

    assert persisted == ["http://camera.local/REC_EXIT.mp4"]
    assert state.media_url == "http://camera.local/REC_EXIT.mp4"
    assert state.media_local_path is None
    assert state.media_sync_error is None
    assert robot.shutdown_recording_confirmed_idle() is True


@pytest.mark.asyncio
async def test_service_shutdown_reaches_adapter_when_capture_owner_is_stalled(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_SHUTDOWN_TIMEOUT_SECONDS", 0.05)

    class LifecycleAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=False)
            self.shutdown_calls = 0

        async def status(self):
            return self.state

        def recording_operation_pending(self):
            return False

        async def shutdown(self):
            self.shutdown_calls += 1
            self.state.connected = False
            return self.state

    adapter = LifecycleAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    await robot._capture_lock.acquire()
    try:
        state = await robot.shutdown()
    finally:
        robot._capture_lock.release()

    assert adapter.shutdown_calls == 1
    assert state.connected is False


@pytest.mark.asyncio
async def test_cancelled_start_before_socket_write_does_not_arm_orphan_cleanup():
    adapter = _goal_attempt_adapter()
    await adapter._request_lock.acquire()
    try:
        starting = asyncio.create_task(adapter.start_recording())
        await asyncio.sleep(0)
        starting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await starting
    finally:
        adapter._request_lock.release()

    assert adapter._recording_reply_recovery is None
    assert adapter._recording_write_attempt_revision is None
    adapter._apply_protocol_state({"gimbal": {"record_status": "recording"}})
    await asyncio.sleep(0)
    assert adapter._recording_compensation_task is None


@pytest.mark.asyncio
async def test_delayed_start_reply_cannot_complete_a_stop_request():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter.state.media_url = "http://camera.local/REC_CURRENT.mp4"
    adapter._recording_media_url = adapter.state.media_url
    command_sent = asyncio.Event()
    sent: list[dict] = []

    class ControlledSocket:
        async def send(self, message):
            sent.append(json.loads(message))
            command_sent.set()

    adapter._socket = ControlledSocket()
    stopping = asyncio.create_task(adapter.stop_recording())
    await command_sent.wait()

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_OLD_START.mp4",
        }
    }))
    await asyncio.sleep(0)
    assert stopping.done() is False
    assert adapter.state.recording is True
    assert adapter.state.media_url == "http://camera.local/REC_CURRENT.mp4"

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
        }
    }))
    state = await stopping

    assert sent == [{"video_record": {"stop": 0}}]
    assert state.recording is False
    assert state.media_url == "http://camera.local/REC_CURRENT.mp4"


@pytest.mark.asyncio
async def test_delayed_stop_reply_neither_completes_nor_mutates_a_start_request():
    adapter = _goal_attempt_adapter()
    command_sent = asyncio.Event()

    class ControlledSocket:
        async def send(self, _message):
            command_sent.set()

    adapter._socket = ControlledSocket()
    starting = asyncio.create_task(adapter.start_recording())
    await command_sent.wait()

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
            "url": "http://camera.local/REC_OLD_STOP.mp4",
        }
    }))
    await asyncio.sleep(0)
    assert starting.done() is False
    assert adapter.state.recording is False
    assert adapter.state.media_url is None

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_NEW.mp4",
        }
    }))
    state = await starting
    assert state.recording is True
    assert state.media_url == "http://camera.local/REC_NEW.mp4"


@pytest.mark.asyncio
async def test_transport_timeout_is_not_relabelled_as_a_reply_deadline(monkeypatch):
    adapter = _goal_attempt_adapter()
    logged = []

    class FailingSocket:
        async def send(self, _message):
            adapter._fail_pending(TimeoutError("transport read timed out"))

    monkeypatch.setattr(
        robot_module,
        "log_event",
        lambda _level, event, **_kwargs: logged.append(event),
    )
    adapter._socket = FailingSocket()

    with pytest.raises(TimeoutError, match="transport read timed out"):
        await adapter.start_recording()

    assert "robot.reply.delayed" not in logged
    assert "robot.reply.timeout" not in logged


@pytest.mark.asyncio
async def test_start_reply_after_final_timeout_is_stopped_automatically(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    sent = []

    class RecoverySocket:
        async def send(self, message):
            payload = json.loads(message)
            sent.append(payload)
            if payload.get("video_record", {}).get("stop") == 0:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_ORPHAN.mp4",
                    }
                }))

    adapter._socket = RecoverySocket()
    with pytest.raises(TimeoutError, match="等待机器人回复超时"):
        await adapter.start_recording()

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_ORPHAN.mp4",
        }
    }))
    compensation = adapter._recording_compensation_task
    assert compensation is not None
    await compensation

    assert sent == [
        {"video_record": {"start": 0, "resolution": 4}},
        {"video_record": {"stop": 0}},
    ]
    assert adapter.state.recording is False
    assert adapter.state.media_url == "http://camera.local/REC_ORPHAN.mp4"


@pytest.mark.asyncio
async def test_heartbeat_confirmed_orphan_start_is_stopped_without_waiting_for_its_ack(
    monkeypatch,
):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    sent = []

    class RecoverySocket:
        async def send(self, message):
            payload = json.loads(message)
            sent.append(payload)
            if payload.get("video_record", {}).get("stop") == 0:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_HEARTBEAT.mp4",
                    }
                }))

    adapter._socket = RecoverySocket()
    with pytest.raises(TimeoutError):
        await adapter.start_recording()

    adapter._apply_protocol_state({"gimbal": {"record_status": "recording"}})
    compensation = adapter._recording_compensation_task
    assert compensation is not None
    await compensation

    assert [next(iter(item["video_record"])) for item in sent] == ["start", "stop"]
    assert adapter.state.recording is False
    assert adapter.state.media_url == "http://camera.local/REC_HEARTBEAT.mp4"


@pytest.mark.asyncio
async def test_confirmed_orphan_stop_is_idempotent_for_a_waiting_finalizer():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = False
    adapter.state.media_url = "http://camera.local/REC_RECOVERED.mp4"
    adapter.state.last_command = "video_record:stop"
    adapter._recording_status_known = True

    class MustNotSendSocket:
        async def send(self, _message):
            raise AssertionError("duplicate Stop must not be sent")

    adapter._socket = MustNotSendSocket()
    state = await adapter.stop_recording()

    assert state.recording is False
    assert state.media_url == "http://camera.local/REC_RECOVERED.mp4"


@pytest.mark.asyncio
async def test_cancel_after_matched_start_reply_stops_the_committed_orphan_once(
    monkeypatch,
):
    adapter = _goal_attempt_adapter()
    original_wait = robot_module._wait_for_reply
    reply_committed = asyncio.Event()
    hold_start_waiter = asyncio.Event()
    sent = []

    async def pause_start_waiter(future, timeout_s):
        response = await original_wait(future, timeout_s)
        if robot_module._video_record_action(response) == "start":
            reply_committed.set()
            await hold_start_waiter.wait()
        return response

    class ReplyingSocket:
        async def send(self, message):
            payload = json.loads(message)
            sent.append(payload)
            if payload.get("video_record", {}).get("stop") == 0:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_ATOMIC.mp4",
                    }
                }))

    monkeypatch.setattr(robot_module, "_wait_for_reply", pause_start_waiter)
    adapter._socket = ReplyingSocket()
    starting = asyncio.create_task(adapter.start_recording())
    await asyncio.sleep(0)
    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_ATOMIC.mp4",
        }
    }))
    await reply_committed.wait()
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0}
    }))
    assert adapter.state.recording is True
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    compensation = adapter._recording_compensation_task
    assert compensation is not None
    await compensation

    assert [next(iter(item["video_record"])) for item in sent] == ["start", "stop"]
    assert adapter._recording_reply_recovery is None
    assert adapter.state.recording is False
    assert adapter.state.media_url == "http://camera.local/REC_ATOMIC.mp4"


@pytest.mark.asyncio
async def test_cancel_after_matched_stop_reply_retains_idle_state_and_url(
    monkeypatch,
):
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True
    original_wait = robot_module._wait_for_reply
    reply_committed = asyncio.Event()
    hold_stop_waiter = asyncio.Event()
    sent = []

    async def pause_stop_waiter(future, timeout_s):
        response = await original_wait(future, timeout_s)
        if robot_module._video_record_action(response) == "stop":
            reply_committed.set()
            await hold_stop_waiter.wait()
        return response

    class ReplyingSocket:
        async def send(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(robot_module, "_wait_for_reply", pause_stop_waiter)
    adapter._socket = ReplyingSocket()
    stopping = asyncio.create_task(adapter.stop_recording())
    await asyncio.sleep(0)
    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
            "url": "http://camera.local/REC_STOP_ATOMIC.mp4",
        }
    }))
    await reply_committed.wait()
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "recording", "yaw": 0, "pitch": 0}
    }))
    assert adapter.state.recording is False
    stopping.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert adapter._recording_reply_recovery is None
    assert adapter.state.recording is False
    assert adapter.recording_status_known() is False
    assert adapter.state.media_url == "http://camera.local/REC_STOP_ATOMIC.mp4"

    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0}
    }))
    assert adapter.recording_status_known() is True

    state = await adapter.stop_recording()
    assert state.media_url == "http://camera.local/REC_STOP_ATOMIC.mp4"
    assert len(sent) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "transitional_status", "expected_recording"),
    [
        ("start", "idle", True),
        ("stop", "recording", False),
    ],
)
async def test_recording_reply_wins_over_bundled_transitional_heartbeat(
    action,
    transitional_status,
    expected_recording,
):
    adapter = _goal_attempt_adapter()
    adapter.state.recording = action == "stop"
    adapter._recording_status_known = True

    class SilentSocket:
        async def send(self, _message):
            return None

    adapter._socket = SilentSocket()
    operation = asyncio.create_task(
        adapter.start_recording() if action == "start" else adapter.stop_recording()
    )
    await asyncio.sleep(0)
    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            action: 0,
            "status": "ok",
            "url": "http://camera.local/REC_BUNDLED.mp4",
        },
        "gimbal": {
            "record_status": transitional_status,
            "yaw": 0,
            "pitch": 0,
        },
    }))
    state = await operation

    assert state.recording is expected_recording
    assert state.media_url == "http://camera.local/REC_BUNDLED.mp4"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "stale_status", "expected_status", "expected_recording"),
    [
        ("start", "idle", "recording", True),
        ("stop", "recording", "idle", False),
    ],
)
async def test_recording_reply_guard_survives_caller_return_until_matching_heartbeat(
    action,
    stale_status,
    expected_status,
    expected_recording,
):
    adapter = _goal_attempt_adapter()
    adapter.state.recording = action == "stop"
    adapter._recording_status_known = True

    class ReplyingSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_video_record": {
                    action: 0,
                    "status": "ok",
                    "url": "http://camera.local/REC_GUARDED.mp4",
                }
            }))

    adapter._socket = ReplyingSocket()
    state = (
        await adapter.start_recording()
        if action == "start"
        else await adapter.stop_recording()
    )
    assert state.recording is expected_recording

    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": stale_status, "yaw": 0, "pitch": 0}
    }))
    assert adapter.state.recording is expected_recording
    assert adapter._recording_heartbeat_guard is not None
    assert adapter._recording_heartbeat_conflict_count == 1
    assert adapter.recording_status_known() is False
    assert adapter.recording_operation_pending() is True

    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": expected_status, "yaw": 0, "pitch": 0}
    }))
    assert adapter.state.recording is expected_recording
    assert adapter._recording_heartbeat_guard is None
    assert adapter._recording_heartbeat_conflict_count == 0
    assert adapter.recording_status_known() is True
    assert adapter.recording_operation_pending() is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "action",
        "contradictory_status",
        "ack_recording",
        "resolved_recording",
        "resolved_known",
    ),
    [
        ("start", "idle", True, True, False),
        ("stop", "recording", False, True, True),
    ],
)
async def test_repeated_contradictory_recording_heartbeat_overrides_ack_guard(
    action,
    contradictory_status,
    ack_recording,
    resolved_recording,
    resolved_known,
):
    adapter = _goal_attempt_adapter()
    adapter.state.recording = action == "stop"
    adapter._recording_status_known = True

    class ReplyingSocket:
        async def send(self, message):
            requested_action = next(iter(json.loads(message)["video_record"]))
            await adapter._handle_message(json.dumps({
                "robot_video_record": {
                    requested_action: 0,
                    "status": "ok",
                    "url": "http://camera.local/REC_CONFLICT.mp4",
                }
            }))

    adapter._socket = ReplyingSocket()
    if action == "start":
        await adapter.start_recording()
    else:
        await adapter.stop_recording()
    assert adapter.state.recording is ack_recording

    conflicting_heartbeat = json.dumps({
        "gimbal": {"record_status": contradictory_status, "yaw": 0, "pitch": 0}
    })
    await adapter._handle_message(conflicting_heartbeat)
    assert adapter.state.recording is ack_recording
    assert adapter.recording_status_known() is False
    assert adapter.recording_operation_pending() is True
    with pytest.raises(ValueError, match="正在等待机器人同步录制状态"):
        await adapter.start_recording()
    with pytest.raises(
        ValueError,
        match="机器人仍在录制|暂时不能断开机器人连接",
    ):
        await adapter.disconnect()

    await adapter._handle_message(conflicting_heartbeat)
    assert adapter.state.recording is resolved_recording
    assert adapter._recording_heartbeat_guard is None
    assert adapter._recording_heartbeat_conflict_count == 0
    assert adapter.recording_status_known() is resolved_known
    assert adapter.recording_operation_pending() is False

    if action == "start":
        await adapter._handle_message(conflicting_heartbeat)
        assert adapter.state.recording is True
        assert adapter.recording_status_known() is False
        stopped = await adapter.stop_recording()
        assert stopped.recording is False
        assert adapter.recording_status_known() is True
        assert adapter._recording_stop_required is False

    if resolved_recording and action != "start":
        with pytest.raises(ValueError, match="机器人仍在录制"):
            await adapter.disconnect()


@pytest.mark.asyncio
async def test_lone_idle_conflict_after_start_expires_to_safe_stop_required_state():
    adapter = _goal_attempt_adapter()
    adapter._recording_status_known = True

    class ReplyingSocket:
        async def send(self, message):
            requested_action = next(iter(json.loads(message)["video_record"]))
            await adapter._handle_message(json.dumps({
                "robot_video_record": {
                    requested_action: 0,
                    "status": "ok",
                    "url": "http://camera.local/REC_START_CONFLICT.mp4",
                }
            }))

    adapter._socket = ReplyingSocket()
    await adapter.start_recording()
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0}
    }))

    assert adapter.recording_operation_pending() is True
    adapter._recording_heartbeat_conflict_deadline = time.monotonic() - 1

    assert adapter.recording_operation_pending() is False
    assert adapter.state.recording is True
    assert adapter.recording_status_known() is False
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0}
    }))
    assert adapter.state.recording is True
    assert adapter.recording_status_known() is False
    with pytest.raises(ValueError, match="机器人仍在录制"):
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_shutdown_reissues_stop_during_transitional_heartbeat_conflict():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = False
    adapter._recording_status_known = True
    sent_actions = []

    class ReplyingSocket:
        async def send(self, message):
            action = next(iter(json.loads(message)["video_record"]))
            sent_actions.append(action)
            await adapter._handle_message(json.dumps({
                "robot_video_record": {
                    action: 0,
                    "status": "ok",
                    "url": "http://camera.local/REC_SHUTDOWN_CONFLICT.mp4",
                }
            }))

        async def close(self):
            return None

    adapter._socket = ReplyingSocket()
    await adapter.stop_recording()
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "recording", "yaw": 0, "pitch": 0}
    }))

    assert adapter.state.recording is False
    assert adapter.recording_status_known() is False
    assert adapter.recording_operation_pending() is True

    await adapter.shutdown(require_idle=False)

    assert sent_actions == ["stop", "stop"]
    assert adapter.shutdown_recording_confirmed_idle() is True
    assert adapter.state.recording is False
    assert adapter.state.connected is False


@pytest.mark.asyncio
async def test_successful_stop_with_empty_url_accepts_later_url_reply():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True
    empty_reply_sent = asyncio.Event()

    class EmptyUrlStopSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_video_record": {"stop": 0, "status": "ok", "url": ""}
            }))
            empty_reply_sent.set()

    adapter._socket = EmptyUrlStopSocket()
    stopping = asyncio.create_task(adapter.stop_recording())
    await empty_reply_sent.wait()
    await asyncio.sleep(0)

    assert stopping.done() is False
    assert adapter.state.recording is False
    assert adapter.state.media_url is None
    assert adapter._recording_reply_recovery is None
    assert adapter._late_stop_metadata_owner is not None

    await adapter._handle_message(json.dumps({
        "robot_video_record": {"stop": 0, "status": "ok", "url": ""}
    }))
    await asyncio.sleep(0)
    assert stopping.done() is False
    assert adapter._late_stop_metadata_owner is not None

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
            "url": "http://camera.local/REC_FINALIZED.mp4",
        }
    }))
    state = await stopping

    assert adapter._late_stop_metadata_owner is None
    assert state.media_url == "http://camera.local/REC_FINALIZED.mp4"


@pytest.mark.asyncio
async def test_empty_stop_url_fails_after_bounded_metadata_grace(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_STOP_METADATA_GRACE_SECONDS", 0.01)
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True

    class EmptyUrlStopSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_video_record": {"stop": 0, "status": "ok", "url": ""}
            }))

    adapter._socket = EmptyUrlStopSocket()
    with pytest.raises(ValueError, match="没有返回视频地址"):
        await adapter.stop_recording()

    assert adapter.state.recording is False
    assert adapter.recording_operation_pending() is False


@pytest.mark.asyncio
async def test_disconnect_and_shutdown_wait_for_late_stop_metadata():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True
    empty_reply_sent = asyncio.Event()

    class EmptyThenLateSocket:
        async def send(self, _message):
            await adapter._handle_message(json.dumps({
                "robot_video_record": {"stop": 0, "status": "ok", "url": ""}
            }))
            empty_reply_sent.set()

        async def close(self):
            return None

    adapter._socket = EmptyThenLateSocket()
    stopping = asyncio.create_task(adapter.stop_recording())
    await empty_reply_sent.wait()
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="暂时不能断开机器人连接"):
        await adapter.disconnect()
    shutting_down = asyncio.create_task(adapter.shutdown())
    await asyncio.sleep(0)
    assert shutting_down.done() is False

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
            "url": "http://camera.local/REC_LIFECYCLE.mp4",
        }
    }))
    stopped_state, shutdown_state = await asyncio.gather(stopping, shutting_down)

    assert stopped_state.media_url == "http://camera.local/REC_LIFECYCLE.mp4"
    assert shutdown_state.connected is False
    assert adapter.shutdown_recording_confirmed_idle() is True


@pytest.mark.asyncio
async def test_recording_recovery_reply_from_replacement_connection_is_quarantined():
    adapter = _goal_attempt_adapter()
    adapter._connection_epoch = 5
    adapter._recording_command_revision = 8
    adapter._recording_write_attempt_revision = 8
    adapter._recording_write_attempt_epoch = 4
    adapter._recording_reply_recovery = (8, "start")

    await adapter._handle_message(
        json.dumps({
            "robot_video_record": {
                "start": 0,
                "status": "ok",
                "url": "http://camera.local/REC_STALE.mp4",
            }
        }),
        connection_epoch=5,
    )

    assert adapter._recording_reply_recovery == (8, "start")
    assert adapter.state.recording is False
    assert adapter.state.media_url is None
    assert adapter._recording_compensation_task is None


@pytest.mark.asyncio
async def test_idle_heartbeat_keeps_late_stop_url_ownership(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.005)
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True

    class SilentSocket:
        async def send(self, _message):
            return None

    adapter._socket = SilentSocket()
    with pytest.raises(TimeoutError):
        await adapter.stop_recording()

    assert adapter._recording_reply_recovery is not None
    assert adapter._late_stop_metadata_owner is not None
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0}
    }))
    assert adapter._recording_reply_recovery is None
    assert adapter._late_stop_metadata_owner is not None
    assert adapter.recording_operation_pending() is False

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
            "url": "http://camera.local/REC_LATE_URL.mp4",
        }
    }))

    assert adapter._late_stop_metadata_owner is None
    assert adapter.state.recording is False
    assert adapter.state.media_url == "http://camera.local/REC_LATE_URL.mp4"


@pytest.mark.asyncio
async def test_old_late_stop_url_cannot_contaminate_a_new_start(monkeypatch):
    monkeypatch.setattr(robot_module, "_RECORDING_REPLY_TIMEOUT_SECONDS", 0.005)
    monkeypatch.setattr(robot_module, "_RECORDING_LATE_REPLY_GRACE_SECONDS", 0.005)
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True
    start_sent = asyncio.Event()

    class ControlledSocket:
        async def send(self, message):
            payload = json.loads(message)
            if payload.get("video_record", {}).get("start") == 0:
                start_sent.set()

    adapter._socket = ControlledSocket()
    with pytest.raises(TimeoutError):
        await adapter.stop_recording()
    await adapter._handle_message(json.dumps({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0}
    }))

    starting = asyncio.create_task(adapter.start_recording())
    await start_sent.wait()
    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "stop": 0,
            "status": "ok",
            "url": "http://camera.local/REC_OLD.mp4",
        }
    }))
    assert not starting.done()
    assert adapter.state.media_url != "http://camera.local/REC_OLD.mp4"

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_NEW.mp4",
        }
    }))
    state = await starting

    assert state.recording is True
    assert state.media_url == "http://camera.local/REC_NEW.mp4"


@pytest.mark.asyncio
async def test_cancelled_start_with_a_late_success_is_stopped_automatically():
    adapter = _goal_attempt_adapter()
    start_sent = asyncio.Event()
    sent = []

    class RecoverySocket:
        async def send(self, message):
            payload = json.loads(message)
            sent.append(payload)
            if payload.get("video_record", {}).get("start") == 0:
                start_sent.set()
            elif payload.get("video_record", {}).get("stop") == 0:
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": "http://camera.local/REC_CANCELLED.mp4",
                    }
                }))

    adapter._socket = RecoverySocket()
    starting = asyncio.create_task(adapter.start_recording())
    await start_sent.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting

    await adapter._handle_message(json.dumps({
        "robot_video_record": {
            "start": 0,
            "status": "ok",
            "url": "http://camera.local/REC_CANCELLED.mp4",
        }
    }))
    compensation = adapter._recording_compensation_task
    assert compensation is not None
    await compensation

    assert [next(iter(item["video_record"])) for item in sent] == ["start", "stop"]
    assert adapter.state.recording is False


def test_ambiguous_video_success_does_not_invent_a_recording_state():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.recording = False

    assert adapter._apply_protocol_state({
        "robot_video_record": {"status": "ok", "url": "robot://unknown.mp4"}
    }) is False

    assert adapter.state.recording is False
    assert adapter.recording_status_known() is False


@pytest.mark.asyncio
async def test_late_stop_without_video_url_never_reuses_shared_photo_url():
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter.state.media_url = "http://camera.local/PHOTO.jpg"
    adapter._recording_media_url = None
    adapter._recording_command_revision = 4
    adapter._recording_write_attempt_epoch = adapter.connection_generation()
    adapter._recording_reply_recovery = (4, "stop")

    await adapter._handle_message(json.dumps({
        "robot_video_record": {"stop": 0, "status": "ok", "url": ""}
    }))

    assert adapter.state.recording is False
    assert adapter.state.media_url is None
    assert adapter.pending_recording_media_url() is None


@pytest.mark.asyncio
async def test_video_url_survives_an_in_recording_photo_when_stop_omits_its_url(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_request(payload, _response_key, timeout_s=5.0):
        del timeout_s
        if "video_record" in payload and "start" in payload["video_record"]:
            return {"status": "ok", "url": "http://camera.local/REC_VIDEO.mp4"}
        if "take_photo" in payload:
            return {"status": "ok", "url": "http://camera.local/PHOTO.jpg"}
        return {"status": "ok"}

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.urls.append(url)
            suffix = "PHOTO.jpg" if url.endswith(".jpg") else "REC_VIDEO.mp4"
            kind = "image" if url.endswith(".jpg") else "video"
            return MediaItem(path=f"C:/downloads/{suffix}", kind=kind, metadata=metadata or {})

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)

    await robot.start_recording()
    photo = await robot.capture_photo()
    stopped = await robot.stop_recording()

    assert photo["local_media_item"]["path"].endswith("PHOTO.jpg")
    assert media.urls == [
        "http://camera.local/PHOTO.jpg",
        "http://camera.local/REC_VIDEO.mp4",
    ]
    assert stopped.media_url == "http://camera.local/REC_VIDEO.mp4"
    assert stopped.media_local_path.endswith("REC_VIDEO.mp4")


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat_count", [1, 2])
async def test_stop_does_not_flatten_newer_recording_heartbeat_during_download(
    monkeypatch,
    heartbeat_count,
):
    adapter = _goal_attempt_adapter()
    adapter.state.recording = True
    adapter._recording_status_known = True
    download_started = asyncio.Event()
    release_download = asyncio.Event()
    sent_actions = []

    async def status_without_reconnect():
        return adapter.state

    adapter.status = status_without_reconnect

    class ReplyingSocket:
        async def send(self, message):
            payload = json.loads(message)
            if payload.get("video_record", {}).get("stop") == 0:
                sent_actions.append("stop")
                url = (
                    "http://camera.local/REC_RACE_A.mp4"
                    if len(sent_actions) == 1
                    else "http://camera.local/REC_RACE_B.mp4"
                )
                await adapter._handle_message(json.dumps({
                    "robot_video_record": {
                        "stop": 0,
                        "status": "ok",
                        "url": url,
                    }
                }))

    class DelayedMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.urls.append(url)
            download_started.set()
            await release_download.wait()
            return MediaItem(
                path=f"C:/downloads/{url.rsplit('/', 1)[-1]}",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    adapter._socket = ReplyingSocket()
    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = DelayedMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)
    stopping = asyncio.create_task(robot.stop_recording())
    await download_started.wait()

    heartbeat = json.dumps({
        "gimbal": {"record_status": "recording", "yaw": 0, "pitch": 0}
    })
    for _ in range(heartbeat_count):
        await adapter._handle_message(heartbeat)
    assert adapter.recording_status_known() is (heartbeat_count > 1)
    assert adapter.state.recording is (heartbeat_count > 1)

    release_download.set()
    stopped = await stopping

    assert sent_actions == ["stop", "stop"]
    assert media.urls == [
        "http://camera.local/REC_RACE_A.mp4",
        "http://camera.local/REC_RACE_B.mp4",
    ]
    assert stopped.recording is False
    assert stopped.media_url.endswith("REC_RACE_B.mp4")
    assert stopped.media_local_path.endswith("REC_RACE_B.mp4")
    assert adapter.state.recording is False
    assert adapter.recording_status_known() is True


@pytest.mark.asyncio
async def test_late_stop_recovery_never_uses_an_in_recording_photo_as_video(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_request(payload, _response_key, timeout_s=5.0):
        del timeout_s
        if "video_record" in payload and "start" in payload["video_record"]:
            return {"status": "ok", "url": "http://camera.local/REC_VIDEO.mp4"}
        if "take_photo" in payload:
            return {"status": "ok", "url": "http://camera.local/PHOTO.jpg"}
        # The camera applies Stop, but its acknowledgement misses the caller's deadline.
        adapter._recording_write_attempt_revision = adapter._recording_command_revision
        adapter._recording_write_attempt_epoch = adapter.connection_generation()
        adapter.state.recording = False
        raise TimeoutError("stop acknowledgement arrived late")

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path=f"C:/downloads/{url.rsplit('/', 1)[-1]}",
                kind="image" if url.endswith(".jpg") else "video",
                metadata=metadata or {},
            )

    monkeypatch.setattr(adapter, "_request", fake_request)
    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)

    await robot.start_recording()
    await robot.capture_photo()
    with pytest.raises(TimeoutError, match="arrived late"):
        await robot.stop_recording()
    adapter._apply_protocol_state({"gimbal": {"record_status": "idle"}})
    recovered = await robot.finalize_capture_recording()

    assert media.urls == [
        "http://camera.local/PHOTO.jpg",
        "http://camera.local/REC_VIDEO.mp4",
    ]
    assert recovered.media_url.endswith("REC_VIDEO.mp4")
    assert recovered.media_local_path.endswith("REC_VIDEO.mp4")


@pytest.mark.asyncio
async def test_disconnected_recording_still_blocks_endpoint_changes():
    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://old.local:8765"
            self.state = RobotState(connected=False, recording=True)
            self.configured = []

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.configured.append(url)
            self.websocket_url = url
            return self.state

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="录制进行中"):
        await robot.configure_websocket_url("ws://new.local:8765")
    with pytest.raises(ValueError, match="录制进行中"):
        await robot.configure_websocket_url_and_commit(
            "ws://new.local:8765", lambda: None
        )

    assert adapter.configured == []
    assert adapter.websocket_url == "ws://old.local:8765"


@pytest.mark.asyncio
async def test_failed_settings_commit_rolls_robot_endpoint_back():
    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://old.local:8765"
            self.state = RobotState(connected=False, recording=False)
            self.configured = []

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.configured.append(url)
            self.websocket_url = url
            return self.state

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    def fail_commit():
        raise OSError("settings disk is read-only")

    with pytest.raises(OSError, match="read-only"):
        await robot.configure_websocket_url_and_commit(
            "ws://new.local:8765", fail_commit
        )

    assert adapter.configured == ["ws://new.local:8765", "ws://old.local:8765"]
    assert adapter.websocket_url == "ws://old.local:8765"


@pytest.mark.asyncio
async def test_endpoint_repair_preserves_a_restored_capture_url(monkeypatch):
    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://wrong.local:8765"
            self.state = RobotState(connected=True, recording=False)

        async def status(self):
            return self.state

        def recording_status_known(self):
            return True

        async def configure_websocket_url(self, url):
            self.websocket_url = url
            # The real hardware adapter clears transient media state when its endpoint changes.
            self.state.media_url = None
            self.state.media_local_path = None
            self.state.media_sync_error = None
            return self.state

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path="C:/downloads/REC_REPAIRED.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = ConfigAdapter()
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)
    robot.restore_recoverable_video_url("http://camera.local/REC_REPAIRED.mp4")

    committed = False

    def commit():
        nonlocal committed
        committed = True

    await robot.configure_websocket_url_and_commit(
        "ws://correct.local:8765",
        commit,
        preserve_capture_recovery=True,
    )
    recovered = await robot.finalize_capture_recording()

    assert committed is True
    assert adapter.websocket_url == "ws://correct.local:8765"
    assert media.urls == ["http://camera.local/REC_REPAIRED.mp4"]
    assert recovered.media_local_path.endswith("REC_REPAIRED.mp4")


@pytest.mark.asyncio
async def test_endpoint_repair_preserves_an_already_downloaded_capture(tmp_path):
    saved_video = tmp_path / "REC_ALREADY_SAVED.mp4"
    saved_video.write_bytes(b"video")

    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://wrong.local:8765"
            self.state = RobotState(connected=False, recording=False)

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.websocket_url = url
            self.state.media_url = None
            self.state.media_local_path = None
            self.state.media_sync_error = None
            return self.state

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_ALREADY_SAVED.mp4",
        str(saved_video),
        "sidecar write failed",
    )

    await robot.configure_websocket_url_and_commit(
        "ws://correct.local:8765",
        lambda: None,
        preserve_capture_recovery=True,
    )
    recovered = await robot.finalize_capture_recording()

    assert recovered.media_url == "http://camera.local/REC_ALREADY_SAVED.mp4"
    assert recovered.media_local_path == str(saved_video)
    assert recovered.media_sync_error == "sidecar write failed"


@pytest.mark.asyncio
async def test_failed_endpoint_repair_restores_capture_recovery_state(tmp_path):
    saved_video = tmp_path / "REC_ROLLBACK.mp4"
    saved_video.write_bytes(b"video")

    class ConfigAdapter:
        def __init__(self):
            self.websocket_url = "ws://wrong.local:8765"
            self.state = RobotState(connected=False, recording=False)

        async def status(self):
            return self.state

        async def configure_websocket_url(self, url):
            self.websocket_url = url
            self.state.media_url = None
            self.state.media_local_path = None
            self.state.media_sync_error = None
            return self.state

    def fail_commit():
        raise OSError("settings store unavailable")

    adapter = ConfigAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_ROLLBACK.mp4",
        str(saved_video),
        "sidecar write failed",
    )

    with pytest.raises(OSError, match="settings store unavailable"):
        await robot.configure_websocket_url_and_commit(
            "ws://correct.local:8765",
            fail_commit,
            preserve_capture_recovery=True,
        )

    recovered = await robot.finalize_capture_recording()
    assert adapter.websocket_url == "ws://wrong.local:8765"
    assert recovered.media_url == "http://camera.local/REC_ROLLBACK.mp4"
    assert recovered.media_local_path == str(saved_video)


@pytest.mark.asyncio
async def test_robot_photo_url_syncs_through_media_service():
    class FakeAdapter:
        def __init__(self):
            self.state = RobotState(connected=True)
            self.websocket_url = "ws://robot.local:8765"

        async def status(self):
            return self.state

        def heartbeat_yaw(self):
            return self.state.yaw

        async def set_camera_angle(self, angle):
            self.state.yaw = angle.angle
            return self.state

        async def capture_photo(self):
            self.state.media_url = "http://robot.local/photo.png"
            return {"status": "ok", "url": self.state.media_url}

    class FakeMedia:
        def __init__(self):
            self.downloads = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.downloads.append((url, metadata, filename_prefix))
            return MediaItem(
                path="/Users/user/Desktop/automated_video_editing/data/downloads/robot-photo.png",
                kind="image",
                metadata=metadata or {},
            )

    events = EventHub()
    media = FakeMedia()
    robot = RobotService(events, adapter=FakeAdapter(), media=media)

    result = await robot.capture_photo()

    assert result["local_media_item"]["kind"] == "image"
    assert media.downloads == [(
        "http://robot.local/photo.png",
        {
            "source": "data/downloads",
            "origin": "robot_hardware",
            "robot_url": "http://robot.local/photo.png",
            "kind_hint": "image",
        },
        "robot-",
    )]
    assert (await robot.status()).media_local_path.endswith("robot-photo.png")


def test_robot_relative_media_paths_resolve_against_robot_host():
    assert resolve_robot_media_url(
        "/media/capture/video.mp4", "ws://10.73.2.199:8765/ws"
    ) == "http://10.73.2.199:8765/media/capture/video.mp4"
    assert resolve_robot_media_url(
        "10.73.2.199:9000/photo.jpg", "ws://10.73.2.199:8765"
    ) == "http://10.73.2.199:9000/photo.jpg"
    assert resolve_robot_media_url(
        "10.73.2.199/photo.jpg", "ws://10.73.2.199:8765"
    ) == "http://10.73.2.199/photo.jpg"
    assert resolve_robot_media_url(
        "https://cdn.example/video.mp4?signature=abc", "ws://10.73.2.199:8765"
    ) == "https://cdn.example/video.mp4?signature=abc"


@pytest.mark.parametrize(
    "value", ["file:///home/robot/video.mp4", "/home/robot/video.mp4", r"C:\\media\\video.mp4"]
)
def test_robot_filesystem_paths_are_rejected(value):
    with pytest.raises(ValueError, match="本机文件路径"):
        resolve_robot_media_url(value, "ws://10.73.2.199:8765")


def test_diagnostic_urls_redact_credentials_and_signed_queries():
    assert safe_url(
        "https://user:password@robot.local/media/video.mp4?signature=secret"
    ) == "https://robot.local/media/video.mp4?<redacted>"


@pytest.mark.asyncio
async def test_zero_yaw_is_a_real_gimbal_start_angle():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.yaw = 0.0
    adapter.state.camera_angle = -90.0
    sent = []

    async def fake_send(payload, *, context="", on_write_started=None):
        if on_write_started is not None:
            on_write_started()
        sent.append(payload)

    adapter._send = fake_send
    await adapter.set_camera_angle(CameraAngle(angle=15))

    assert sent[0]["gimbal_control"]["yaw_start"] == 0.0


@pytest.mark.asyncio
async def test_photo_keeps_current_angle_and_clears_stale_media():
    class AngleTrackingAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                yaw=-90,
                media_url="http://robot.local/old.mp4",
                media_local_path="C:/old.mp4",
            )
            self.websocket_url = "ws://robot.local:8765"
            self.calls = []

        async def status(self):
            return self.state

        def heartbeat_yaw(self):
            return self.state.yaw

        async def set_camera_angle(self, angle):
            self.calls.append(("angle", angle.angle))
            self.state.yaw = angle.angle
            return self.state

        async def capture_photo(self):
            self.calls.append(("photo", self.state.yaw))
            return {"status": "ok", "url": "http://robot.local/new.jpg"}

    class FakeMedia:
        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            return MediaItem(path="C:/downloads/new.jpg", kind="image", metadata=metadata or {})

    adapter = AngleTrackingAdapter()
    result = await RobotService(EventHub(), adapter=adapter, media=FakeMedia()).capture_photo()

    assert adapter.calls == [("photo", -90)]
    assert result["local_media_item"]["path"] == "C:/downloads/new.jpg"
    assert adapter.state.media_url is None
    assert adapter.state.media_local_path == "C:/downloads/new.jpg"


@pytest.mark.asyncio
async def test_normal_recording_keeps_current_gimbal_angle():
    class AngleTrackingAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, yaw=135.0)
            self.calls = []

        async def status(self):
            return self.state

        async def set_camera_angle(self, angle):
            self.calls.append(("angle", angle.angle))
            self.state.yaw = angle.angle
            return self.state

        async def start_recording(self):
            self.calls.append(("record", self.state.yaw))
            self.state.recording = True
            return self.state

    adapter = AngleTrackingAdapter()
    await RobotService(EventHub(), adapter=adapter).start_recording()

    assert adapter.calls == [("record", 135.0)]


@pytest.mark.asyncio
async def test_download_failure_does_not_turn_successful_recording_into_capture_failure(
    monkeypatch,
):
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://192.168.1.201:82/old.mp4",
                media_local_path="C:/downloads/old.mp4",
            )

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://192.168.1.201:82/video.mp4"
            return self.state

    class FailingMedia:
        async def download_url(self, *_args, **_kwargs):
            raise ConnectionError("无法连接到远程服务器")

    adapter = RecordingAdapter()

    async def retry_without_wait(operation, **_kwargs):
        last_error = None
        for _ in range(5):
            try:
                return await operation()
            except ConnectionError as exc:
                last_error = exc
        raise last_error

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    state = await RobotService(EventHub(), adapter=adapter, media=FailingMedia()).stop_recording()

    assert state.recording is False
    assert state.media_url == "http://192.168.1.201:82/video.mp4"
    assert state.media_local_path is None
    assert state.media_sync_error == "摄像头文件传输连接中断，自动重试后仍未成功"


@pytest.mark.asyncio
async def test_next_recording_waits_for_previous_media_and_cannot_corrupt_its_result(
    monkeypatch,
):
    download_started = asyncio.Event()
    release_download = asyncio.Event()

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(connected=True, recording=True)
            self.start_calls = 0

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://192.168.1.201:82/REC_A.mp4"
            return self.state

        async def start_recording(self):
            self.start_calls += 1
            self.state.recording = True
            self.state.media_url = "http://192.168.1.201:82/REC_B.mp4"
            return self.state

    class BlockingMedia:
        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            download_started.set()
            await release_download.wait()
            return MediaItem(
                path="C:/downloads/robot-REC_A.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=BlockingMedia())

    stopping = asyncio.create_task(robot.stop_recording())
    await download_started.wait()
    starting = asyncio.create_task(robot.start_recording())
    await asyncio.sleep(0)
    assert adapter.start_calls == 0

    # Heartbeats continue while a large file downloads. The stop response must keep the
    # operation's URL/path while returning the newest unrelated live telemetry.
    adapter.state.recording = True
    adapter.state.connected = False
    adapter.state.yaw = 77
    adapter.state.battery = 13
    adapter.state.media_url = "http://192.168.1.201:82/STALE_HEARTBEAT.mp4"
    release_download.set()
    stopped = await stopping
    started = await starting

    assert stopped.recording is False
    assert stopped.connected is False
    assert stopped.yaw == 77
    assert stopped.battery == 13
    assert stopped.media_url.endswith("REC_A.mp4")
    assert stopped.media_local_path.endswith("robot-REC_A.mp4")
    assert stopped.media_sync_error is None
    assert started.media_url.endswith("REC_B.mp4")
    assert started.media_local_path is None
    assert adapter.state.media_url.endswith("REC_B.mp4")
    assert adapter.state.media_local_path is None


@pytest.mark.asyncio
async def test_final_stop_url_is_durable_before_video_download_finishes(
    monkeypatch,
    tmp_path,
):
    download_started = asyncio.Event()
    release_download = asyncio.Event()

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(connected=True, recording=True)

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_FINAL.mp4"
            return self.state

    class BlockingMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            download_started.set()
            await release_download.wait()
            return MediaItem(
                path="C:/downloads/REC_FINAL.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    sessions_path = tmp_path / "capture-sessions.json"
    capture = CaptureService(EventHub(), path=sessions_path)
    session = await capture.start("崩溃恢复验证")
    robot = RobotService(EventHub(), adapter=RecordingAdapter(), media=BlockingMedia())

    stopping = asyncio.create_task(
        robot.finalize_capture_recording(
            on_media_url=lambda url: capture.remember_pending_media(session, url),
        )
    )
    try:
        await download_started.wait()
        restored = CaptureService(EventHub(), path=sessions_path).active_session()
        assert restored is not None
        assert restored.pending_media_url == "http://camera.local/REC_FINAL.mp4"
        assert not stopping.done()
    finally:
        release_download.set()
        await stopping


@pytest.mark.asyncio
async def test_media_url_persistence_failure_does_not_skip_video_download(monkeypatch):
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(connected=True, recording=True)

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_FINAL.mp4"
            return self.state

    class RecordingMedia:
        def __init__(self):
            self.downloads = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.downloads.append(url)
            return MediaItem(
                path="C:/downloads/REC_FINAL.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    def fail_persist(_url):
        raise OSError("capture store unavailable")

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    media = RecordingMedia()
    state = await RobotService(
        EventHub(),
        adapter=RecordingAdapter(),
        media=media,
    ).finalize_capture_recording(on_media_url=fail_persist)

    assert media.downloads == ["http://camera.local/REC_FINAL.mp4"]
    assert state.media_local_path == "C:/downloads/REC_FINAL.mp4"


@pytest.mark.asyncio
async def test_stuck_photo_transfer_does_not_delay_stop_or_overwrite_video(
    monkeypatch,
):
    photo_download_started = asyncio.Event()
    release_photo = asyncio.Event()
    stop_called = asyncio.Event()

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://camera.local/REC_ACTIVE.mp4",
            )

        async def status(self):
            return self.state

        async def capture_photo(self):
            self.state.media_url = "http://camera.local/PHOTO.jpg"
            return {"status": "ok", "url": self.state.media_url}

        async def stop_recording(self):
            stop_called.set()
            self.state.recording = False
            self.state.media_url = "http://camera.local/REC_DONE.mp4"
            return self.state

    class BlockingPhotoMedia:
        async def download_url(self, url, metadata=None, **_kwargs):
            if url.endswith("PHOTO.jpg"):
                photo_download_started.set()
                await release_photo.wait()
                return MediaItem(
                    path="C:/downloads/PHOTO.jpg",
                    kind="image",
                    metadata=metadata or {},
                )
            return MediaItem(
                path="C:/downloads/REC_DONE.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=BlockingPhotoMedia())
    photo_task = asyncio.create_task(robot.capture_photo())
    try:
        await photo_download_started.wait()
        stopped = await asyncio.wait_for(robot.stop_recording(), timeout=1.0)
        assert stop_called.is_set()
        assert not photo_task.done()
        assert stopped.media_url.endswith("REC_DONE.mp4")
        assert stopped.media_local_path.endswith("REC_DONE.mp4")
    finally:
        release_photo.set()
        await photo_task

    live = await robot.status()
    assert live.media_url.endswith("REC_DONE.mp4")
    assert live.media_local_path.endswith("REC_DONE.mp4")


@pytest.mark.asyncio
async def test_restored_url_waits_for_observed_idle_before_download(monkeypatch):
    class RecoveringAdapter:
        def __init__(self):
            self.websocket_url = "ws://robot.local:8765"
            self.state = RobotState(connected=True, recording=False)
            self.known = False

        async def status(self):
            return self.state

        def recording_status_known(self):
            return self.known

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path="C:/downloads/REC_RESTORED.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecoveringAdapter()
    media = RecordingMedia()
    robot = RobotService(EventHub(), adapter=adapter, media=media)
    robot.restore_recoverable_video_url("http://camera.local/REC_RESTORED.mp4")

    with pytest.raises(ConnectionError, match="等待机器人同步录制状态"):
        await robot.finalize_capture_recording()
    assert media.urls == []

    adapter.known = True
    recovered = await robot.finalize_capture_recording()
    assert media.urls == ["http://camera.local/REC_RESTORED.mp4"]
    assert recovered.media_local_path.endswith("REC_RESTORED.mp4")


@pytest.mark.asyncio
async def test_new_start_timeout_cannot_recover_previous_recording(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub())
    adapter.state.connected = True
    adapter._recording_media_url = "http://camera.local/REC_OLD.mp4"

    async def timeout_start(*_args, **_kwargs):
        raise TimeoutError("start acknowledgement timed out")

    monkeypatch.setattr(adapter, "_request", timeout_start)
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url("http://camera.local/REC_OLD.mp4")

    with pytest.raises(TimeoutError, match="timed out"):
        await robot.start_recording()

    adapter._apply_protocol_state({
        "gimbal": {"record_status": "idle", "yaw": 0, "pitch": 0},
    })
    with pytest.raises(ValueError, match="没有可恢复的视频地址"):
        await robot.finalize_capture_recording()
    assert adapter.pending_recording_media_url() is None


@pytest.mark.asyncio
async def test_verified_local_recovery_finishes_before_first_post_restart_heartbeat(tmp_path):
    video = tmp_path / "already-downloaded.mp4"
    video.write_bytes(b"video")

    class RecoveringAdapter:
        def __init__(self):
            self.state = RobotState(connected=False, recording=False)

        async def status(self):
            return self.state

        def recording_status_known(self):
            return False

    robot = RobotService(EventHub(), adapter=RecoveringAdapter())
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_DONE.mp4",
        str(video),
        "old transfer warning",
    )

    recovered = await robot.finalize_capture_recording()
    assert recovered.media_local_path == str(video)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recording", "known"),
    [(True, True), (False, False)],
)
async def test_same_process_local_file_does_not_bypass_stop_recovery(
    tmp_path,
    recording,
    known,
):
    video = tmp_path / "same-process-download.mp4"
    video.write_bytes(b"video")

    class UnstableAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                recording=recording,
                media_url="http://camera.local/REC_UNSTABLE.mp4",
                media_local_path=str(video),
            )
            self.stop_calls = 0

        async def status(self):
            return self.state

        def recording_status_known(self):
            return known

        async def stop_recording(self):
            self.stop_calls += 1
            raise ConnectionError("cleanup Stop was not acknowledged")

    adapter = UnstableAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(RuntimeError, match="补发停止指令失败"):
        await robot.finalize_capture_recording()

    assert adapter.stop_calls == 1
    assert adapter.state.media_local_path == str(video)


@pytest.mark.asyncio
async def test_photo_path_is_never_restored_as_a_completed_recording(tmp_path):
    photo = tmp_path / "PHOTO.jpg"
    photo.write_bytes(b"photo")

    class RecoveringAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=False)

        async def status(self):
            return self.state

        def recording_status_known(self):
            return True

    robot = RobotService(EventHub(), adapter=RecoveringAdapter(), media=None)
    robot.restore_recoverable_video_url(
        "http://camera.local/REC_DONE.mp4",
        str(photo),
    )

    recovered = await robot.finalize_capture_recording()

    assert recovered.media_local_path is None
    assert recovered.media_sync_error == "媒体服务不可用，无法保存机器人文件"


@pytest.mark.asyncio
async def test_discard_requires_observed_idle_and_forgets_recovery_only_after_commit():
    class RecoveringAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=False)
            self.known = False

        async def status(self):
            return self.state

        def recording_status_known(self):
            return self.known

    adapter = RecoveringAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    robot.restore_recoverable_video_url("http://camera.local/REC_LOST.mp4")
    committed = False

    async def commit():
        nonlocal committed
        committed = True
        return "discarded"

    with pytest.raises(ConnectionError, match="等待机器人同步录制状态"):
        await robot.discard_capture_recovery(commit)
    assert committed is False

    adapter.known = True
    assert await robot.discard_capture_recovery(commit) == "discarded"
    assert committed is True
    with pytest.raises(ValueError, match="没有可恢复的视频地址"):
        await robot.finalize_capture_recording()


@pytest.mark.asyncio
async def test_repeated_stop_is_refused_without_erasing_the_saved_result():
    class IdleAdapter:
        def __init__(self):
            self.state = RobotState(
                connected=True,
                recording=False,
                media_url="http://camera.local/REC_DONE.mp4",
                media_local_path="C:/downloads/REC_DONE.mp4",
            )
            self.stop_calls = 0

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.stop_calls += 1
            return self.state

    adapter = IdleAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="当前未在录制"):
        await robot.stop_recording()

    assert adapter.stop_calls == 0
    assert adapter.state.media_url.endswith("REC_DONE.mp4")
    assert adapter.state.media_local_path.endswith("REC_DONE.mp4")


@pytest.mark.asyncio
async def test_stop_preserves_a_recording_url_returned_at_start(monkeypatch):
    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765"
            self.state = RobotState(
                connected=True,
                recording=True,
                media_url="http://192.168.1.201:82/REC_FROM_START.mp4",
            )

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            return self.state

    class RecordingMedia:
        def __init__(self):
            self.urls = []

        async def download_url(self, url, metadata=None, filename_prefix="", **_kwargs):
            self.urls.append(url)
            return MediaItem(
                path="C:/downloads/robot-REC_FROM_START.mp4",
                kind="video",
                metadata=metadata or {},
            )

    async def retry_without_wait(operation, **_kwargs):
        return await operation()

    monkeypatch.setattr(robot_module, "retry_camera_media_download", retry_without_wait)
    adapter = RecordingAdapter()
    media = RecordingMedia()
    stopped = await RobotService(EventHub(), adapter=adapter, media=media).stop_recording()

    assert media.urls == ["http://192.168.1.201:82/REC_FROM_START.mp4"]
    assert stopped.media_url.endswith("REC_FROM_START.mp4")
    assert stopped.media_local_path.endswith("robot-REC_FROM_START.mp4")


@pytest.mark.asyncio
async def test_disposable_preview_download_holds_capture_lock_and_resolves_relative_url():
    preview_started = asyncio.Event()
    release_preview = asyncio.Event()
    downloaded_urls = []

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765/ws"
            self.state = RobotState(connected=True, recording=True)
            self.start_calls = 0

        async def status(self):
            return self.state

        async def stop_recording(self):
            self.state.recording = False
            self.state.media_url = "/mp4/REC_PREVIEW.mp4"
            return self.state

        async def start_recording(self):
            self.start_calls += 1
            self.state.recording = True
            return self.state

    async def download_preview(url):
        downloaded_urls.append(url)
        preview_started.set()
        await release_preview.wait()
        return "temporary-preview"

    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter)
    stopping = asyncio.create_task(robot.stop_recording_with_download(download_preview))
    await preview_started.wait()
    starting = asyncio.create_task(robot.start_recording())
    await asyncio.sleep(0)
    assert adapter.start_calls == 0

    release_preview.set()
    stopped, preview = await stopping
    await starting

    assert stopped.media_url == "/mp4/REC_PREVIEW.mp4"
    assert preview == "temporary-preview"
    assert downloaded_urls == ["http://10.73.2.199:8765/mp4/REC_PREVIEW.mp4"]
    assert adapter.start_calls == 1


@pytest.mark.asyncio
async def test_disposable_preview_cleanup_url_uses_caller_downloader_only():
    caller_downloads = []

    class RecordingAdapter:
        def __init__(self):
            self.websocket_url = "ws://10.73.2.199:8765/ws"
            self.state = RobotState(connected=True, recording=True)
            self.known = True
            self.stop_calls = 0

        async def status(self):
            return self.state

        def recording_status_known(self):
            return self.known

        async def stop_recording(self):
            self.stop_calls += 1
            self.state.recording = False
            if self.stop_calls == 1:
                self.state.media_url = "/mp4/REC_PREVIEW_A.mp4"
                self.known = False
            else:
                self.state.media_url = "/mp4/REC_PREVIEW_B.mp4"
                self.known = True
            return self.state

    class ForbiddenMediaPool:
        async def download_url(self, *_args, **_kwargs):
            raise AssertionError("temporary preview must not enter the media pool")

    async def download_preview(url):
        caller_downloads.append(url)
        return "temporary-preview-b"

    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter, media=ForbiddenMediaPool())
    stopped, preview = await robot.stop_recording_with_download(download_preview)

    assert adapter.stop_calls == 2
    assert stopped.media_url == "/mp4/REC_PREVIEW_B.mp4"
    assert caller_downloads == ["http://10.73.2.199:8765/mp4/REC_PREVIEW_B.mp4"]
    assert preview == "temporary-preview-b"


@pytest.mark.asyncio
async def test_start_recording_refuses_an_already_active_recording():
    class RecordingAdapter:
        def __init__(self):
            self.state = RobotState(connected=True, recording=True)
            self.start_calls = 0

        async def status(self):
            return self.state

        async def start_recording(self):
            self.start_calls += 1
            return self.state

    adapter = RecordingAdapter()
    robot = RobotService(EventHub(), adapter=adapter)

    with pytest.raises(ValueError, match="已在录制"):
        await robot.start_recording()
    assert adapter.start_calls == 0


def _heartbeat(adapter, **blocks):
    """Feed one heartbeat straight into the state parser."""
    adapter._apply_protocol_state(blocks)


def test_heartbeat_records_hardware_and_map_mode():
    """system.status and map.mode were read past entirely, so a faulted robot in mapping mode
    looked identical to a healthy one ready to drive."""
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")

    _heartbeat(
        adapter,
        system={"status": "error", "battery": 41},
        map={"mode": "mapping", "name": "map1", "status": "faild"},
    )

    assert adapter.state.system_status == "error"
    assert adapter.state.map_mode == "mapping"
    assert adapter.state.battery == 41
    # Their spelling, normalised, so the UI never has to show "faild".
    assert adapter.state.map_status == "failed"


def test_mapping_mode_refuses_a_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "ready"}, map={"mode": "mapping", "status": "ready"})

    with pytest.raises(ValueError, match="扫图模式"):
        refuse_if_unfit_to_drive(adapter.state)


def test_hardware_fault_refuses_a_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "error"}, map={"mode": "localization", "status": "ready"})

    with pytest.raises(ValueError, match="硬件状态异常"):
        refuse_if_unfit_to_drive(adapter.state)


def test_lost_localisation_refuses_a_goal():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "ready"}, map={"mode": "localization", "status": "faild"})

    with pytest.raises(ValueError, match="定位失败"):
        refuse_if_unfit_to_drive(adapter.state)


def test_a_healthy_robot_is_fit_to_drive():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")
    _heartbeat(adapter, system={"status": "ready"}, map={"mode": "localization", "status": "ready"})

    refuse_if_unfit_to_drive(adapter.state)


def test_a_robot_that_has_said_nothing_yet_is_not_blocked():
    """Before the first heartbeat every field is None. Refusing then would make the app
    unusable until a heartbeat happened to arrive."""
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")

    refuse_if_unfit_to_drive(adapter.state)


def test_task_goal_status_is_normalised_too():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.invalid:8765")

    _heartbeat(adapter, task={"goal_status": "faild", "object_status": "faild", "goal_id": 3})

    assert adapter.state.goal_status == "failed"
    assert adapter.state.object_status == "failed"


def test_a_heartbeat_without_a_record_field_leaves_the_recording_alone():
    """`.get` on a missing key returns None, which is not "recording" — so a gimbal block
    carrying only yaw would rewrite a running recording as stopped. Whether to send the stop
    command is decided from this flag and nothing else, so absent has to mean unchanged.
    """
    import asyncio
    import json

    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True

    recording = json.dumps({"gimbal": {"record_status": "recording", "yaw": 0, "pitch": 0}})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        adapter._handle_message(recording)
    )
    assert adapter.state.recording is True

    # The same robot, a beat later, reporting only where the camera is pointed.
    partial = json.dumps({"gimbal": {"yaw": 12, "pitch": 0}})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        adapter._handle_message(partial)
    )
    assert adapter.state.recording is True, "a silent field must not stop a running recording"

    idle = json.dumps({"gimbal": {"record_status": "idle", "yaw": 12}})
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        adapter._handle_message(idle)
    )
    assert adapter.state.recording is False


def test_zero_heartbeat_updates_camera_angle_instead_of_preserving_minus_ninety():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.camera_angle = -90.0

    _heartbeat(adapter, gimbal={"yaw": 0, "pitch": 0})

    assert adapter.state.yaw == 0.0
    assert adapter.state.camera_angle == 0.0


@pytest.mark.asyncio
async def test_gimbal_diagnostics_separate_sent_target_from_physical_heartbeat():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"
    adapter.state.moving = True

    async def fake_connect():
        return adapter.state

    class FakeSocket:
        async def send(self, _message):
            return None

    adapter.connect = fake_connect
    adapter._socket = FakeSocket()

    await adapter.set_gimbal(
        GimbalMoveRequest(
            yaw_start=5,
            yaw_end=-40,
            yaw_speed=3,
            pitch_start=0,
            pitch_end=-8,
            pitch_speed=2,
            zoom_start=1,
            zoom_end=1,
        ),
        context="cruise_moving",
    )

    command = adapter.state.diagnostics.last_gimbal_command
    assert command is not None
    assert command.context == "cruise_moving"
    assert command.base_motion_intent == "moving"
    assert command.payload["gimbal_control"] == {
        "mode": 1,
        "yaw_start": 5.0,
        "yaw_speed": 3.0,
        "yaw_end": -40.0,
        "pitch_start": 0.0,
        "pitch_speed": 2.0,
        "pitch_end": -8.0,
        "zoom_start": 1.0,
        "zoom_speed": 0,
        "zoom_end": 1.0,
    }
    # The ordinary state is optimistic, but the physical diagnostic remains absent until a
    # hardware heartbeat arrives. The UI must never confuse these two values.
    assert adapter.state.yaw == -40
    assert adapter.state.diagnostics.last_heartbeat is None

    await adapter._handle_message(json.dumps({
        "task": {"goal_status": "going", "goal_id": 7},
        "gimbal": {"yaw": -12, "pitch": -3, "mode": 1},
    }))
    heartbeat = adapter.state.diagnostics.last_heartbeat
    assert heartbeat is not None
    assert heartbeat.yaw == -12
    assert heartbeat.pitch == -3
    assert heartbeat.gimbal_mode == 1
    assert heartbeat.payload["task"]["goal_status"] == "going"
    assert heartbeat.payload["gimbal"]["yaw"] == -12

    adapter.state.diagnostics.last_goal_attempt = GoalCommandAttemptDiagnostic(
        context="cruise_navigation_goal",
        payload={"set_goal": {"path_name": "path-a", "goal_id": 7}},
    )
    adapter._clear_heartbeat_diagnostics()
    assert adapter.state.diagnostics.last_goal_attempt is None
    assert adapter.state.diagnostics.last_heartbeat is None
    assert adapter.state.diagnostics.last_goal_command is None
    assert adapter.state.diagnostics.last_gimbal_command is None


@pytest.mark.asyncio
async def test_stationary_camerawork_diagnostic_marks_the_base_as_parked():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_connect():
        return adapter.state

    class FakeSocket:
        async def send(self, _message):
            return None

    adapter.connect = fake_connect
    adapter._socket = FakeSocket()
    await adapter.set_gimbal(
        GimbalMoveRequest(
            yaw_start=0,
            yaw_end=10,
            yaw_speed=2,
            pitch_start=0,
            pitch_end=-5,
            pitch_speed=2,
            zoom_start=1,
            zoom_end=1,
        ),
        context="cruise_stationary_camerawork",
    )

    command = adapter.state.diagnostics.last_gimbal_command
    assert command is not None
    assert command.context == "cruise_stationary_camerawork"
    assert command.base_motion_intent == "stationary"


@pytest.mark.asyncio
async def test_gimbal_command_boundary_cannot_pair_pre_command_yaw_with_post_command_pitch():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter.state.connected = True
    adapter.state.connection_status = "connected"

    async def fake_connect():
        return adapter.state

    class FakeSocket:
        async def send(self, _message):
            return None

    adapter.connect = fake_connect
    adapter._socket = FakeSocket()
    adapter._apply_protocol_state({"gimbal": {"yaw": 17}})

    await adapter.set_gimbal(GimbalMoveRequest(yaw_start=17, yaw_end=0, pitch_end=0))
    adapter._apply_protocol_state({"gimbal": {"pitch": 0}})
    assert adapter.heartbeat_revision() is None

    adapter._apply_protocol_state({"gimbal": {"yaw": 0}})
    assert adapter.heartbeat_revision() == 1


@pytest.mark.asyncio
async def test_disconnect_discards_a_final_heartbeat_that_arrives_during_socket_close():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter._apply_protocol_state({
        "system": {"status": "ready"},
        "map": {"mode": "localization", "name": "map1", "status": "ready"},
        "naviagtion": {"status": "ready"},
        "task": {"path_file": "path1", "goal_id": 3, "goal_status": "going"},
        "gimbal": {"yaw": 1, "pitch": 2},
    })
    adapter.state.diagnostics.last_gimbal_command = GimbalCommandDiagnostic(context="manual")

    class ClosingSocket:
        async def close(self):
            adapter._apply_protocol_state({"gimbal": {"yaw": 9, "pitch": 9}})

    adapter._socket = ClosingSocket()
    await adapter._stop_connection_loop()

    assert adapter.state.diagnostics.last_heartbeat is None
    assert adapter.state.diagnostics.last_goal_command is None
    assert adapter.state.diagnostics.last_gimbal_command is None
    assert adapter.heartbeat_revision() is None
    assert adapter.state.system_status is None
    assert adapter.state.map_name is None
    assert adapter.state.map_mode is None
    assert adapter.state.map_status is None
    assert adapter.state.navigation_status is None
    assert adapter.state.path_file is None
    assert adapter.state.goal_id is None
    assert adapter.state.moving is False


@pytest.mark.asyncio
async def test_clean_websocket_close_immediately_fails_old_pending_requests(monkeypatch):
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    closed_socket = object()

    class ClosedSocket:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def close(self):
            return None

    closed_socket = ClosedSocket()

    class FakeWebsockets:
        @staticmethod
        async def connect(*_args, **_kwargs):
            return closed_socket

    monkeypatch.setattr(robot_module, "_load_websockets", lambda: FakeWebsockets)
    loop = asyncio.get_running_loop()
    pending = loop.create_future()
    adapter._pending["robot_map_list"] = pending
    adapter._pending_connection_epochs["robot_map_list"] = 0

    task = asyncio.create_task(adapter._connection_loop())
    with pytest.raises(ConnectionError, match="websocket closed"):
        await asyncio.wait_for(asyncio.shield(pending), timeout=0.1)

    assert adapter.state.connected is False
    assert adapter.state.connection_status == "reconnecting"
    assert adapter._socket is None
    assert adapter._pending == {}
    assert adapter._pending_connection_epochs == {}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_reply_from_retired_connection_cannot_resolve_new_waiter():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter._connection_epoch = 4
    loop = asyncio.get_running_loop()
    pending = loop.create_future()
    adapter._pending["robot_map_list"] = pending
    adapter._pending_connection_epochs["robot_map_list"] = 4

    await adapter._handle_message(
        json.dumps({"robot_map_list": ["stale"]}),
        connection_epoch=3,
    )

    assert not pending.done()


def test_new_map_heartbeat_drops_task_identity_from_the_previous_map():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")
    adapter._apply_protocol_state({
        "map": {"mode": "localization", "name": "map1", "status": "ready"},
        "naviagtion": {"status": "ready"},
        "task": {"path_file": "path1", "goal_id": 3, "goal_status": "done"},
    })

    adapter._apply_protocol_state({
        "map": {"mode": "localization", "name": "map2", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })

    assert adapter.state.map_name == "map2"
    assert adapter.state.path_file is None
    assert adapter.state.goal_id is None
    assert adapter.state.goal_status is None


def test_partial_heartbeat_preserves_each_physical_axis_and_movement_state():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")

    adapter._apply_protocol_state({
        "task": {"goal_status": "going", "goal_id": 4},
        "gimbal": {"yaw": 7, "pitch": -2, "zoom": 1.2, "mode": 1},
    })
    first = adapter.state.diagnostics.last_heartbeat
    assert first is not None
    assert adapter.heartbeat_revision() == 1

    # A yaw-only sample is still useful, but it must not erase the last physical pitch.
    adapter._apply_protocol_state({"gimbal": {"yaw": 8}})
    second = adapter.state.diagnostics.last_heartbeat
    assert second is not None
    assert second.yaw == 8
    assert second.pitch == -2
    assert second.pitch_received_at == first.pitch_received_at
    assert second.zoom == 1.2
    assert second.zoom_received_at == first.zoom_received_at
    assert adapter.state.pitch == -2
    assert adapter.heartbeat_revision() == 1

    # The matching pitch-only packet completes one new logical two-axis sample.
    adapter._apply_protocol_state({"gimbal": {"pitch": -2}})
    assert adapter.heartbeat_revision() == 2

    # A zoom-only heartbeat updates the diagnostic independently without inventing
    # a new yaw/pitch pose sample.
    adapter._apply_protocol_state({"gimbal": {"zoom": 1.5}})
    zoom_frame = adapter.state.diagnostics.last_heartbeat
    assert zoom_frame is not None
    assert zoom_frame.zoom == 1.5
    # Consecutive packets can share a timestamp on Windows. Sequence/revision is the
    # authoritative freshness signal; the diagnostic timestamp must only be monotonic.
    assert zoom_frame.sequence > first.sequence
    assert zoom_frame.zoom_received_at >= first.zoom_received_at
    assert adapter.heartbeat_revision() == 2

    # Recording-only gimbal beats and partial task beats update diagnostics without inventing
    # a new pose or falsely turning an already-moving base into a static one.
    adapter._apply_protocol_state({"gimbal": {"record_status": "recording"}})
    adapter._apply_protocol_state({"task": {"goal_id": 4}})
    assert adapter.state.yaw == 8
    assert adapter.state.pitch == -2
    assert adapter.state.moving is True
    assert adapter.heartbeat_revision() == 2
    latest = adapter.state.diagnostics.last_heartbeat
    assert latest.sequence == 6
    assert latest.task_goal_status == "going"
    assert latest.goal_id == 4
    assert latest.task_goal_status_received_at == first.task_goal_status_received_at


def test_split_heartbeat_retains_the_identity_that_supplied_each_goal_status():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")

    adapter._apply_protocol_state({
        "naviagtion": {"goal_status": "going"},
        "task": {
            "path_file": "path-a",
            "goal_id": 1,
            "goal_object": "car",
            "goal_status": "going",
            "object_status": "done",
        },
    })
    status_frame = adapter.state.diagnostics.last_heartbeat
    assert status_frame is not None
    expected_identity = {
        "path_file": "path-a",
        "goal_id": 1,
        "goal_object": "car",
    }
    assert status_frame.task_goal_status_identity == expected_identity
    assert status_frame.navigation_goal_status_identity == expected_identity
    assert status_frame.object_status_identity == expected_identity

    # A later split frame has no task identity. It must update the physical pose without
    # divorcing the retained statuses from the point that originally supplied them.
    adapter._apply_protocol_state({"gimbal": {"yaw": 4, "pitch": -2}})
    pose_frame = adapter.state.diagnostics.last_heartbeat
    assert pose_frame is not None
    assert pose_frame.payload == {"gimbal": {"yaw": 4, "pitch": -2}}
    assert pose_frame.task_goal_status_identity == expected_identity
    assert pose_frame.navigation_goal_status_identity == expected_identity
    assert pose_frame.object_status_identity == expected_identity


def test_a_new_map_heartbeat_cannot_retain_the_previous_maps_task_identity():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")

    adapter._apply_protocol_state({
        "map": {"mode": "localization", "name": "map-a", "status": "ready"},
        "naviagtion": {"status": "ready", "goal_status": "done"},
        "task": {
            "path_file": "path-a",
            "goal_id": 7,
            "goal_status": "done",
            "object_status": "done",
        },
        "gimbal": {"yaw": 4, "pitch": -2},
    })

    adapter._apply_protocol_state({
        "map": {"mode": "localization", "name": "map-b", "status": "ready"},
        "naviagtion": {"status": "ready"},
    })

    heartbeat = adapter.state.diagnostics.last_heartbeat
    assert heartbeat is not None
    assert adapter.state.path_file is None
    assert adapter.state.goal_id is None
    assert heartbeat.path_file is None
    assert heartbeat.goal_id is None
    assert heartbeat.task_goal_status is None
    assert heartbeat.navigation_goal_status is None
    assert heartbeat.object_status is None
    # Gimbal pose is independent of map/task ownership and remains useful.
    assert heartbeat.yaw == 4
    assert heartbeat.pitch == -2
