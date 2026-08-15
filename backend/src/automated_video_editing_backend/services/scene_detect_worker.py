"""Shot boundaries, in a subprocess so a decoder hang cannot take the backend with it.

Run alone rather than imported because PySceneDetect opens the file with its own decoder, and
a malformed recording that wedges it should cost one process, not the app.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# A shot shorter than a cut is not a shot. Kept in step with MIN_SLOT_SECONDS in `slots`, and
# expressed in seconds here because the detector wants frames and only it knows the rate.
MIN_SHOT_SECONDS = 0.6
# How much weight edge differences carry against hue, saturation and luminance. The library
# defaults this to zero, which throws away the one signal that survives an evenly lit, low
# contrast interior — most of what a robot films indoors.
EDGE_WEIGHT = 0.5


def open_best(path: Path):
    """Open the video on a backend that can actually report timestamps.

    PySceneDetect's OpenCV backend asks the capture for a position in milliseconds and turns
    it into a timecode. On these recordings OpenCV answers NaN, the conversion raises, and the
    decode thread dies — on every file tried, including plain exports. The failure surfaced
    only as a warning, so scene detection silently fell back to a crude frame-differencer for
    the entire life of this project and PySceneDetect never once ran.

    PyAV reads timestamps from the container instead of asking a capture object, and does not
    have the problem. OpenCV stays as a fallback for a machine without PyAV, where the old
    behaviour — failure, then the frame-differencer — is what happens anyway.
    """
    from scenedetect import open_video
    from scenedetect.backends import AVAILABLE_BACKENDS

    if "pyav" in AVAILABLE_BACKENDS:
        try:
            return open_video(str(path), backend="pyav")
        except Exception:
            pass
    return open_video(str(path))


def detect(path: Path) -> list[dict[str, Any]]:
    from scenedetect import AdaptiveDetector, SceneManager, StatsManager
    from scenedetect.detectors import ContentDetector

    video = open_best(path)
    rate = float(video.frame_rate or 30.0)
    stats = StatsManager()
    manager = SceneManager(stats_manager=stats)
    # Adaptive rather than Content: its threshold is a rolling average of recent frame deltas
    # instead of a fixed number, which is what stops a moving camera reading as a cut. A robot
    # gliding between two points is exactly that case, and a fixed threshold either invents
    # cuts in the glide or misses the real ones between similar-looking places.
    detector = AdaptiveDetector(
        min_scene_len=max(1, int(MIN_SHOT_SECONDS * rate)),
        weights=ContentDetector.Components(
            delta_hue=1.0, delta_sat=1.0, delta_lum=1.0, delta_edges=EDGE_WEIGHT,
        ),
    )
    manager.add_detector(detector)
    manager.detect_scenes(video=video)

    metric_keys = detector.get_metrics()
    adaptive_key = next(key for key in metric_keys if key.startswith("adaptive_ratio"))
    scenes = []
    for index, (start, end) in enumerate(manager.get_scene_list()):
        metrics = stats.get_metrics(start, metric_keys)
        values = dict(zip(metric_keys, metrics))
        content = float(values.get("content_val") or 0.0)
        adaptive = float(values.get(adaptive_key) or 0.0)
        # Confidence is diagnostic, not a second cut threshold. The detector already decided
        # the boundary; normalising both its absolute and locally-adaptive evidence lets later
        # selection prefer strong edit points without re-decoding the video.
        confidence = 1.0 if index == 0 else min(
            1.0,
            max(content / max(1.0, detector.min_content_val), adaptive / max(1.0, detector.adaptive_threshold)) / 2.0,
        )
        scenes.append({
            "start": round(float(start.seconds), 3),
            "end": round(float(end.seconds), 3),
            "score": round(confidence, 4),
            "boundary_score": round(confidence, 4),
            "boundary_metrics": {
                key: round(float(value), 4) if value is not None else None
                for key, value in values.items()
            },
        })
    if scenes:
        return scenes
    return [{"start": 0.0, "end": round(float(video.duration.seconds), 3), "score": 1.0}]


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: scene_detect_worker <video-path>", file=sys.stderr)
        return 2
    print(json.dumps(detect(Path(sys.argv[1]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
