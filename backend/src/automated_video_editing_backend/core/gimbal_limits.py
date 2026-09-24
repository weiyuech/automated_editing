"""Command targets and telemetry grace shared by validation and arrival checks."""

YAW_MIN = -90.0
YAW_MAX = 90.0
PITCH_MIN = -60.0
PITCH_MAX = 15.0
ZOOM_MIN = 1.0
ZOOM_MAX = 3.5

POSE_TOLERANCE_DEG = 5.0
POSE_STABLE_SAMPLES = 2
# The tolerance is a final framing allowance, not proof that a sweep has stopped.
POSE_SETTLED_DELTA_DEG = 1.0
ZOOM_TOLERANCE = 0.05
