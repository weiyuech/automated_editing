from automated_video_editing_backend.core.models import AnalysisResult, EditTimeline, TimelineClip
from automated_video_editing_backend.services.editorial import (
    score_timeline,
    _clip_static_fraction,
    _timeline_static_fraction,
)


def _clip(media_id="m", start=0.0, dur=10.0):
    return TimelineClip(
        media_id=media_id, source_path=f"/x/{media_id}.mp4",
        start=start, duration=dur, timeline_start=0.0,
    )


def _analysis(motion):
    profile = [
        {"start": s, "end": s + 2, "quality": 0.7, "motion": motion, "steadiness": 0.9}
        for s in range(0, 10, 2)
    ]
    scene = {"start": 0.0, "end": 10.0, "quality": 0.7, "motion": motion,
             "steadiness": 0.9, "quality_profile": profile}
    return AnalysisResult(media_id="m", scenes=[scene])


def _timeline():
    return EditTimeline(title="t", clips=[_clip()], output_path="/x/out.mp4", target_duration_seconds=10.0)


def test_static_timeline_scores_below_lively_via_layered_multiplier():
    lively_score, lively = score_timeline(_timeline(), [_analysis(0.6)], "dynamic")
    static_score, static = score_timeline(_timeline(), [_analysis(0.02)], "dynamic")
    assert lively["static_fraction"] == 0.0 and static["static_fraction"] == 1.0
    assert lively["liveliness"] == 1.0 and static["liveliness"] < 1.0
    assert static_score < lively_score


def test_static_fraction_counts_the_frozen_part_not_the_best_window():
    # Half the clip is dead, half is lively -> 0.5. The old max()-over-windows hid the dead half.
    profile = [
        {"start": 0, "end": 2, "motion": 0.02, "quality": 0.7},
        {"start": 2, "end": 4, "motion": 0.6, "quality": 0.7},
    ]
    scenes = [{"start": 0, "end": 4, "quality": 0.7, "motion": 0.3, "quality_profile": profile}]
    assert _clip_static_fraction(_clip(start=0, dur=4), scenes) == 0.5


def test_footage_without_motion_evidence_is_unpenalised():
    scenes = [{"start": 0, "end": 10, "quality": 0.7}]  # no motion, no profile
    assert _clip_static_fraction(_clip(), scenes) is None
    assert _timeline_static_fraction(_timeline(), {"m": scenes}) == 0.0
