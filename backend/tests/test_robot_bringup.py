from __future__ import annotations

import importlib.util
from pathlib import Path

from automated_video_editing_backend.core.models import (
    CruiseRun,
    CruiseSegment,
    TimelineMarker,
)


def _load_bringup_module():
    script = Path(__file__).resolve().parents[2] / "scripts" / "robot_bringup.py"
    spec = importlib.util.spec_from_file_location("robot_bringup", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cruise_summary_uses_only_current_run_contract(capsys):
    module = _load_bringup_module()
    run = CruiseRun(
        status="succeeded",
        segments=[
            CruiseSegment(
                index=0,
                path_name="path-1",
                goal_id=2,
                status="arrived",
                arrived_at_seconds=3.0,
                dwell_seconds=7.5,
            )
        ],
        markers=[TimelineMarker(timestamp=3.0, label="path-1#2")],
        media_url="https://robot/video.mp4",
        media_local_path="/media/video.mp4",
        warnings=["example warning"],
    )

    module._print_cruise_summary(run)

    output = capsys.readouterr().out
    assert "run succeeded" in output
    assert "#2" in output
    assert "path-1#2" in output
    assert "https://robot/video.mp4" in output
    assert "/media/video.mp4" in output
    assert "example warning" in output
    assert "usable spans" not in output
    assert "discard spans" not in output
