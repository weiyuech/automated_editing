import json
import tempfile
from pathlib import Path

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CruisePoint,
    CruiseRequest,
    CruiseRoute,
    RobotState,
)
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.cruise_routes import CruiseRouteStore
from automated_video_editing_backend.services.robot import RobotService


def request_for(map_name="map1", paths=("path1",)):
    return CruiseRequest(
        map_name=map_name,
        points=[CruisePoint(path_name=path, goal_id=index + 1) for index, path in enumerate(paths)],
    )


def test_saving_the_same_name_replaces_and_promotes(tmp_path):
    store = CruiseRouteStore(path=tmp_path / "cruise-routes.json")

    store.save("morning", request_for(paths=("path1",)))
    store.save("evening", request_for(paths=("path2",)))
    updated = store.save("morning", request_for(paths=("path3",)))

    names = [route.name for route in store.list_routes()]
    assert names == ["morning", "evening"]
    assert updated.request.points[0].path_name == "path3"
    assert len(store.list_routes()) == 2


def test_store_caps_at_ten_and_evicts_least_recently_used(tmp_path):
    store = CruiseRouteStore(path=tmp_path / "cruise-routes.json")
    for index in range(12):
        store.save(f"route-{index}", request_for())

    names = [route.name for route in store.list_routes()]
    assert len(names) == CruiseRouteStore.MAX_ROUTES
    assert names[0] == "route-11"
    assert "route-0" not in names and "route-1" not in names


def test_touch_promotes_and_survives_a_reload(tmp_path):
    path = tmp_path / "cruise-routes.json"
    store = CruiseRouteStore(path=path)
    first = store.save("first", request_for())
    store.save("second", request_for())

    assert [route.name for route in store.list_routes()] == ["second", "first"]
    store.touch(first.id)
    assert [route.name for route in store.list_routes()] == ["first", "second"]

    reloaded = CruiseRouteStore(path=path)
    assert [route.name for route in reloaded.list_routes()] == ["first", "second"]
    assert reloaded.get(first.id).request.map_name == "map1"


def test_corrupt_and_unreadable_entries_are_dropped_not_fatal(tmp_path):
    path = tmp_path / "cruise-routes.json"
    path.write_text(json.dumps([{"name": "broken"}, "nonsense"]), encoding="utf-8")
    assert CruiseRouteStore(path=path).list_routes() == []

    path.write_text("{not json", encoding="utf-8")
    assert CruiseRouteStore(path=path).list_routes() == []


def test_delete_reports_whether_anything_was_removed(tmp_path):
    store = CruiseRouteStore(path=tmp_path / "cruise-routes.json")
    route = store.save("only", request_for())

    assert store.delete(route.id) is True
    assert store.delete(route.id) is False
    assert store.list_routes() == []


class FakeRobotAdapter:
    def __init__(self, maps=("map1",), paths=("path1",), fail=None):
        self.state = RobotState(connected=True, map_name="map1")
        self._maps = list(maps)
        self._paths = list(paths)
        self._fail = fail
        self.dispatched = []

    async def status(self):
        return self.state

    async def set_goal(self, command):
        self.dispatched.append(command)
        return {"goal_check": "true"}

    async def map_list(self):
        if self._fail:
            raise self._fail
        return self._maps

    async def path_list(self, map_name):
        return self._paths


def cruise_with(adapter):
    events = EventHub()
    capture = CaptureService(events, path=Path(tempfile.mkdtemp()) / "sessions.json")
    return CruiseService(events, RobotService(events, adapter=adapter), capture)


def route_for(**kwargs):
    return CruiseRoute(name="saved", request=request_for(**kwargs))


@pytest.mark.asyncio
async def test_validation_passes_when_map_and_paths_exist():
    cruise = cruise_with(FakeRobotAdapter(maps=["map1"], paths=["path1"]))
    result = await cruise.validate_route(route_for())

    assert result.ok is True
    assert result.checked is True
    assert result.issues == []


@pytest.mark.asyncio
async def test_missing_map_and_missing_path_are_errors_that_block_a_start():
    cruise = cruise_with(FakeRobotAdapter(maps=["other-map"]))
    missing_map = await cruise.validate_route(route_for())
    assert missing_map.ok is False
    assert missing_map.issues[0].field == "map_name"

    cruise = cruise_with(FakeRobotAdapter(maps=["map1"], paths=["path9"]))
    missing_path = await cruise.validate_route(route_for(paths=("path1", "path2")))
    assert missing_path.ok is False
    assert {issue.value for issue in missing_path.issues} == {"path1", "path2"}
    # Each error points at the rows the operator has to fix.
    assert [issue.point_indexes for issue in missing_path.issues] == [[0], [1]]


@pytest.mark.asyncio
async def test_unreachable_robot_warns_instead_of_blocking():
    cruise = cruise_with(FakeRobotAdapter(fail=ConnectionError("socket closed")))
    result = await cruise.validate_route(route_for())

    assert result.checked is False
    # A robot we cannot reach is not proof the route is wrong, so this must not be an error.
    assert result.ok is True
    assert result.issues[0].level == "warning"
    assert result.issues[0].field == "robot"


@pytest.mark.asyncio
async def test_empty_robot_lists_warn_rather_than_condemning_every_point():
    cruise = cruise_with(FakeRobotAdapter(maps=[]))
    empty_maps = await cruise.validate_route(route_for())
    assert empty_maps.ok is True
    assert empty_maps.issues[0].level == "warning"

    cruise = cruise_with(FakeRobotAdapter(maps=["map1"], paths=[]))
    empty_paths = await cruise.validate_route(route_for())
    assert empty_paths.ok is True
    assert empty_paths.issues[0].level == "warning"


@pytest.mark.asyncio
async def test_validation_never_dispatches_a_goal():
    adapter = FakeRobotAdapter(maps=["map1"], paths=["path1"])
    cruise = cruise_with(adapter)
    await cruise.validate_route(route_for())

    # set_goal is the only way to test a goal_id and it drives the robot whenever the id is
    # valid, so validation must never reach for it.
    assert adapter.dispatched == []
    assert cruise.is_running is False
