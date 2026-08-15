from __future__ import annotations

import json
from itertools import pairwise
from pathlib import Path

import pytest

from automated_video_editing_backend.core.models import AnalysisResult, EditJobRequest, MediaItem
from automated_video_editing_backend.services.capture import sidecar_path
from automated_video_editing_backend.services.semantic import MODEL_PATH, SemanticService
from automated_video_editing_backend.services.timeline import EditPlanner

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "semantic"


def _materialise_real_world_pair(tmp_path):
    video_path = tmp_path / "robot-REC_0042.mp4"
    video_path.write_bytes(b"simulated video container")
    sidecar_path(video_path).write_text(
        (FIXTURE_DIR / "complex_robot_recording.capture.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    voice_path = tmp_path / "factory-tour-voice.mp3"
    voice_path.write_bytes(b"simulated audio container")
    metadata_path = voice_path.with_suffix(".json")
    metadata_path.write_text(
        (FIXTURE_DIR / "complex_tts_metadata.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    video = MediaItem(path=str(video_path), kind="video")
    voice = MediaItem(
        path=str(voice_path),
        kind="audio",
        metadata={"metadata_path": str(metadata_path), "duration_ms": 27000},
    )
    return video, voice


def _real_world_scenes(media_id):
    return AnalysisResult(media_id=media_id, scenes=[
        {"start": 0.0, "end": 5.2, "kind": "transit", "label": "cs2#1", "quality": 0.75},
        {"start": 5.2, "end": 12.0, "kind": "dwell", "label": "cs2#1", "quality": 0.82},
        {"start": 12.0, "end": 22.1, "kind": "transit", "label": "cs2#2", "quality": 0.70},
        {"start": 22.1, "end": 31.0, "kind": "dwell", "label": "cs2#2", "quality": 0.90},
        {"start": 31.0, "end": 40.0, "kind": "failed", "label": "cs2#3", "quality": 0.35},
        {"start": 40.0, "end": 49.3, "kind": "transit", "label": "cs2#3", "quality": 0.72},
        {"start": 49.3, "end": 60.0, "kind": "dwell", "label": "cs2#3", "quality": 0.92},
        {"start": 60.0, "end": 70.4, "kind": "transit", "label": "cs2#4", "quality": 0.74},
        {"start": 70.4, "end": 95.0, "kind": "dwell", "label": "cs2#4", "quality": 0.88},
    ])


@pytest.mark.skipif(not MODEL_PATH.is_file(), reason="semantic model asset is not prepared")
def test_real_model_handles_noisy_notes_retry_paraphrases_and_backward_narration(tmp_path):
    video, voice = _materialise_real_world_pair(tmp_path)

    alignment = SemanticService().align(video, voice, 27.0)

    assert alignment.evidence == "bge-small-zh-v1.5-int8"
    assert [item.label for item in alignment.point_descriptions] == [
        "cs2#1", "cs2#2", "cs2#3", "cs2#4",
    ]
    # A later correction to point 2 is retained, while the camera/noise note before the first
    # explicit point marker is not guessed onto any location.
    point_two = alignment.point_descriptions[1].description
    assert "产品展示区" in point_two and "展厅" in point_two
    assert all("机器噪声" not in item.description for item in alignment.point_descriptions)

    assert [chapter.label for chapter in alignment.chapters] == [
        None, "cs2#2", "cs2#3", "cs2#4", None,
    ]
    # The final sentence really describes point 1, but using it after point 4 would make the
    # picture jump backwards. It must stay neutral rather than winning on text alone.
    assert "登记和咨询" in alignment.chapters[-1].text
    assert alignment.chapters[-1].label is None
    assert alignment.matched_seconds / alignment.narration_duration == pytest.approx(
        0.6019, abs=0.001
    )


@pytest.mark.skipif(not MODEL_PATH.is_file(), reason="semantic model asset is not prepared")
def test_real_semantic_schedule_keeps_requested_tail_and_source_chronology(tmp_path):
    video, voice = _materialise_real_world_pair(tmp_path)
    alignment = SemanticService().align(video, voice, 27.0)
    request = EditJobRequest(
        media_ids=[video.id],
        target_duration_seconds=38,
        pace="normal",
        point_scope=0.5,
        variant_seed=29,
        output_name="semantic-real-world.mp4",
    )

    timeline = EditPlanner().plan(
        request,
        [video],
        [_real_world_scenes(video.id)],
        None,
        voice,
        voiceover_duration=27.0,
        semantic_alignment=alignment,
    )

    assert sum(clip.duration for clip in timeline.clips) == pytest.approx(38.0, abs=0.05)
    diagnostics = timeline.planning_diagnostics["semantic_alignment"]
    assert diagnostics["applied"] is True
    assert diagnostics["fallback_reason"] == ""
    assert any("尾部无人声" in warning for warning in timeline.warnings)
    assert timeline.warnings.count("本条只用了 3/4 个点位") == 1

    for earlier, later in pairwise(timeline.clips):
        assert earlier.start + earlier.duration <= later.start + 1e-6

    # Neutral opening copy borrows the upcoming display point. The three confident chapters
    # then follow the route; the late request to revisit reception never brings point 1 back.
    narrated_labels = [
        clip.label for clip in timeline.clips if clip.timeline_start < 20.05
    ]
    assert narrated_labels[0] == "cs2#2"
    assert {"cs2#2", "cs2#3", "cs2#4"}.issubset(narrated_labels)
    assert "cs2#1" not in {clip.label for clip in timeline.clips}
    assert all(clip.footage != "failed" for clip in timeline.clips)


@pytest.mark.skipif(not MODEL_PATH.is_file(), reason="semantic model asset is not prepared")
def test_real_semantic_tail_reservation_is_stable_across_one_hundred_cut_seeds(tmp_path):
    video, voice = _materialise_real_world_pair(tmp_path)
    alignment = SemanticService().align(video, voice, 27.0)
    analysis = _real_world_scenes(video.id)

    for seed in range(100):
        request = EditJobRequest(
            media_ids=[video.id],
            target_duration_seconds=38,
            pace="normal",
            point_scope=0.5,
            variant_seed=seed,
            output_name=f"semantic-real-world-{seed}.mp4",
        )
        timeline = EditPlanner().plan(
            request,
            [video],
            [analysis],
            None,
            voice,
            voiceover_duration=27.0,
            semantic_alignment=alignment,
        )

        assert timeline.planning_diagnostics["semantic_alignment"]["applied"] is True, seed
        assert sum(clip.duration for clip in timeline.clips) == pytest.approx(
            38.0, abs=0.05
        ), seed
        assert all(
            earlier.start + earlier.duration <= later.start + 1e-6
            for earlier, later in pairwise(timeline.clips)
        ), seed


def test_real_fixture_is_valid_json_and_preserves_failed_retry_evidence():
    capture = json.loads(
        (FIXTURE_DIR / "complex_robot_recording.capture.json").read_text(encoding="utf-8")
    )
    tts = json.loads(
        (FIXTURE_DIR / "complex_tts_metadata.json").read_text(encoding="utf-8")
    )

    attempts = [item for item in capture["segments"] if item["goal_id"] == 3]
    assert [item["status"] for item in attempts] == ["failed", "arrived"]
    assert tts["duration_ms"] > max(word["end_time"] for word in tts["words"])
