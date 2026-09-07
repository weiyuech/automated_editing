import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automated_video_editing_backend.api.routes import build_router
from automated_video_editing_backend.api.ws import _handle_command


class RobotStub:
    def __init__(self) -> None:
        self.commands: list[tuple[str, object]] = []

    async def switch_map(self, map_name: str):
        self.commands.append(("switch-map", map_name))
        return {"ok": True}

    async def set_goal(self, command):
        self.commands.append(("goal", command))
        return {"goal_check": "true"}

    async def move(self, command):
        self.commands.append(("move", command))
        return {"ok": True}


class CruiseStub:
    def __init__(self, running: bool) -> None:
        self.is_running = running


def _client(monkeypatch, robot: RobotStub, cruise: CruiseStub) -> TestClient:
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "navigation-test-token")
    unused = object()
    app = FastAPI()
    app.include_router(
        build_router(
            robot=robot,
            capture=unused,
            cruise=cruise,
            cruise_routes=unused,
            media=unused,
            jobs=unused,
            vault=unused,
            settings=unused,
            llm=unused,
            tts=unused,
            seedance=unused,
            renamer=unused,
            framing_test=unused,
            admin_access=unused,
        ),
        prefix="/api",
    )
    return TestClient(app)


def test_cruise_owns_map_and_goal_commands(monkeypatch):
    robot = RobotStub()
    client = _client(monkeypatch, robot, CruiseStub(running=True))
    headers = {"x-bridge-token": "navigation-test-token"}

    switch = client.post(
        "/api/robot/switch-map",
        headers=headers,
        json={"map_name": "exhibition-hall"},
    )
    goal = client.post(
        "/api/robot/goal",
        headers=headers,
        json={"path_name": "tour.csv", "goal_id": 3},
    )
    move = client.post(
        "/api/robot/move",
        headers=headers,
        json={"direction": "forward"},
    )

    assert switch.status_code == 409
    assert goal.status_code == 409
    assert move.status_code == 409
    assert switch.json()["detail"] == "巡游正在进行，地图与导航由巡游控制"
    assert goal.json()["detail"] == "巡游正在进行，地图与导航由巡游控制"
    assert move.json()["detail"] == "巡游正在进行，地图与导航由巡游控制"
    assert robot.commands == []


def test_manual_map_and_goal_commands_still_work_while_idle(monkeypatch):
    robot = RobotStub()
    client = _client(monkeypatch, robot, CruiseStub(running=False))
    headers = {"x-bridge-token": "navigation-test-token"}

    switch = client.post(
        "/api/robot/switch-map",
        headers=headers,
        json={"map_name": "exhibition-hall"},
    )
    goal = client.post(
        "/api/robot/goal",
        headers=headers,
        json={"path_name": "tour.csv", "goal_id": 3},
    )
    move = client.post(
        "/api/robot/move",
        headers=headers,
        json={"direction": "forward"},
    )

    assert switch.status_code == 200
    assert goal.status_code == 200
    assert move.status_code == 200
    assert [name for name, _ in robot.commands] == ["switch-map", "goal", "move"]


@pytest.mark.asyncio
async def test_cruise_owns_websocket_manual_move_commands():
    robot = RobotStub()

    with pytest.raises(ValueError, match="地图与导航由巡游控制"):
        await _handle_command(
            object(),
            "ROBOT_MOVE",
            {"direction": "forward"},
            robot,
            object(),
            CruiseStub(running=True),
            object(),
        )

    assert robot.commands == []
