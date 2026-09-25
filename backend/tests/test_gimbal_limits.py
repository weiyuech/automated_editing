import pytest
from pydantic import ValidationError

from automated_video_editing_backend.core.models import CameraworkConfig, GimbalMoveRequest


@pytest.mark.parametrize("yaw,pitch,zoom", [(-91.2, -61.2, 0.99), (91.2, 16.2, 3.52),
                                            (-95, -65, 0.95), (95, 20, 3.55)])
def test_measured_start_preserves_outward_drift(yaw, pitch, zoom):
    command = GimbalMoveRequest(yaw_start=yaw, pitch_start=pitch, zoom_start=zoom, yaw_end=0)
    assert (command.yaw_start, command.pitch_start, command.zoom_start) == (yaw, pitch, zoom)
    assert (command.yaw_end, command.pitch_end, command.zoom_end) == (0, 0, 1)


@pytest.mark.parametrize("field,value", [
    ("yaw_start", -120.01), ("yaw_start", 120.01),
    ("pitch_start", -90.01), ("pitch_start", 45.01),
    ("zoom_start", -0.51), ("zoom_start", 5.01),
    ("yaw_end", -90.01), ("yaw_end", 90.01),
    ("pitch_end", -60.01), ("pitch_end", 15.01),
    ("zoom_end", 0.99), ("zoom_end", 3.51),
])
def test_grace_does_not_remove_start_bounds_or_expand_destinations(field, value):
    with pytest.raises(ValidationError):
        GimbalMoveRequest(**{"yaw_start": 0, "yaw_end": 0, field: value})


@pytest.mark.parametrize("field", ["yaw_start", "pitch_start", "zoom_start"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_start_is_never_sent(field, value):
    with pytest.raises(ValidationError):
        GimbalMoveRequest(**{"yaw_start": 0, "yaw_end": 0, field: value})


@pytest.mark.parametrize("bounds", [{"yaw_min": -95}, {"yaw_max": 95},
                                     {"pitch_min": -65}, {"pitch_max": 20},
                                     {"anchor_zoom": 0.95}, {"zoom_target": 3.55}])
def test_profile_still_rejects_targets_in_telemetry_grace_band(bounds):
    with pytest.raises(ValidationError):
        CameraworkConfig(**bounds)
