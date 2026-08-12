from __future__ import annotations

from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def assert_inside_root(path: str | Path) -> Path:
    resolved = Path(path).resolve()
    if resolved == APP_ROOT or APP_ROOT in resolved.parents:
        return resolved
    raise SystemExit(f"Refusing to write outside project root: {resolved}")


def ensure_runtime_dirs() -> None:
    for name in ["data", ".cache", "logs", "exports", "previews"]:
        target = assert_inside_root(APP_ROOT / name)
        target.mkdir(parents=True, exist_ok=True)
