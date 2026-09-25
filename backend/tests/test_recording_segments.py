from automated_video_editing_backend.services.recording_segments import apply_motion_trim


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
        (2.0, 10.0, 0.0, 1.0),
        (3.0, 20.0, 0.0, 1.0),
        (4.0, 30.0, 0.0, 1.0),
        (5.0, 30.0, 0.0, 1.0),
        (6.0, 30.0, 0.0, 1.0),
        (7.0, 30.0, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 1.0),
        ("shot", 1.0, 4.0),
        ("preparation", 4.0, 10.0),
    ]
    assert children[0]["id"] == "shot-1:prep-head"
    assert children[1]["id"] == "shot-1"
    assert children[2]["id"] == "shot-1:prep-tail"


def test_zoom_only_motion_is_kept_when_yaw_and_pitch_are_static():
    segments = _dwell([_shot(kind="zoom")])
    samples = [
        (0.0, 0.0, 0.0, 1.0),
        (1.0, 0.0, 0.0, 1.0),
        (2.0, 0.0, 0.0, 1.5),
        (3.0, 0.0, 0.0, 2.0),
        (4.0, 0.0, 0.0, 2.0),
        (5.0, 0.0, 0.0, 2.0),
        (6.0, 0.0, 0.0, 2.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 1.0),
        ("zoom", 1.0, 3.0),
        ("preparation", 3.0, 10.0),
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


def test_adjacent_preparation_intervals_merge():
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
        (2.5, 5.0, 0.0, 1.0),
        (3.5, 5.0, 0.0, 1.0),
        (4.5, 15.0, 0.0, 1.0),
        (5.5, 25.0, 0.0, 1.0),
        (6.5, 25.0, 0.0, 1.0),
        (7.5, 25.0, 0.0, 1.0),
    ]

    apply_motion_trim(segments, samples)
    children = segments[0]["children"]

    assert [(c["kind"], c["start"], c["end"]) for c in children] == [
        ("preparation", 0.0, 3.5),
        ("shot", 3.5, 5.5),
        ("preparation", 5.5, 8.0),
    ]
    assert children[0]["id"] == "gap-0"
