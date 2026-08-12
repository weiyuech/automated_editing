from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from automated_video_editing_backend.core.models import CruiseRequest, CruiseRoute, utc_now
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.core.store import read_json, write_json


class CruiseRouteStore:
    """Saved cruise setups, kept most-recently-used first and capped at MAX_ROUTES.

    Separate from robot-live-system's tour-list.json by design: the two projects share no
    files and no runtime state.
    """

    MAX_ROUTES = 10

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or generated_path("data", "cruise-routes.json")
        self.load_problem = ""
        self._routes: list[CruiseRoute] = self._load()

    def list_routes(self) -> list[CruiseRoute]:
        return list(self._routes)

    def get(self, route_id: str) -> CruiseRoute | None:
        return next((route for route in self._routes if route.id == route_id), None)

    def save(self, name: str, request: CruiseRequest) -> CruiseRoute:
        """Save under a name, replacing any existing route with the same name."""
        existing = next((route for route in self._routes if route.name == name), None)
        if existing is not None:
            existing.request = request
            existing.last_used_at = utc_now()
            self._routes.remove(existing)
            route = existing
        else:
            route = CruiseRoute(name=name, request=request)

        self._routes.insert(0, route)
        del self._routes[self.MAX_ROUTES :]
        self._save()
        return route

    def touch(self, route_id: str) -> CruiseRoute | None:
        route = self.get(route_id)
        if route is None:
            return None
        route.last_used_at = utc_now()
        self._routes.remove(route)
        self._routes.insert(0, route)
        self._save()
        return route

    def delete(self, route_id: str) -> bool:
        route = self.get(route_id)
        if route is None:
            return False
        self._routes.remove(route)
        self._save()
        return True

    def _load(self) -> list[CruiseRoute]:
        raw, problem = read_json(self.path)
        if problem:
            # goal ids cannot be enumerated from the robot — they are typed by hand and
            # discovered by driving — so a lost route file is expensive to rebuild and must
            # never be mistaken for having saved nothing.
            self.load_problem = problem
        if not isinstance(raw, list):
            return []

        routes: list[CruiseRoute] = []
        for entry in raw:
            try:
                routes.append(CruiseRoute(**entry))
            except (ValidationError, TypeError):
                continue
        routes.sort(key=lambda route: route.last_used_at, reverse=True)
        return routes[: self.MAX_ROUTES]

    def _save(self) -> None:
        write_json(self.path, [route.model_dump(mode="json") for route in self._routes])
