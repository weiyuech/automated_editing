import asyncio
import json

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

    async def fake_send(payload, *, context=""):
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
    adapter._apply_protocol_state({"gimbal": {"yaw": 1, "pitch": 2}})
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


def test_partial_heartbeat_preserves_each_physical_axis_and_movement_state():
    adapter = HardwareRobotAdapter(EventHub(), "ws://robot.local:8765")

    adapter._apply_protocol_state({
        "task": {"goal_status": "going", "goal_id": 4},
        "gimbal": {"yaw": 7, "pitch": -2, "mode": 1},
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
    assert adapter.state.pitch == -2
    assert adapter.heartbeat_revision() == 1

    # The matching pitch-only packet completes one new logical two-axis sample.
    adapter._apply_protocol_state({"gimbal": {"pitch": -2}})
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
    assert latest.sequence == 5
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
