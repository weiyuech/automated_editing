from copy import deepcopy

import pytest

from automated_video_editing_backend.services.recording_segments import (
    apply_motion_trim, build_timeline,
)


def _dwell(children):
    return [
        {
            "id": "dwell-1",
            "kind": "dwell",
            "label": "A",
            "start": 0,
            "end": 10,
            "children": children,
        }
    ]


def _shot(shot_id="shot-1", start=0, end=10, kind="shot"):
    return {
        "id": shot_id,
        "kind": kind,
        "label": "s",
        "start": start,
        "end": end,
        "complete": True,
        "boundary_source": "command_to_pose_feedback",
        "order": 0,
    }


def test_trim_moves_still_head_and_tail_into_preparation():
    segments = _dwell([_shot()])
    samples = [
        (0.0, 0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
        (2.0, 0.0, 0.0, 1.0),
        (3.0, 10.0, 0.0, 1.0),
        (4.0, 20.0, 0.0, 1.0),
        (5.0, 30.0, 0.0, 1.0),
        (6.0, 30.0, 0.0, 1.0),
        (7.0, 30.0, 0.0, 1.0),
        (8.0, 30.0, 0.0, 1.0),
        (9.0, 30.0, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 1.0),
        ("shot", 1.0, 6.0),
        ("preparation", 6.0, 10.0),
    ]
    assert children[0]["id"] == "shot-1:prep-head"
    assert children[1]["id"] == "shot-1"
    assert children[2]["id"] == "shot-1:prep-tail"
    assert children[0]["label"] == "镜头前等待"
    assert children[2]["label"] == "镜头后等待"
    assert children[2]["source_shot_id"] == "shot-1"


def test_zoom_only_motion_is_kept_when_yaw_and_pitch_are_static():
    segments = _dwell([_shot(kind="zoom")])
    samples = [
        (0.0, 0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
        (2.0, 0.0, 0.0, 1.0),
        (3.0, 0.0, 0.0, 1.5),
        (4.0, 0.0, 0.0, 2.0),
        (5.0, 0.0, 0.0, 2.0),
        (6.0, 0.0, 0.0, 2.0),
        (7.0, 0.0, 0.0, 2.0),
        (8.0, 0.0, 0.0, 2.0),
        (9.0, 0.0, 0.0, 2.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 1.0),
        ("zoom", 1.0, 5.0),
        ("preparation", 5.0, 10.0),
    ]


def test_no_motion_leaves_the_shot_untouched():
    segments = _dwell([_shot()])
    samples = [
        (0.0, 5.0, 0.0, 1.0),
        (1.0, 5.0, 0.0, 1.0),
        (2.0, 5.0, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert len(children) == 1
    assert children[0]["id"] == "shot-1"
    assert children[0]["start"] == 0
    assert children[0]["end"] == 10


def test_repositioning_and_trimmed_wait_keep_distinct_ownership():
    segments = _dwell(
        [
            {
                "id": "gap-0",
                "kind": "preparation",
                "label": "镜头准备",
                "start": 0,
                "end": 2,
                "complete": True,
                "boundary_source": "application_estimate",
                "order": 0,
            },
            _shot(start=2, end=8),
        ]
    )
    samples = [
        (2.0, 5.0, 0.0, 1.0),
        (2.5, 5.0, 0.0, 1.0),
        (3.0, 5.0, 0.0, 1.0),
        (3.5, 5.0, 0.0, 1.0),
        (4.5, 15.0, 0.0, 1.0),
        (5.5, 25.0, 0.0, 1.0),
        (6.5, 25.0, 0.0, 1.0),
        (7.5, 25.0, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 2),
        ("preparation", 2, 3.0),
        ("shot", 3.0, 6.5),
        ("preparation", 6.5, 8.0),
    ]
    assert children[0]["id"] == "gap-0"


@pytest.mark.parametrize("kind,shot_id", [("zoom", "s"), ("shot", "visit-0:zoom-return")])
def test_endpoint_only_zoom_retains_full_shot_even_with_angle_jitter(kind, shot_id):
    segments = _dwell([_shot(shot_id=shot_id, kind=kind)])
    samples = [(i / 4, (i % 3) * 0.8, 0, 2 if i < 20 else 1) for i in range(40)]
    apply_motion_trim(segments, samples)
    assert len(segments[0]["children"]) == 1
    shot = segments[0]["children"][0]
    assert (shot["start"], shot["end"]) == (0, 10)
    assert shot["trim_status"] == "retained_uncertain"


def test_slow_motion_below_per_sample_threshold_is_not_reclassified_as_waiting():
    segments = _dwell([_shot("visit-0:right-origin")])
    samples = [(i / 4, 8 - i * 0.2, 0, 1) for i in range(40)]
    apply_motion_trim(segments, samples)
    assert [(x["kind"], x["start"], x["end"]) for x in segments[0]["children"]] == [
        ("shot", 0, 10),
    ]


def test_intended_return_shot_and_internal_pause_remain_one_shot():
    segments = _dwell([_shot("visit-0:right-origin")])
    samples = [(i, yaw, 0, 1) for i, yaw in enumerate([30, 30, 30, 20, 20, 20, 10, 0, 0, 0])]
    apply_motion_trim(segments, samples)
    shots = [x for x in segments[0]["children"] if x["kind"] == "shot"]
    assert len(shots) == 1
    assert shots[0]["id"] == "visit-0:right-origin"
    assert shots[0]["start"] < 3 < 6 < shots[0]["end"]


def test_overshoot_hold_and_settle_back_become_trimmed_wait():
    """A sweep that stops, holds, then returns must end where the sweep ended."""
    segments = _dwell([_shot("visit-0:origin-left", end=14)])
    samples = [
        (0.0, 0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
        (2.0, 0.0, 0.0, 1.0),
        (3.0, 20.0, 0.0, 1.0),
        (4.0, 60.0, 0.0, 1.0),
        (5.0, 90.0, 0.0, 1.0),
        (6.0, 112.0, 0.0, 1.0),
        (7.0, 112.0, 0.0, 1.0),
        (8.0, 112.0, 0.0, 1.0),
        (9.0, 112.0, 0.0, 1.0),
        (10.0, 112.0, 0.0, 1.0),
        (11.0, 90.0, 0.0, 1.0),
        (12.0, 90.0, 0.0, 1.0),
        (13.0, 90.0, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 1.0),
        ("shot", 1.0, 7.0),
        ("preparation", 7.0, 14),
    ]
    assert children[2]["id"] == "visit-0:origin-left:prep-tail"
    assert children[2]["source_shot_id"] == "visit-0:origin-left"


def test_a_stall_that_resumes_the_same_way_stays_in_the_shot():
    """A retried nudge continues the sweep; only a reversal ends the shot."""
    segments = _dwell([_shot("visit-0:origin-left")])
    samples = [(i, yaw, 0, 1) for i, yaw in enumerate([0, 0, 0, 40, 40, 40, 40, 80, 80, 80])]

    apply_motion_trim(segments, samples)
    shots = [x for x in segments[0]["children"] if x["kind"] == "shot"]

    assert len(shots) == 1
    assert shots[0]["start"] < 7 < shots[0]["end"]


def test_slow_drift_after_a_sweep_is_cut_as_waiting():
    segments = _dwell([_shot("visit-0:left-right")])
    samples = [
        (0.0, 0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
        (2.0, 0.0, 0.0, 1.0),
        (3.0, 10.0, 0.0, 1.0),
        (4.0, 20.0, 0.0, 1.0),
        (5.0, 30.0, 0.0, 1.0),
        (6.0, 30.0, 0.0, 1.0),
        (7.0, 30.4, 0.0, 1.0),
        (8.0, 30.8, 0.0, 1.0),
        (9.0, 31.2, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)

    assert [(c["kind"], c["start"], c["end"]) for c in segments[0]["children"]] == [
        ("preparation", 0.0, 1.0),
        ("shot", 1.0, 6.0),
        ("preparation", 6.0, 10),
    ]


@pytest.mark.parametrize("problem", ["gap", "incomplete", "missing_tail"])
def test_unreliable_edges_are_not_silently_discarded(problem):
    segments = _dwell([_shot("visit-0:origin-left")])
    samples = [(i, yaw, 0, 1) for i, yaw in enumerate([0, 0, 0, 10, 20, 30, 30, 30, 30, 30])]
    if problem == "gap":
        del samples[3:5]
    elif problem == "incomplete":
        segments[0]["children"][0]["complete"] = False
    else:
        samples = samples[:7]
    apply_motion_trim(segments, samples)
    shot = next(x for x in segments[0]["children"] if x["kind"] == "shot")
    assert shot["end"] == 10
    if problem != "missing_tail":
        assert shot["start"] == 0


def test_tree_retains_zoom_intent_and_original_evidence():
    payload = {"segments": [{"index": 0, "arrived_at_seconds": 0, "shots": [{
        "id": "custom-zoom", "label": "缩放", "kind": "shot", "start": 0, "end": 10,
        "status": "complete", "zoom_start": 2, "zoom_end": 1, "motion_axes": ["zoom"],
    }]}]}
    original = deepcopy(payload)
    tree = build_timeline(payload, 10)
    assert tree[0]["children"][0]["zoom_start"] == 2
    assert tree[0]["children"][0]["motion_axes"] == ["zoom"]
    apply_motion_trim(tree, [(i, 0, 0, 2 if i < 5 else 1) for i in range(10)])
    assert len(tree[0]["children"]) == 1
    assert payload == original
