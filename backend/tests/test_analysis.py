import json
from pathlib import Path

import cv2
import numpy as np

from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.analysis import AnalysisService


def test_pyscenedetect_worker_handles_basic_video():
    video_path = generated_path("cache", "test-scenes.avi")
    writer = cv2.VideoWriter(
        str(video_path),
        cv2.VideoWriter_fourcc(*"MJPG"),
        10,
        (64, 64),
    )
    for _ in range(20):
        writer.write(np.full((64, 64, 3), 20, dtype=np.uint8))
    for _ in range(20):
        writer.write(np.full((64, 64, 3), 235, dtype=np.uint8))
    writer.release()

    warnings = []
    scenes = AnalysisService().detect_scenes(Path(video_path), warnings)

    assert scenes[0]["start"] == 0.0
    assert scenes[-1]["end"] > 1.0


def test_the_recording_itself_is_read_when_the_container_can_be(monkeypatch):
    """The proxy is a full transcode of every source, and it existed only because the OpenCV
    backend could not get a timestamp out of these files. PyAV can, so the transcode is
    skipped — the point of the fix is that it stops happening, not that it happens faster."""
    from automated_video_editing_backend.services import analysis as module

    module._has_pyav.cache_clear()
    monkeypatch.setattr(module, "_has_pyav", lambda: True)
    service = AnalysisService()
    source = Path("/tmp/source.mp4")
    monkeypatch.setattr(service, "_timestamps_need_repair", lambda path: True)
    monkeypatch.setattr(service, "_create_analysis_proxy", lambda path: Path("/tmp/proxy.mp4"))

    warnings = []
    assert service._scene_input(source, warnings) == source
    assert warnings == []


def test_the_proxy_is_still_there_for_a_machine_without_pyav(monkeypatch):
    """Without PyAV both scene detection and scoring use one decoder-compatible proxy."""
    from automated_video_editing_backend.services import analysis as module

    monkeypatch.setattr(module, "_has_pyav", lambda: False)
    service = AnalysisService()
    source = Path("/tmp/source.mp4")
    proxy = Path("/tmp/proxy.mp4")
    monkeypatch.setattr(service, "_create_analysis_proxy", lambda path: proxy)

    warnings = []
    assert service._scene_input(source, warnings) == proxy
    assert warnings == ["Using decoder-compatible analysis proxy: proxy.mp4"]


def test_timestamp_value_rejects_missing_and_nan():
    service = AnalysisService()

    assert service._timestamp_value("N/A") is None
    assert service._timestamp_value("nan") is None
    assert service._timestamp_value("0.040000") == 0.04


def test_rate_value_parses_ffprobe_fraction():
    service = AnalysisService()

    assert service._rate_value("25/1") == 25.0
    assert service._rate_value("0/0") is None


def test_scene_detection_is_read_once_per_file_not_once_per_job(tmp_path):
    """A hundred outputs of one recording analysed that recording a hundred times — a
    subprocess launch and a full decode each, some seventeen minutes of re-deriving an answer
    that cannot have changed."""
    from automated_video_editing_backend.services.analysis import AnalysisService

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not really a video")
    service = AnalysisService()
    calls = []

    def counted(path, warnings):
        calls.append(path)
        return [{"start": 0.0, "end": 6.0, "score": 1.0}]

    service._detect_scenes_with_pyscenedetect = counted

    first = service.detect_scenes(video, [])
    for _ in range(9):
        service.detect_scenes(video, [])

    assert len(calls) == 1
    # The warnings a caller sees do not depend on whether it happened to be first.
    cold, warm = [], []
    AnalysisService().detect_scenes(video, cold)
    service.detect_scenes(video, warm)
    assert warm and first


def test_a_rewritten_file_is_analysed_again(tmp_path):
    from automated_video_editing_backend.services.analysis import AnalysisService

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"first")
    service = AnalysisService()
    calls = []
    service._detect_scenes_with_pyscenedetect = lambda path, warnings: (
        calls.append(1) or [{"start": 0.0, "end": 6.0, "score": 1.0}]
    )

    service.detect_scenes(video, [])
    video.write_bytes(b"second content, different size")
    service.detect_scenes(video, [])

    assert len(calls) == 2


def test_cruise_spans_arriving_late_invalidate_the_cached_analysis(tmp_path):
    """A cruise writes its spans after the recording already exists, so a file analysed in
    between has no points and must be read again once they land."""
    from automated_video_editing_backend.services.analysis import AnalysisService
    from automated_video_editing_backend.services.capture import sidecar_path

    video = tmp_path / "cruise.mp4"
    video.write_bytes(b"x")
    service = AnalysisService()
    service._detect_scenes_with_pyscenedetect = lambda path, warnings: [
        {"start": 0.0, "end": 40.0, "score": 1.0}
    ]

    before = service.detect_scenes(video, [])
    sidecar_path(video).write_text(json.dumps({"segments": [
        {"index": 0, "path_name": "path1", "goal_id": 1, "status": "arrived",
         "transit_start_seconds": 0.0, "arrived_at_seconds": 12.0, "departed_at_seconds": 20.0},
    ]}), encoding="utf-8")
    after = service.detect_scenes(video, [])

    assert all("kind" not in scene for scene in before)
    assert any(scene.get("kind") == "dwell" for scene in after)


def test_long_scenes_receive_local_quality_profiles(monkeypatch, tmp_path):
    """Candidates using different parts of one continuous take need different evidence;
    scoring one three-frame sample for the entire take makes portfolio ranking cosmetic."""
    from automated_video_editing_backend.services import analysis as module
    from automated_video_editing_backend.services.scoring import ShotScore

    seen = []

    def fake_scores(_path, spans):
        seen.extend(spans)
        return [
            ShotScore(
                quality=0.3 + index * 0.3,
                sharpness=0.5,
                exposure=0.8,
                motion=0.4,
                steadiness=0.7,
                colour=(50.0 + index, 128.0, 128.0),
                fingerprint=index,
            )
            for index, _span in enumerate(spans)
        ], ""

    monkeypatch.setattr(module, "score_shots", fake_scores)
    scenes = [{"start": 0.0, "end": 30.0, "boundary_score": 0.9}]
    AnalysisService()._score_scenes(tmp_path / "video.mp4", scenes, [])

    assert seen == [(0.0, 12.0), (12.0, 24.0), (24.0, 30.0)]
    assert [item["quality"] for item in scenes[0]["quality_profile"]] == [0.3, 0.6, 0.9]
    assert 0.3 < scenes[0]["quality"] < 0.9


def test_point_merge_preserves_only_real_visual_boundary_metrics():
    service = AnalysisService()
    visual = [
        {"start": 0.0, "end": 20.0, "boundary_score": 1.0, "boundary_metrics": {"content_val": 0}},
        {"start": 20.0, "end": 40.0, "boundary_score": 0.8, "boundary_metrics": {"content_val": 30}},
    ]
    segments = [{
        "path_name": "path", "goal_id": 1, "status": "arrived",
        "transit_start_seconds": 0.0, "arrived_at_seconds": 10.0,
        "departed_at_seconds": 30.0,
    }]

    merged = service._merge_cruise_segments(visual, segments, [])
    by_start = {scene["start"]: scene for scene in merged}

    assert by_start[20.0]["boundary_score"] == 0.8
    assert by_start[20.0]["from_scene_detector"] is True
    assert "boundary_score" not in by_start[10.0]
