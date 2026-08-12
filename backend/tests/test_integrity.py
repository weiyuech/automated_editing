"""Tests that the machinery is alive, not merely that its output has the right shape.

Every other test in this suite checks a contract: scenes come back, durations add up, clips
are in order. A contract is exactly what a fallback satisfies — which is why PySceneDetect
could crash on every file for the life of the project while the suite stayed green, and why
frame scoring could measure nothing at all and report that everything was excellent.

So these assert something **only the working mechanism can produce**: that the real detector
ran rather than the stand-in, that frames were genuinely decoded rather than assumed, that a
cached answer is the same answer, and that a failure that does happen is visible.

The rule for adding to this file: if a broken implementation could pass the test by returning
a plausible constant, it is not an integrity test.
"""

import json
import subprocess

import pytest

from automated_video_editing_backend.services import scoring
from automated_video_editing_backend.services.analysis import AnalysisService

SIZE = "size=320x240:rate=25"


def _clip(path, source, seconds=6):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"{source}:{SIZE}:duration={seconds}" if "=" in source else f"{source}={SIZE}:duration={seconds}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


def _spliced(path, tmp_path):
    """One file with two hard cuts at known times: 6s and 12s."""
    parts = [
        _clip(tmp_path / "p0.mp4", "testsrc"),
        _clip(tmp_path / "p1.mp4", "smptebars"),
        _clip(tmp_path / "p2.mp4", "testsrc2"),
    ]
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(parts[0]), "-i", str(parts[1]), "-i", str(parts[2]),
         "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]", "-map", "[v]",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return path


# ── the detector, not its understudy ────────────────────────────────────────────────────

def test_the_real_scene_detector_runs_rather_than_the_fallback(tmp_path):
    """The test that was missing for the life of the project.

    PySceneDetect's OpenCV backend answered NaN when asked a video's position and killed its
    own decode thread, on every file. The failure became a warning string, detection fell back
    to a frame-differencer, and every existing test still passed because scenes came back.
    Asserting *which* path produced them is the only version of this test that has teeth.
    """
    video = _spliced(tmp_path / "spliced.mp4", tmp_path)
    warnings: list[str] = []

    scenes = AnalysisService().detect_scenes(video, warnings)

    assert not any("PySceneDetect failed" in note for note in warnings), warnings
    assert not any("fallback" in note for note in warnings), warnings
    assert scenes


def test_detected_cuts_land_on_the_real_ones(tmp_path):
    """Shape is not enough: the fallback also returns plausibly-shaped scenes. These are the
    cuts that are actually in the file."""
    video = _spliced(tmp_path / "spliced.mp4", tmp_path)

    scenes = AnalysisService().detect_scenes(video, [])
    starts = [scene["start"] for scene in scenes]

    for truth in (6.0, 12.0):
        assert any(abs(start - truth) < 0.5 for start in starts), (truth, starts)


# ── frames were really decoded ──────────────────────────────────────────────────────────

def test_scoring_measures_frames_instead_of_assuming_them(tmp_path):
    """A scorer that cannot decode returns neutral for everything, which reads as "all shots
    are excellent" and is indistinguishable from success. Only real measurement can make
    visibly different footage score differently."""
    # testsrc2 genuinely animates frame to frame; testsrc is a near-static card.
    busy = _clip(tmp_path / "busy.mp4", "testsrc2")
    dead = _clip(tmp_path / "dead.mp4", "color=c=gray")

    (lively,), first_problem = scoring.score_shots(busy, [(0.0, 6.0)])
    (flat,), second_problem = scoring.score_shots(dead, [(0.0, 6.0)])

    assert not first_problem and not second_problem
    assert lively.quality != flat.quality, "scores identical — nothing was measured"
    assert lively.fingerprint != flat.fingerprint
    # Neutral is exactly 1.0 on every field; a measured shot will not be.
    assert (flat.quality, flat.motion) != (1.0, 1.0)


def test_a_scorer_that_cannot_see_says_so(tmp_path):
    """The failure must be reportable, not merely survivable."""
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"this is not a video")

    scores, problem = scoring.score_shots(broken, [(0.0, 5.0)])

    assert problem, "silent neutral scores are how this bug class hides"
    assert len(scores) == 1


def test_every_shot_of_a_spliced_file_is_measured(tmp_path):
    """Sampling walks the file once and must reach every shot, including the last."""
    video = _spliced(tmp_path / "spliced.mp4", tmp_path)

    scores, problem = scoring.score_shots(video, [(0.0, 6.0), (6.0, 12.0), (12.0, 18.0)])

    assert not problem, problem
    assert len({score.fingerprint for score in scores}) == 3, "shots were not sampled apart"


# ── the cache returns the same answer, not just a fast one ──────────────────────────────

def test_a_cached_analysis_is_the_same_analysis(tmp_path):
    """A cache that is merely fast is worse than none. Cold and warm must agree, warnings
    included, or a job's behaviour would depend on whether it happened to be first."""
    video = _spliced(tmp_path / "spliced.mp4", tmp_path)
    service = AnalysisService()

    cold_warnings: list[str] = []
    cold = service.detect_scenes(video, cold_warnings)
    warm_warnings: list[str] = []
    warm = service.detect_scenes(video, warm_warnings)

    assert cold == warm
    assert cold_warnings == warm_warnings


def test_the_cache_notices_the_file_changed(tmp_path):
    """Stale scenes would point at footage that no longer exists at those timestamps."""
    video = _clip(tmp_path / "clip.mp4", "testsrc")
    service = AnalysisService()
    before = service.detect_scenes(video, [])

    _clip(video, "smptebars", seconds=10)
    after = service.detect_scenes(video, [])

    assert before != after


# ── failures that change the output must not be reported as facts about the footage ─────

def test_an_unprobeable_source_is_not_reported_as_a_silent_one(tmp_path):
    """`has_audio_stream` used to answer False both for "this clip is silent" and "ffprobe did
    not run". The operator was then told their footage had no audio track — a claim about
    their footage built from a fact about the tool."""
    from automated_video_editing_backend.services.render import RenderService

    missing = tmp_path / "not-here.mp4"
    silent = _clip(tmp_path / "silent.mp4", "testsrc")

    renderer = RenderService()
    assert renderer.has_audio_stream(str(missing)) is None, "unknown must not read as False"
    assert renderer.has_audio_stream(str(silent)) is False


@pytest.mark.asyncio
async def test_the_two_audio_failures_produce_different_warnings(tmp_path):
    from automated_video_editing_backend.core.models import EditTimeline, TimelineClip
    from automated_video_editing_backend.services.jobs import JobService
    from automated_video_editing_backend.services.render import RenderService

    class Events:
        async def publish(self, *_args, **_kwargs):
            return None

    silent = _clip(tmp_path / "silent.mp4", "testsrc")
    service = JobService(Events(), None, None, None, RenderService())

    def timeline_for(source):
        return EditTimeline(
            title="t", output_path=str(tmp_path / "out.mp4"), mute_original_audio=False,
            clips=[TimelineClip(media_id="c", source_path=str(source), start=0, duration=1, timeline_start=0)],
        )

    truly_silent = timeline_for(silent)
    service._resolve_original_audio(False, [str(silent)], truly_silent)

    unprobeable = timeline_for(tmp_path / "missing.mp4")
    service._resolve_original_audio(False, [str(tmp_path / "missing.mp4")], unprobeable)

    assert any("没有声音轨" in note for note in truly_silent.warnings)
    assert any("无法确认音轨" in note for note in unprobeable.warnings)
    assert truly_silent.warnings != unprobeable.warnings


# ── a limit that resets by accident is not a limit ──────────────────────────────────────

def test_the_daily_allowance_cannot_be_reset_by_a_half_written_file(tmp_path):
    """A crash midway through a plain write leaves an unparseable file, which reads back as
    "nothing used today" and silently grants the day over again."""
    from automated_video_editing_backend.services.settings import SettingsService

    settings = SettingsService(path=tmp_path / "settings.json")
    settings.record_outputs(40)
    assert settings.output_quota().used_today == 40

    usage = tmp_path / "automation-usage.json"
    text = usage.read_text(encoding="utf-8")
    # What a torn write looks like on disk.
    usage.write_text(text[: len(text) // 2], encoding="utf-8")

    recovered = SettingsService(path=tmp_path / "settings.json")
    torn = recovered.output_quota().used_today
    # Reading a torn file cannot recover the count, but writing must never produce one: after
    # any completed write the file parses.
    settings.record_outputs(1)
    assert json.loads(usage.read_text(encoding="utf-8"))
    assert torn == 0  # documents the one case that is genuinely unrecoverable


def test_the_allowance_survives_a_restart(tmp_path):
    from automated_video_editing_backend.services.settings import SettingsService

    SettingsService(path=tmp_path / "settings.json").record_outputs(7)

    assert SettingsService(path=tmp_path / "settings.json").output_quota().used_today == 7


# ── state files must not lose data when a write is interrupted ──────────────────────────

def test_a_torn_write_can_never_be_produced(tmp_path):
    """Every small state file in this app was written with a plain write and read back with a
    try/except returning empty. A crash, a full disk or a closed lid mid-write leaves a
    truncated file, which will not parse, which reads as "nothing saved" — a legitimate state.
    That is how notes, saved routes and a paid quota disappear without anyone being told."""
    from automated_video_editing_backend.core.store import read_json, write_json

    target = tmp_path / "state.json"
    assert write_json(target, {"kept": [1, 2, 3]})

    # No stray temporary is left behind to be mistaken for the real file.
    assert not list(tmp_path.glob("*.tmp"))
    data, problem = read_json(target)
    assert data == {"kept": [1, 2, 3]} and not problem


def test_an_unreadable_state_file_is_not_reported_as_an_empty_one(tmp_path):
    """Absent and destroyed are different answers and must not share a return value."""
    from automated_video_editing_backend.core.store import read_json

    absent, no_problem = read_json(tmp_path / "never-written.json")
    assert absent is None and no_problem == ""

    damaged = tmp_path / "damaged.json"
    damaged.write_text('{"half": [1, 2', encoding="utf-8")
    data, problem = read_json(damaged)

    assert data is None
    assert problem, "a destroyed store must not read as an empty one"


def test_a_damaged_store_is_kept_rather_than_overwritten(tmp_path):
    """The next save would otherwise destroy the evidence, and with it any chance of getting
    the notes or the hand-typed goal ids back."""
    from automated_video_editing_backend.core.store import read_json

    damaged = tmp_path / "routes.json"
    damaged.write_text('[{"name": "morning round"', encoding="utf-8")

    read_json(damaged)

    saved = list(tmp_path.glob("routes.json.corrupt-*"))
    assert saved, "the bytes must survive for a human to look at"
    assert "morning round" in saved[0].read_text(encoding="utf-8")


def test_capture_notes_survive_a_damaged_sessions_file(tmp_path):
    """The store reports the loss instead of starting silently empty."""
    from automated_video_editing_backend.core.events import EventHub
    from automated_video_editing_backend.services.capture import CaptureService

    path = tmp_path / "capture-sessions.json"
    path.write_text('[{"id": "a", "title": "produ', encoding="utf-8")

    service = CaptureService(EventHub(), path=path)

    assert service.load_problem, "a lost session file must not look like a fresh install"
    assert service.list_sessions() == []


def test_saved_routes_report_their_own_loss(tmp_path):
    """goal ids cannot be enumerated from the robot — they are typed by hand and discovered by
    driving — so a lost route file is expensive and must never look like having saved none."""
    from automated_video_editing_backend.services.cruise_routes import CruiseRouteStore

    path = tmp_path / "cruise-routes.json"
    path.write_text('[{"id": "r1", "name": "aisle', encoding="utf-8")

    store = CruiseRouteStore(path=path)

    assert store.load_problem
    assert store.list_routes() == []


# ── the motion metric measures movement, not texture ────────────────────────────────────

def test_shake_is_detected_on_footage_that_differs_only_by_shake(tmp_path):
    """The same frames, jittered. Content, exposure and subject are identical, so anything
    that separates them is measuring camera movement and nothing else.

    A plain frame difference barely can: on this pair it rose 27 to 33 while the frame shift
    went 1.0 to 5.0 pixels. Built on the difference alone, a scorer calls the shaky version
    steady — and an earlier version of this one did.
    """
    source = _clip(tmp_path / "source.mp4", "testsrc2", seconds=4)
    steady = tmp_path / "steady.mp4"
    shaky = tmp_path / "shaky.mp4"
    for target, crop in (
        (steady, "crop=in_w-60:in_h-60:30:30"),
        (shaky, "crop=in_w-60:in_h-60:30+20*sin(n*1.7):30+20*cos(n*2.3)"),
    ):
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-vf", crop, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(target)],
            check=True,
        )

    (calm,), _ = scoring.score_shots(steady, [(0.0, 4.0)])
    (rough,), _ = scoring.score_shots(shaky, [(0.0, 4.0)])

    assert rough.steadiness < calm.steadiness, "shake is invisible to this scorer"
    assert rough.quality < calm.quality


def test_texture_that_shimmers_is_not_mistaken_for_a_shaking_camera(tmp_path):
    """Fine detail changing in place — water sparkling, leaves moving — fills a raw frame
    difference without the camera going anywhere. Real footage of water measured 26 that way,
    of which only 5 was movement. Judged raw, a tripod shot of a pond looks unusable.
    """
    # A fixed frame whose pixels churn: the picture never moves, the detail never stops.
    shimmer = tmp_path / "shimmer.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"color=c=gray:{SIZE}:duration=4",
         "-vf", "noise=alls=45:allf=t+u", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(shimmer)],
        check=True,
    )

    (score,), problem = scoring.score_shots(shimmer, [(0.0, 4.0)])

    assert not problem
    # The frame never moves, whatever the pixels do.
    assert score.steadiness > 0.5, score
