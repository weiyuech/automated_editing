"""Meaning can be uncertain; clocks and the allowed speed range must stay exact."""

import numpy as np
import pytest

from automated_video_editing_backend.services.narration_alignment import (
    map_script,
    playback_plan,
    reliable_words,
    retime_words,
)
from automated_video_editing_backend.services.semantic import BgeOnnxEmbedder, MODEL_PATH


class Scores:
    def __init__(self, rows):
        self.rows = rows

    def similarity(self, left, right):
        return np.asarray(self.rows) if self.rows is not None else None


def windows():
    return [
        {"id": "A", "label": "新品区", "notes": ["工业机械臂"], "start": 0, "end": 10},
        {"id": "B", "label": "休息区", "notes": ["咖啡和座椅"], "start": 10, "end": 20},
    ]


def test_manual_rewrite_uses_semantics_but_keeps_unchanged_reference():
    references = [
        {"node_id": "A", "text": "展区陈列机械臂。"},
        {"node_id": "B", "text": "这里有咖啡。"},
    ]
    result, evidence = map_script(
        "可以在此了解工业机械臂。这里有咖啡。",
        references,
        windows(),
        Scores([[0.83, 0.31], [0.29, 0.85]]),
    )
    assert [r["node_id"] for r in result] == ["A", "B"]
    assert [r["method"] for r in result] == ["semantic", "reference"]
    assert evidence == "bge-small-zh-v1.5-int8"


def test_ambiguity_transition_and_backwards_text_are_not_forced():
    result, _ = map_script(
        "休息一下。回看展品。接着向前走。",
        [],
        windows(),
        Scores([[0.3, 0.85], [0.86, 0.3], [0.7, 0.69]]),
    )
    assert [r["node_id"] for r in result] == ["B", None, None]


def test_semantics_warns_but_never_overrides_explicit_binding():
    result, _ = map_script(
        "已确认的内容。",
        [{"node_id": "A", "text": "已确认的内容。"}],
        windows(),
        Scores([[0.3, 0.9]]),
    )
    assert result[0]["node_id"] == "A"
    assert result[0]["warning"]


def test_missing_model_keeps_reference_and_leaves_edits_unassigned():
    result, evidence = map_script(
        "修改后的内容。原文。", [{"node_id": "B", "text": "原文。"}], windows(), Scores(None)
    )
    assert evidence == "unavailable"
    assert [r["node_id"] for r in result] == [None, "B"]


@pytest.mark.parametrize("voice,rate,expected", [(11, 1, 10), (9, 0.9, 10), (8, 1, 8)])
def test_whole_audio_speed_bounds(voice, rate, expected):
    blocks, local = playback_plan(voice, 10, [], complete=False, rate=rate)
    assert not local
    assert blocks[-1]["end"] == pytest.approx(expected)
    assert 0.9 <= blocks[0]["rate"] <= 1.1


def test_excessive_speed_is_rejected_instead_of_cutting_words():
    with pytest.raises(ValueError, match="精简文案"):
        playback_plan(14, 10, [], complete=False)
    with pytest.raises(ValueError):
        playback_plan(11, 10, [], complete=False, auto_tempo=False)


def test_local_clocks_and_silence_preserve_every_word():
    checks = [
        {"actual_start": 0.1, "actual_end": 1, "planned_start": 0, "planned_end": 1.2},
        {"actual_start": 1.2, "actual_end": 1.8, "planned_start": 2, "planned_end": 4},
    ]
    blocks, local = playback_plan(2, 4, checks, complete=True)
    assert local
    assert blocks[0]["source_end"] == blocks[1]["source_start"] == 1.1
    assert blocks[1]["start"] == 2
    words = retime_words([{"text": "后句", "start_time": 1200, "end_time": 1800}], blocks)
    assert words == [{"text": "后句", "start_time": 2100, "end_time": 2700}]


def test_a_word_crossing_paragraph_boundary_disables_local_slicing():
    checks = [
        {"actual_start": 0, "actual_end": 1, "planned_start": 0, "planned_end": 1},
        {"actual_start": 0.8, "actual_end": 2, "planned_start": 1, "planned_end": 2},
    ]
    blocks, local = playback_plan(2, 2, checks, complete=True)
    assert not local and len(blocks) == 1


def test_only_complete_real_word_clocks_allow_local_processing():
    words = [{"text": "你好", "start_time": 0, "end_time": 900}]
    assert reliable_words("你好。", words, "exact", 1)
    assert not reliable_words("你好，欢迎。", words, "exact", 1)
    assert not reliable_words("你好", words, "estimated", 1)
    assert not reliable_words("你好", words, "exact", 0.5)


@pytest.mark.skipif(
    not MODEL_PATH.is_file(), reason="optional model not downloaded in source checkout"
)
def test_real_baai_distinguishes_points_and_leaves_generic_transition_unmapped():
    refs = [
        {"id": "A", "label": "新品展区", "notes": ["工业机械臂和协作机器人"]},
        {"id": "B", "label": "休息区", "notes": ["沙发座椅、咖啡和茶水"]},
        {"id": "C", "label": "企业历史展墙", "notes": ["公司发展历程和里程碑"]},
    ]
    text = "这里陈列着新款工业机械臂。大家可以在这里坐下喝杯咖啡。我们接着向前走。这里展示公司从创立至今的重要历程。"
    result, evidence = map_script(text, [], refs, BgeOnnxEmbedder())
    assert evidence != "unavailable"
    assert [r["node_id"] for r in result] == ["A", "B", None, "C"]
