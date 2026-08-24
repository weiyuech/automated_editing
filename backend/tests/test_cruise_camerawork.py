import collections
import random

from automated_video_editing_backend.services.cruise import CruiseService, _CW_PITCH, _CW_YAW, _clamp


def _service():
    # Only the pure helpers are exercised here, so we skip the full constructor.
    return CruiseService.__new__(CruiseService)


def test_camerawork_mode_weights_are_roughly_50_30_20():
    random.seed(1)
    service = _service()
    counts = collections.Counter(service._pick_camerawork_mode() for _ in range(20000))
    assert 46 <= counts["wander"] / 200 <= 54
    assert 26 <= counts["pingpong"] / 200 <= 34
    assert 16 <= counts["holds"] / 200 <= 24


def test_camerawork_poses_stay_inside_the_ui_range():
    service = _service()
    for _ in range(500):
        yaw, pitch = service._camerawork_pose()
        assert _CW_YAW[0] <= yaw <= _CW_YAW[1]
        assert _CW_PITCH[0] <= pitch <= _CW_PITCH[1]


def test_clamp_keeps_a_start_pose_within_command_range():
    assert _clamp(-135.0, *_CW_YAW) == -90.0   # hardware can reach -135; command range caps at -90
    assert _clamp(135.0, *_CW_YAW) == 90.0
    assert _clamp(25.0, *_CW_PITCH) == 15.0
