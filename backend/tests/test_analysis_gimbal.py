import json
from pathlib import Path

from automated_video_editing_backend.services.analysis import (
    _gimbal_rate,
    _lift_static_windows_with_gimbal,
)


def _write_track(video: Path, samples):
    Path(str(video) + ".gimbal.json").write_text(json.dumps({"samples": samples}), encoding="utf-8")


def test_visual_static_is_not_true_static_when_gimbal_is_moving(tmp_path):
    video = tmp_path / "v.mp4"
    _write_track(video, [[0, 0, 0], [1, 3, 0], [2, 6, 0], [3, 9, 0], [4, 12, 0]])  # ~3 deg/s pan
    scenes = [{"start": 0, "end": 4, "quality_profile": [{"start": 0, "end": 4, "motion": 0.02}]}]
    _lift_static_windows_with_gimbal(video, scenes)
    assert scenes[0]["quality_profile"][0]["motion"] == 0.35
    assert scenes[0]["motion"] == 0.35


def test_no_sidecar_leaves_analysis_untouched(tmp_path):
    video = tmp_path / "v.mp4"
    scenes = [{"start": 0, "end": 4, "quality_profile": [{"start": 0, "end": 4, "motion": 0.02}]}]
    _lift_static_windows_with_gimbal(video, scenes)
    assert scenes[0]["quality_profile"][0]["motion"] == 0.02


def test_static_window_with_a_still_gimbal_is_not_promoted(tmp_path):
    video = tmp_path / "v.mp4"
    _write_track(video, [[0, 10, 5], [1, 10, 5], [2, 10, 5], [3, 10, 5]])  # gimbal held still
    scenes = [{"start": 0, "end": 3, "quality_profile": [{"start": 0, "end": 3, "motion": 0.02}]}]
    _lift_static_windows_with_gimbal(video, scenes)
    assert scenes[0]["quality_profile"][0]["motion"] == 0.02
    assert "motion" not in scenes[0]


def test_still_gimbal_does_not_erase_real_visual_motion(tmp_path):
    video = tmp_path / "v.mp4"
    _write_track(video, [[0, 10, 5], [1, 10, 5], [2, 10, 5], [3, 10, 5]])  # gimbal held still
    scenes = [{"start": 0, "end": 3, "quality_profile": [{"start": 0, "end": 3, "motion": 0.9}]}]
    _lift_static_windows_with_gimbal(video, scenes)
    assert scenes[0]["quality_profile"][0]["motion"] == 0.9
    assert "motion" not in scenes[0]


def test_gimbal_rate_is_travel_per_second():
    assert _gimbal_rate([(0, 0, 0), (1, 3, 0), (2, 6, 0)], 0, 2) == 3.0
