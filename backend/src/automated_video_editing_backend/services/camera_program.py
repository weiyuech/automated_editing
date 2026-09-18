"""Pure ordered camera programs. Coordinates follow the robot protocol, not screen axes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class Bounds(Protocol):
    yaw_min: int
    yaw_max: int
    pitch_min: int
    pitch_max: int
    point_mode: int


@dataclass(frozen=True)
class CameraPiece:
    id: str
    label: str
    poses: tuple[tuple[float, float], ...]


def camera_program(config: Bounds, selected: list[str] | None = None) -> list[CameraPiece]:
    origin = (0, 0)
    left, right = (config.yaw_max, 0), (config.yaw_min, 0)
    up, down = (0, config.pitch_min), (0, config.pitch_max)
    pieces = [
        CameraPiece("origin-left", "原点 → 左", (origin, left)),
        CameraPiece("left-right", "左 → 右", (left, right)),
        CameraPiece("right-origin", "右 → 原点", (right, origin)),
        CameraPiece("origin-up", "原点 → 上", (origin, up)),
        CameraPiece("up-down", "上 → 下", (up, down)),
        CameraPiece("down-origin", "下 → 原点", (down, origin)),
    ]
    if config.point_mode == 8:
        for key, label, corner in (
            ("upper-left", "左上", (config.yaw_max, config.pitch_min)),
            ("upper-right", "右上", (config.yaw_min, config.pitch_min)),
            ("lower-right", "右下", (config.yaw_min, config.pitch_max)),
            ("lower-left", "左下", (config.yaw_max, config.pitch_max)),
        ):
            pieces.append(CameraPiece(key, f"原点 → {label} → 原点", (origin, corner, origin)))
    if selected is None:
        return pieces
    if len(selected) != len(set(selected)) or set(selected) - {piece.id for piece in pieces}:
        raise ValueError("镜头选择不属于当前 4 / 8 点模式，请重新选择")
    return [piece for piece in pieces if piece.id in selected]
