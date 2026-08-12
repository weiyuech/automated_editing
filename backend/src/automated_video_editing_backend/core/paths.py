from __future__ import annotations

import os
from pathlib import Path


class RootPathError(ValueError):
    pass


def app_root() -> Path:
    env_root = os.environ.get("APP_ROOT")
    if env_root:
        return Path(env_root).resolve()
    return Path(__file__).resolve().parents[4]


APP_ROOT = app_root()
GENERATED_DIRS = {
    "data": APP_ROOT / "data",
    "cache": APP_ROOT / ".cache",
    "logs": APP_ROOT / "logs",
    "exports": APP_ROOT / "exports",
    "previews": APP_ROOT / "previews",
}


def ensure_generated_dirs() -> None:
    for path in GENERATED_DIRS.values():
        path.mkdir(parents=True, exist_ok=True)


def ensure_inside_root(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == APP_ROOT or APP_ROOT in resolved.parents:
        return resolved
    raise RootPathError(f"Refusing path outside app root: {resolved}")


def generated_path(area: str, *parts: str) -> Path:
    if area not in GENERATED_DIRS:
        raise RootPathError(f"Unknown generated area: {area}")
    candidate = GENERATED_DIRS[area].joinpath(*parts)
    return ensure_inside_root(candidate)
