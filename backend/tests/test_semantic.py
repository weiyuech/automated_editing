from __future__ import annotations

import json

import numpy as np
import pytest

from automated_video_editing_backend.core.models import (
    AnalysisResult,
    EditJobRequest,
    MediaItem,
    TTSGenerateRequest,
)
from automated_video_editing_backend.services.capture import sidecar_path
from automated_video_editing_backend.services.editorial import score_timeline
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.semantic import (
    MODEL_PATH,
    BgeOnnxEmbedder,
    SemanticAlignment,
    SemanticChapter,
    SemanticService,
    narration_units,
    parse_point_descriptions,
)
from automated_video_editing_backend.services.timeline import EditPlanner


class MeaningEmbedder:
    evidence = "test-embedding"

    def similarity(self, left, right):
        def subject(text):
            if "产品" in text or "展区" in text:
                return "showcase"
            if "仓库" in text or "货物" in text or "储存" in text:
                return "warehouse"
            return "other"

        return np.asarray([
            [0.84 if subject(a) == subject(b) != "other" else 0.32 for b in right]
            for a in left
        ])


def _write_semantic_pair(tmp_path):
    video_path = tmp_path / "cruise.mp4"
    video_path.write_bytes(b"video")
    sidecar_path(video_path).write_text(json.dumps({
        "notes": ["点位1：产品展示区；点位2：仓库"],
        "segments": [
            {"path_name": "p", "goal_id": 1, "status": "arrived"},
            {"path_name": "p", "goal_id": 2, "status": "arrived"},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    text = "这里展示最新产品。这里负责储存货物。"
    words = [
        {"word": char, "start_time": index * 180, "end_time": index * 180 + 160}
        for index, char in enumerate(text)
    ]
    voice_path = tmp_path / "voice.mp3"
    voice_path.write_bytes(b"audio")
    metadata_path = voice_path.with_suffix(".json")
    duration = (len(text) * 180 + 160) / 1000
    metadata_path.write_text(json.dumps({
        "text": text, "duration_ms": int(duration * 1000), "words": words,
    }, ensure_ascii=False), encoding="utf-8")
    video = MediaItem(path=str(video_path), kind="video")
    voice = MediaItem(
        path=str(voice_path), kind="audio",
        metadata={"metadata_path": str(metadata_path), "duration_ms": int(duration * 1000)},
    )
    return video, voice, duration


def test_one_recording_note_maps_explicit_ordinals_to_robot_labels():
    points = parse_point_descriptions(
        ["点位一：产品展示区；第四个点：成品仓库"],
        ["route#1", "route#2", "route#3", "route#4"],
    )

    assert [(item.label, item.description) for item in points] == [
        ("route#1", "产品展示区"),
        ("route#4", "成品仓库"),
    ]


def test_english_narration_without_word_timing_splits_by_sentence_not_space():
    units = narration_units(
        {"text": "Welcome to the main showroom. Finished goods wait here!"},
        10.0,
    )

    assert [unit.text for unit in units] == [
        "Welcome to the main showroom.",
        "Finished goods wait here!",
    ]
    assert units[0].start == 0
    assert units[-1].end == 10


def test_english_word_timing_restores_missing_spaces_between_provider_tokens():
    units = narration_units({"words": [
        {"word": "Welcome", "start_time": 0, "end_time": 300},
        {"word": "to", "start_time": 320, "end_time": 450},
        {"word": "our", "start_time": 470, "end_time": 620},
        {"word": "showroom.", "start_time": 640, "end_time": 1000},
    ]}, 1.0)

    assert [unit.text for unit in units] == ["Welcome to our showroom."]


def test_semantic_alignment_uses_tts_time_and_never_reverses_points(tmp_path):
    video, voice, duration = _write_semantic_pair(tmp_path)

    alignment = SemanticService(MeaningEmbedder()).align(video, voice, duration)

    assert alignment.active
    assert alignment.evidence == "test-embedding"
    assert [chapter.label for chapter in alignment.chapters] == ["p#1", "p#2"]
    assert alignment.chapters[0].start == 0
    assert alignment.chapters[-1].end == pytest.approx(duration)
    assert alignment.matched_seconds == pytest.approx(duration)


def test_notes_are_not_a_tts_request_option_anymore():
    assert "include_notes" not in TTSGenerateRequest.model_fields


@pytest.mark.asyncio
async def test_voiceover_drafter_receives_only_operator_speech_text(monkeypatch):
    class Settings:
        def llm_config(self):
            return {"enabled": True, "api_key": "test", "model": "test"}

    captured = {}

    async def fake_chat(_cfg, **kwargs):
        captured.update(kwargs)
        return "整理后的口播"

    service = LLMService(Settings())
    monkeypatch.setattr(service, "_chat", fake_chat)

    result = await service.draft_voiceover("只介绍新品")

    assert result == "整理后的口播"
    assert captured["user"] == "文案或要求：\n只介绍新品"
    assert "备注" not in captured["user"]


def test_no_semantic_evidence_preserves_the_existing_picture_plan_exactly():
    video = MediaItem(path="/tmp/no-semantic-evidence.mp4", kind="video")
    analysis = AnalysisResult(media_id=video.id, scenes=[
        {"start": index * 5, "end": (index + 1) * 5, "quality": 0.5 + index / 20}
        for index in range(10)
    ])

    def request():
        return EditJobRequest(
            media_ids=[video.id], target_duration_seconds=20, pace="normal", variant_seed=17,
        )

    base = EditPlanner().plan(request(), [video], [analysis], None)
    inert = EditPlanner().plan(
        request(), [video], [analysis], None,
        semantic_alignment=SemanticAlignment(reason="no point descriptions"),
    )

    assert [(clip.start, clip.duration) for clip in inert.clips] == [
        (clip.start, clip.duration) for clip in base.clips
    ]
    assert inert.warnings == base.warnings


def test_bundled_bge_separates_a_paraphrase_from_an_unrelated_sentence():
    if not MODEL_PATH.is_file():
        pytest.skip("run scripts/prepare_assets.py --semantic-only")
    scores = BgeOnnxEmbedder().similarity(
        ["产品展示区陈列最新产品"],
        ["这里展示了公司最新的产品系列", "今天天气很好"],
    )

    assert scores is not None
    assert scores[0, 0] > scores[0, 1] + 0.2


def test_three_second_voiceover_still_produces_a_thirty_second_edit():
    video = MediaItem(path="/tmp/semantic-cruise.mp4", kind="video")
    request = EditJobRequest(
        media_ids=[video.id], target_duration_seconds=30, pace="normal", point_scope="all",
    )
    analysis = AnalysisResult(media_id=video.id, scenes=[
        {"start": 0, "end": 20, "kind": "dwell", "label": "p#1"},
        {"start": 20, "end": 60, "kind": "transit", "label": "p#2"},
    ])
    alignment = SemanticAlignment(
        chapters=(SemanticChapter(0, 3, "介绍产品", "p#1", 0.8),),
        evidence="test",
        narration_duration=3,
    )

    timeline = EditPlanner().plan(
        request, [video], [analysis], None, voiceover_duration=3,
        semantic_alignment=alignment,
    )

    assert sum(clip.duration for clip in timeline.clips) == pytest.approx(30, abs=0.05)
    narrated = [clip for clip in timeline.clips if clip.timeline_start < 3]
    assert narrated and {clip.label for clip in narrated} == {"p#1"}
    assert timeline.planning_diagnostics["semantic_alignment"]["applied"] is True


def test_unfulfillable_semantic_chapter_falls_back_to_the_complete_base_edit():
    video = MediaItem(path="/tmp/short-described-point.mp4", kind="video")
    request = EditJobRequest(
        media_ids=[video.id], target_duration_seconds=20, pace="normal", point_scope="all",
    )
    analysis = AnalysisResult(media_id=video.id, scenes=[
        {"start": 0, "end": 3, "kind": "dwell", "label": "p#1"},
        {"start": 3, "end": 40, "kind": "transit", "label": "p#2"},
    ])
    alignment = SemanticAlignment(
        chapters=(SemanticChapter(0, 10, "持续介绍产品", "p#1", 0.8),),
        evidence="test",
        narration_duration=10,
    )

    timeline = EditPlanner().plan(
        request, [video], [analysis], None, voiceover_duration=10,
        semantic_alignment=alignment,
    )

    assert sum(clip.duration for clip in timeline.clips) == pytest.approx(20, abs=0.05)
    diagnostics = timeline.planning_diagnostics["semantic_alignment"]
    assert diagnostics["applied"] is False
    assert "holds" in diagnostics["fallback_reason"]
    for earlier, later in zip(timeline.clips, timeline.clips[1:]):
        assert earlier.start + earlier.duration <= later.start + 1e-6


def test_long_voiceover_widens_scope_before_considering_a_picture_loop():
    video = MediaItem(path="/tmp/long-voice-cruise.mp4", kind="video")
    scenes = [
        {"start": index * 20, "end": (index + 1) * 20, "kind": "dwell", "label": f"p{index}"}
        for index in range(5)
    ]
    request = EditJobRequest(
        media_ids=[video.id], target_duration_seconds=30, voiceover_media_id="voice",
        pace="normal", point_scope=0.5, variant_seed=3,
    )

    analysis = AnalysisResult(media_id=video.id, scenes=scenes)
    timeline = EditPlanner().plan(
        request, [video], [analysis], None,
        voiceover_duration=45,
    )

    assert sum(clip.duration for clip in timeline.clips) == pytest.approx(45, abs=0.05)
    assert not any("画面循环" in warning for warning in timeline.warnings)
    assert any("成片延长至 45 秒" in warning for warning in timeline.warnings)
    _score, components = score_timeline(timeline, [analysis], "immersive")
    assert timeline.planning_diagnostics["effective_duration_seconds"] == 45
    assert components["duration_accuracy"] == 1
