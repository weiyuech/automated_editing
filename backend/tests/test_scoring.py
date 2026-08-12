import subprocess

import pytest

from automated_video_editing_backend.services import scoring


SIZE = "size=320x240:rate=25:duration=6"


def _render(path, source):
    """`source` is a full lavfi spec; sources differ in how they take their options."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", source, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


def test_a_still_shot_and_a_moving_one_do_not_score_the_same(tmp_path):
    """Motion is a band, not a direction: a locked-off shot of nothing is as unusable as a
    violent pan, and a scorer blind to one end fills the edit with the other."""
    # testsrc2, not testsrc: the latter is a near-static card whose adjacent frames differ by
    # 0.3 of 255, which is correctly read as no motion at all.
    moving = _render(tmp_path / "moving.mp4", f"testsrc2={SIZE}")
    still = _render(tmp_path / "still.mp4", f"color=c=gray:{SIZE}")

    (busy,), _ = scoring.score_shots(moving, [(0.0, 6.0)])
    (dead,), _ = scoring.score_shots(still, [(0.0, 6.0)])

    assert dead.motion < busy.motion
    assert dead.quality < busy.quality


def test_nothing_is_ever_scored_to_zero(tmp_path):
    """A run of poor footage still has to produce a video. Weak shots are priced down and stay
    eligible; a rule that can reject everything needs a fallback, and fallbacks are where this
    planner's ordering bugs have all lived."""
    flat = _render(tmp_path / "flat.mp4", f"color=c=black:{SIZE}")

    (score,), _ = scoring.score_shots(flat, [(0.0, 6.0)])

    assert score.quality >= scoring.FLOOR
    assert 0.0 <= score.quality <= 1.0


def test_one_bad_measurement_cannot_annihilate_a_shot():
    """These thresholds are uncalibrated guesses, so a wrong one must cost a shot its ranking
    rather than remove it from the recording."""
    worst = scoring._blend(0.0, 0.0, 0.0)
    one_bad = scoring._blend(0.0, 1.0, 1.0)
    perfect = scoring._blend(1.0, 1.0, 1.0)

    assert perfect == pytest.approx(1.0)
    assert one_bad > worst
    # A single zero leaves most of the score intact.
    assert one_bad > 0.5


def test_scores_are_relative_to_the_recording_they_came_from():
    """Sharpness has no fixed scale — it depends on lens, resolution and how much detail the
    subject happens to hold. A recording that is uniformly soft must not be thrown away whole;
    its least soft shots should still rank."""
    uniformly_soft = [
        {"sharp": 5.0, "clipped": 0.0, "motion": 6.0, "colour": (0.0, 0.0, 0.0), "fingerprint": 0},
        {"sharp": 4.0, "clipped": 0.0, "motion": 6.0, "colour": (0.0, 0.0, 0.0), "fingerprint": 0},
    ]
    scores = scoring._combine(uniformly_soft)

    assert all(score.quality > 0.8 for score in scores)


def test_a_shot_worse_lit_than_the_rest_is_the_one_penalised():
    """Clipping has no absolute meaning either: a night shot is mostly black and a shot through
    a window is partly white. What matters is being worse than this recording's normal."""
    shots = [
        {"sharp": 100.0, "clipped": 0.10, "motion": 6.0, "colour": (0.0, 0.0, 0.0), "fingerprint": 0},
        {"sharp": 100.0, "clipped": 0.10, "motion": 6.0, "colour": (0.0, 0.0, 0.0), "fingerprint": 0},
        {"sharp": 100.0, "clipped": 0.55, "motion": 6.0, "colour": (0.0, 0.0, 0.0), "fingerprint": 0},
    ]
    scores = scoring._combine(shots)

    assert scores[0].exposure == pytest.approx(1.0)
    assert scores[2].exposure < scores[0].exposure


def test_an_unreadable_file_says_so_instead_of_scoring_everything_perfect(tmp_path):
    """The decoder this replaced failed on every file in the project and reported it by
    returning nothing, which is exactly how it went unnoticed for so long."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"not a video")

    scores, problem = scoring.score_shots(broken, [(0.0, 5.0)])

    assert len(scores) == 1
    assert problem, "a scorer that cannot see must say so"


def test_the_same_frame_hashes_alike_and_a_different_one_does_not(tmp_path):
    bars = _render(tmp_path / "bars.mp4", f"smptebars={SIZE}")
    noise = _render(tmp_path / "noise.mp4", f"testsrc2={SIZE}")

    (first,), _ = scoring.score_shots(bars, [(0.0, 3.0)])
    (again,), _ = scoring.score_shots(bars, [(0.0, 3.0)])
    (other,), _ = scoring.score_shots(noise, [(0.0, 3.0)])

    assert scoring.fingerprint_distance(first.fingerprint, again.fingerprint) == 0
    assert scoring.fingerprint_distance(first.fingerprint, other.fingerprint) > 8


def test_colour_distance_grows_with_the_jolt_between_two_shots():
    dim = (30.0, 128.0, 128.0)
    similar = (34.0, 128.0, 128.0)
    bright = (220.0, 128.0, 128.0)

    assert scoring.colour_distance(dim, similar) < scoring.colour_distance(dim, bright)
