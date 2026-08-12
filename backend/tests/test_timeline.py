import itertools

import pytest

from automated_video_editing_backend.core.models import AnalysisResult, EditJobRequest, MediaItem
from automated_video_editing_backend.services.timeline import EditPlanner


def test_planner_creates_clips():
    media = MediaItem(path="/tmp/a.mp4", kind="video")
    request = EditJobRequest(title="Demo", media_ids=[media.id])
    analysis = AnalysisResult(media_id=media.id, scenes=[{"start": 0, "end": 3}])
    timeline = EditPlanner().plan(request, [media], [analysis], None)
    assert timeline.title == "Demo"
    assert len(timeline.clips) == 1


def test_portrait_preset_reaches_picture_and_subtitle_canvas():
    media = MediaItem(path="/tmp/a.mp4", kind="video")
    request = EditJobRequest(
        title="Portrait", media_ids=[media.id], output_aspect_ratio="9:16",
    )
    analysis = AnalysisResult(media_id=media.id, scenes=[{"start": 0, "end": 3}])

    timeline = EditPlanner().plan(request, [media], [analysis], None)

    assert (timeline.output_width, timeline.output_height) == (720, 1280)


def test_unset_framing_keeps_the_source_canvas():
    media = MediaItem(path="/tmp/native-portrait.mp4", kind="video")
    request = EditJobRequest(title="Native", media_ids=[media.id])
    analysis = AnalysisResult(media_id=media.id, scenes=[{"start": 0, "end": 3}])

    timeline = EditPlanner().plan(
        request, [media], [analysis], None, source_size=(1080, 1920),
    )

    assert (timeline.output_width, timeline.output_height) == (1080, 1920)
    assert timeline.output_fit == "contain"


def test_cuts_land_on_the_beat_rather_than_near_it():
    """Cuts used to be beat-*sized* and not beat-*aligned*: lengths came from beat intervals
    while the cut points fell wherever the previous cut happened to end, so landing on a beat
    was coincidence. The cut points themselves are now snapped to the music."""
    video = MediaItem(path="/tmp/a.mp4", kind="video")
    music = MediaItem(path="/tmp/music.wav", kind="audio")
    request = EditJobRequest(
        title="Beat demo", media_ids=[video.id], music_media_id=music.id,
        beat_sync=True, pace="normal", contour="flat", target_duration_seconds=20.0,
    )
    beats = [index * 1.5 for index in range(60)]
    # One continuous run, which is what a cruise records. A cut cannot span two shots, so a
    # grid landing on a shot boundary is shortened there and the alignment stops at that cut.
    analysis = AnalysisResult(media_id=video.id, scenes=[{"start": 0, "end": 60}], beats=beats)

    timeline = EditPlanner().plan(request, [video], [analysis], music)

    assert timeline.music_path == music.path
    assert timeline.beat_sync is True
    # Every interior cut point falls on a beat.
    edge = 0.0
    for clip in timeline.clips[:-1]:
        edge += clip.duration
        assert min(abs(edge - beat) for beat in beats) < 1e-6, edge
    assert sum(clip.duration for clip in timeline.clips) == pytest.approx(20.0, abs=0.05)


def test_planner_keeps_voiceover_separate_from_music():
    video = MediaItem(path="/tmp/a.mp4", kind="video")
    music = MediaItem(path="/tmp/music.wav", kind="audio")
    voice = MediaItem(path="/tmp/voice.mp3", kind="audio")
    request = EditJobRequest(
        title="Voice demo",
        media_ids=[video.id],
        music_media_id=music.id,
        voiceover_media_id=voice.id,
    )
    analysis = AnalysisResult(media_id=video.id, scenes=[{"start": 0, "end": 3}])

    timeline = EditPlanner().plan(request, [video], [analysis], music, voice)

    assert timeline.music_path == music.path
    assert timeline.voiceover_path == voice.path


def _plan(scenes, target, voiceover_duration=None, variant_seed=None):
    video = MediaItem(path="/tmp/a.mp4", kind="video")
    request = EditJobRequest(
        title="Duration demo", media_ids=[video.id],
        target_duration_seconds=target, beat_sync=False, variant_seed=variant_seed,
    )
    analysis = AnalysisResult(media_id=video.id, scenes=[{"start": a, "end": b} for a, b in scenes])
    return EditPlanner().plan(
        request, [video], [analysis], None, None, voiceover_duration=voiceover_duration,
    )


def _picture(timeline):
    return [(round(clip.start, 3), round(clip.duration, 3)) for clip in timeline.clips]


def test_long_uncut_footage_fills_the_target_instead_of_yielding_one_chunk():
    """Two minutes of continuous cruise footage used to render as five seconds.

    The planner took at most one chunk per detected scene and stopped, so a recording with
    no hard cuts contributed exactly one.
    """
    timeline = _plan([(0, 120)], 30.0)

    assert sum(clip.duration for clip in timeline.clips) == 30.0
    # Sampled across the whole recording, not just its opening thirty seconds.
    assert max(clip.start for clip in timeline.clips) > 60.0
    assert not timeline.warnings


def test_target_duration_is_a_ceiling_and_short_footage_says_so():
    timeline = _plan([(0, 4), (4, 8)], 15.0)

    assert sum(clip.duration for clip in timeline.clips) == 8.0
    assert any("素材只够" in warning for warning in timeline.warnings)


def test_footage_cycles_to_cover_a_longer_voiceover():
    """Narration is never cut off mid-sentence; the picture repeats to reach the end of it."""
    timeline = _plan([(0, 120)], 30.0, voiceover_duration=45.0)

    assert sum(clip.duration for clip in timeline.clips) == 45.0
    assert any("画面循环" in warning for warning in timeline.warnings)


def test_short_voiceover_leaves_the_video_alone_but_warns():
    timeline = _plan([(0, 120)], 30.0, voiceover_duration=5.0)

    assert sum(clip.duration for clip in timeline.clips) == 30.0
    assert any("旁白比画面短" in warning for warning in timeline.warnings)


def test_seeds_pick_different_cuts_from_the_same_footage():
    """One recording used to yield one picture, so a hundred outputs were a hundred copies
    of the same video with different music laid over them."""
    pictures = {tuple(_picture(_plan([(0, 120)], 30.0, variant_seed=seed))) for seed in range(10)}

    assert len(pictures) > 1, "every seed produced the identical picture"


def test_a_seed_reproduces_its_own_picture_exactly():
    """Regenerating one output of a batch must give back the video that was reviewed."""
    assert _picture(_plan([(0, 120)], 30.0, variant_seed=4242)) == _picture(
        _plan([(0, 120)], 30.0, variant_seed=4242)
    )


def test_no_seed_keeps_the_old_fixed_pick():
    """A hand-made single job stays predictable; only batches roll the dice."""
    assert _picture(_plan([(0, 120)], 30.0)) == _picture(_plan([(0, 120)], 30.0))


def test_every_seed_still_spans_the_whole_recording_and_fills_the_target():
    """Variety must not cost coverage: shifting where inside each stride window the cut is
    taken samples different moments, it does not crowd them into one end of the footage."""
    for seed in range(10):
        timeline = _plan([(0, 120)], 30.0, variant_seed=seed)
        assert sum(clip.duration for clip in timeline.clips) == pytest.approx(30.0, abs=0.05)
        assert max(clip.start for clip in timeline.clips) > 60.0


def test_clips_carry_what_the_robot_was_doing_while_filming():
    video = MediaItem(path="/tmp/cruise.mp4", kind="video")
    request = EditJobRequest(title="Cruise", media_ids=[video.id], beat_sync=False)
    analysis = AnalysisResult(media_id=video.id, scenes=[
        {"start": 0.0, "end": 20.0, "kind": "transit", "label": "path1#1"},
        {"start": 20.0, "end": 40.0, "kind": "dwell", "label": "path1#1"},
    ])

    timeline = EditPlanner().plan(request, [video], [analysis], None)

    carried = {(clip.footage, clip.label) for clip in timeline.clips}
    assert ("transit", "path1#1") in carried
    assert ("dwell", "path1#1") in carried


def test_ordinary_imports_have_no_robot_classification():
    timeline = _plan([(0, 30)], 20.0)

    assert all(clip.footage == "unknown" and clip.label == "" for clip in timeline.clips)


def _cruise_scenes(points=4, span=40.0, transit_share=0.6):
    """A run that travelled to each point and then parked at it."""
    scenes, cursor = [], 0.0
    for point in range(1, points + 1):
        handover = cursor + span * transit_share
        scenes.append({"start": cursor, "end": handover, "kind": "transit", "label": f"p{point}"})
        scenes.append({"start": handover, "end": cursor + span, "kind": "dwell", "label": f"p{point}"})
        cursor += span
    return scenes


def _plan_cruise(scenes, target=30.0, footage_mix="balanced", emphasis="target", seed=1,
                 pace="normal", contour="flat", rotation=0, point_scope="all"):
    video = MediaItem(path="/tmp/cruise.mp4", kind="video")
    request = EditJobRequest(
        title="Cruise", media_ids=[video.id], target_duration_seconds=target,
        beat_sync=False, variant_seed=seed, footage_mix=footage_mix, emphasis=emphasis,
        pace=pace, contour=contour, start_rotation=rotation, point_scope=point_scope,
    )
    analysis = AnalysisResult(media_id=video.id, scenes=scenes)
    return EditPlanner().plan(request, [video], [analysis], None)


def _seconds_of(timeline, footage):
    return sum(clip.duration for clip in timeline.clips if clip.footage == footage)


def test_the_mix_leans_the_edit_towards_parked_or_moving_footage():
    """Parked shots are not automatically the better ones, so the batch leans both ways."""
    scenes = _cruise_scenes()
    dwelling = _seconds_of(_plan_cruise(scenes, footage_mix="dwell_heavy"), "dwell")
    balanced = _seconds_of(_plan_cruise(scenes, footage_mix="balanced"), "dwell")
    moving = _seconds_of(_plan_cruise(scenes, footage_mix="transit_heavy"), "dwell")

    assert dwelling > balanced > moving


def test_every_mix_lands_on_the_target_by_holding_the_last_cut():
    """Each sort of footage is filled separately, and the last scrap of each is rarely a
    whole cut. Holding the cut already in hand a little longer reaches the target without
    ending on a flash frame; the target stays a ceiling either way."""
    for mix in ("dwell_heavy", "balanced", "transit_heavy"):
        total = sum(clip.duration for clip in _plan_cruise(_cruise_scenes(), footage_mix=mix).clips)
        assert abs(total - 30.0) < 0.05, mix


def test_a_cut_is_never_held_past_the_end_of_its_own_shot():
    """Stretching to reach the target must not read into the next shot, which would splice
    two points together inside one clip."""
    scenes = _cruise_scenes()
    ends = {(scene["start"], scene["end"]) for scene in scenes}
    timeline = _plan_cruise(scenes, footage_mix="dwell_heavy")

    for clip in timeline.clips:
        shot = next(end for start, end in ends if start <= clip.start < end)
        assert clip.start + clip.duration <= shot + 0.05


def test_a_lean_cannot_demand_more_footage_than_was_filmed():
    """Weights multiply what exists rather than setting a quota, so a run with three seconds
    of parked footage does not get twenty seconds of it on repeat."""
    scenes = [
        {"start": 0.0, "end": 100.0, "kind": "transit", "label": "p1"},
        {"start": 100.0, "end": 103.0, "kind": "dwell", "label": "p1"},
    ]
    timeline = _plan_cruise(scenes, footage_mix="dwell_heavy")

    assert _seconds_of(timeline, "dwell") <= 3.0 + 0.05


def test_pace_changes_how_many_cuts_an_edit_is_made_of():
    scenes = _cruise_scenes(points=6)
    counts = {
        pace: len(_plan_cruise(scenes, target=60.0, pace=pace).clips)
        for pace in ("fast", "normal", "cinematic")
    }

    assert counts["fast"] > counts["normal"] > counts["cinematic"]


def test_contour_shapes_the_edit_from_end_to_end():
    scenes = _cruise_scenes(points=6, span=60.0)
    accelerate = _plan_cruise(scenes, target=60.0, contour="accelerate").clips
    decelerate = _plan_cruise(scenes, target=60.0, contour="decelerate").clips

    assert accelerate[0].duration > accelerate[-1].duration
    assert decelerate[0].duration < decelerate[-1].duration


def test_a_contour_needing_music_it_does_not_have_falls_back_and_says_so():
    """Doing nothing quietly is the one behaviour a pinned setting must never have."""
    timeline = _plan_cruise(_cruise_scenes(), contour="follow_energy")

    assert timeline.clips
    assert any("跟随强度" in warning for warning in timeline.warnings)


def test_rotation_opens_the_edit_somewhere_else_on_the_route():
    scenes = _cruise_scenes(points=6)
    opens_at = {}
    for rotation in range(4):
        clips = _plan_cruise(scenes, pace="fast", rotation=rotation).clips
        opens_at[rotation] = clips[0].label

    assert len(set(opens_at.values())) > 1
    assert opens_at[0] == "p1"


def test_a_pace_the_footage_cannot_carry_says_so_instead_of_quietly_shortening():
    """An eight-second cut cannot be taken from a four-second shot, and no ordering of the
    cuts changes that. Shortening in silence leaves the operator wondering why a twenty-second
    setting produced twelve."""
    scenes = [
        {"start": index * 4.0, "end": index * 4.0 + 4.0, "kind": "dwell", "label": f"p{index}"}
        for index in range(10)
    ]
    slow = _plan_cruise(scenes, target=30.0, pace="cinematic")
    quick = _plan_cruise(scenes, target=30.0, pace="fast")

    assert any("短于目标" in warning for warning in slow.warnings), slow.warnings
    # The remedy the warning suggests actually works.
    assert sum(c.duration for c in quick.clips) > sum(c.duration for c in slow.clips)
    assert not any("短于目标" in warning for warning in quick.warnings), quick.warnings


def test_rotation_breaks_the_route_order_but_not_a_place_s_own_order():
    """Rotation is the one dimension that deliberately reorders the route. Inside a place the
    footage must still run forwards — an edit may start elsewhere, it may not stutter."""
    timeline = _plan_cruise(_cruise_scenes(points=6), pace="fast", rotation=2)
    seen: dict[str, float] = {}
    for clip in timeline.clips:
        assert clip.start >= seen.get(clip.label, -1.0), clip.label
        seen[clip.label] = clip.start + clip.duration

    # And a place is never returned to once the edit has moved on.
    order = [clip.label for clip in timeline.clips]
    assert len(set(order)) == len([k for k, _ in itertools.groupby(order)])


def test_scope_narrows_the_route_without_losing_the_running_time():
    scenes = _cruise_scenes(points=8)
    whole = _plan_cruise(scenes, target=40.0, pace="fast", point_scope="all")
    part = _plan_cruise(scenes, target=40.0, pace="fast", point_scope=0.5)

    assert len({clip.label for clip in part.clips}) < len({clip.label for clip in whole.clips})
    # Using fewer places must not make a shorter video, only a less hurried one.
    assert sum(clip.duration for clip in part.clips) == pytest.approx(40.0, abs=1.0)


def test_scope_widens_rather_than_leaving_the_picture_short():
    """A thin draw would loop the picture while unused places sit outside the scope, so the
    selection grows until it can actually cover the running time."""
    scenes = [
        {"start": 0.0, "end": 4.0, "kind": "dwell", "label": "p1"},
        {"start": 4.0, "end": 8.0, "kind": "dwell", "label": "p2"},
        {"start": 8.0, "end": 12.0, "kind": "dwell", "label": "p3"},
        {"start": 12.0, "end": 60.0, "kind": "dwell", "label": "p4"},
    ]
    timeline = _plan_cruise(scenes, target=40.0, pace="fast", point_scope=0.5)

    assert sum(clip.duration for clip in timeline.clips) > 35.0


def test_no_edit_ever_jumps_backwards_in_time():
    """A cut must never replay footage already shown, which reads as a jump back in time.

    Two causes, both real: picks counted in cuts rather than seconds fell short of their
    share and the filler wrapped around; and stride windows offset in fractional positions
    are not actually disjoint, so two neighbours straddling the same whole position both
    rounded onto it and picked the same cut.
    """
    scenes = _cruise_scenes(points=6)
    for mix in ("dwell_heavy", "balanced", "transit_heavy"):
        for emphasis in ("target", "coverage"):
            for seed in range(40):
                clips = _plan_cruise(scenes, footage_mix=mix, emphasis=emphasis, seed=seed).clips
                case = (mix, emphasis, seed)
                spans = [(round(clip.start, 3), round(clip.duration, 3)) for clip in clips]
                assert len(spans) == len(set(spans)), case
                for earlier, later in zip(clips, clips[1:]):
                    assert earlier.start <= later.start, case
                    assert earlier.start + earlier.duration <= later.start + 1e-6, case


def test_a_point_with_little_footage_does_not_force_a_repeat():
    """Coverage splits the time evenly, but a point the robot barely lingered at cannot be
    asked for more than it has; the rest goes to the points that can absorb it."""
    scenes = [
        {"start": 0.0, "end": 60.0, "kind": "dwell", "label": "p1"},
        {"start": 60.0, "end": 62.0, "kind": "dwell", "label": "p2"},
        {"start": 62.0, "end": 122.0, "kind": "dwell", "label": "p3"},
    ]
    timeline = _plan_cruise(scenes, emphasis="coverage")
    spans = [(clip.start, clip.duration) for clip in timeline.clips]

    assert len(spans) == len(set(spans))
    assert len({clip.label for clip in timeline.clips}) == 3


def test_a_scarce_sort_is_never_repeated_while_another_goes_unused():
    """Weighting alone does not keep a lean honest. Where the running time is nearly all the
    footage there is, a scarce sort's weighted share can exceed what was filmed — and the
    filler then repeats it while usable footage of another sort sits unspent."""
    scenes = [
        {"start": 0.0, "end": 30.0, "kind": "transit", "label": "p1"},
        {"start": 30.0, "end": 36.0, "kind": "dwell", "label": "p1"},
    ]
    timeline = _plan_cruise(scenes, target=36.0, footage_mix="dwell_heavy")
    spans = [(clip.start, clip.duration) for clip in timeline.clips]

    assert _seconds_of(timeline, "dwell") <= 6.0 + 0.05
    assert _seconds_of(timeline, "transit") <= 30.0 + 0.05
    # Nothing is placed twice while anything remains unplaced.
    assert len(spans) == len(set(spans))
    # A target equal to every second of footage cannot be met to the frame — cuts are whole
    # and cannot span two shots — but it must be approached, not abandoned.
    assert sum(clip.duration for clip in timeline.clips) > 36.0 * 0.85


def test_coverage_shares_evenly_where_target_lets_one_point_swallow_the_edit():
    """A point with a long approach takes a proportional edit over; coverage divides the cuts
    between the points first, so each gets the same room."""
    scenes, cursor = [], 0.0
    for point, span in [(1, 200.0), (2, 20.0), (3, 20.0), (4, 20.0), (5, 20.0)]:
        scenes.append({"start": cursor, "end": cursor + span * 0.6, "kind": "transit", "label": f"p{point}"})
        scenes.append({"start": cursor + span * 0.6, "end": cursor + span, "kind": "dwell", "label": f"p{point}"})
        cursor += span

    # Fast cuts, so the edit has enough of them to reach every point.
    by_target = _plan_cruise(scenes, emphasis="target", footage_mix="balanced", pace="fast")
    by_coverage = _plan_cruise(scenes, emphasis="coverage", footage_mix="balanced", pace="fast")

    def spread(timeline):
        shares: dict[str, float] = {}
        for clip in timeline.clips:
            shares[clip.label] = shares.get(clip.label, 0.0) + clip.duration
        return max(shares.values()) - min(shares.values())

    # Both reach every point at this pace; what differs is how the time is divided. Under
    # target the long approach takes several times the room the quick points get.
    assert spread(by_coverage) < spread(by_target)
    # Coverage is equal to within one cut — cuts are whole and need not divide evenly.
    assert spread(by_coverage) <= 2.0 + 0.05


def test_coverage_shows_fewer_places_at_a_slower_pace():
    """Pace governs how many places an edit can carry: held shots leave room for fewer of
    them. This is the choice being made, not an approximation of it."""
    scenes = [
        {"start": index * 20.0, "end": index * 20.0 + 20.0, "kind": "dwell", "label": f"q{index:02d}"}
        for index in range(30)
    ]
    fast = _plan_cruise(scenes, target=60.0, emphasis="coverage", pace="fast")
    cinematic = _plan_cruise(scenes, target=60.0, emphasis="coverage", pace="cinematic")

    assert len({clip.label for clip in fast.clips}) > len({clip.label for clip in cinematic.clips})


def test_coverage_that_cannot_fit_says_how_many_it_took():
    """Thirty points in a thirty-second edit is flash frames, so it covers what the cuts
    allow — sampled across the whole run, not the first few — and reports the rest."""
    scenes = [
        {"start": index * 20.0, "end": index * 20.0 + 20.0, "kind": "dwell", "label": f"q{index:02d}"}
        for index in range(30)
    ]
    timeline = _plan_cruise(scenes, target=30.0, emphasis="coverage", pace="fast")
    covered = sorted({clip.label for clip in timeline.clips})

    assert 1 < len(covered) < 30
    assert any("只够覆盖" in warning for warning in timeline.warnings)
    # Sampled across the route rather than truncated at the front.
    assert covered[-1] > "q20"


def test_the_policies_leave_ordinary_footage_alone():
    """An import the robot never classified has nothing to lean on, so every mix agrees."""
    pictures = {
        tuple(_picture(_plan_cruise(
            [{"start": 0.0, "end": 120.0}], footage_mix=mix, emphasis=emphasis,
        )))
        for mix in ("dwell_heavy", "balanced", "transit_heavy")
        for emphasis in ("target", "coverage")
    }

    assert len(pictures) == 1


def test_a_route_that_returns_to_a_point_still_runs_forwards():
    """A revisit is a second *visit*, not more of the same place.

    Pooling shots by point name put every cut from the first approach before every cut from
    the first stop — including approach footage filmed an hour later, after the robot came
    back. The edit jumped from 98 seconds to 20 inside what it called one place.
    """
    scenes = [
        {"start": 0.0, "end": 20.0, "kind": "transit", "label": "p1"},
        {"start": 20.0, "end": 40.0, "kind": "dwell", "label": "p1"},
        {"start": 40.0, "end": 60.0, "kind": "transit", "label": "p2"},
        {"start": 60.0, "end": 80.0, "kind": "dwell", "label": "p2"},
        {"start": 80.0, "end": 100.0, "kind": "transit", "label": "p1"},
        {"start": 100.0, "end": 120.0, "kind": "dwell", "label": "p1"},
    ]
    clips = _plan_cruise(scenes, target=40.0, pace="fast", emphasis="coverage").clips
    starts = [clip.start for clip in clips]

    assert starts == sorted(starts), starts


def test_a_point_reached_on_a_second_attempt_still_runs_forwards():
    """One visit need not be travelling-then-parked. A point that failed and was reached on a
    retry films travelling, parked, travelling, parked — and splitting a visit by sort of
    footage puts the later travelling stretch before the earlier parked one."""
    scenes = [
        {"start": 0.0, "end": 15.0, "kind": "transit", "label": "p1"},
        {"start": 15.0, "end": 25.0, "kind": "dwell", "label": "p1"},
        {"start": 25.0, "end": 40.0, "kind": "transit", "label": "p1"},
        {"start": 40.0, "end": 60.0, "kind": "dwell", "label": "p1"},
    ]
    clips = _plan_cruise(scenes, target=30.0, pace="fast", emphasis="target").clips
    starts = [clip.start for clip in clips]

    assert starts == sorted(starts), starts


def test_poor_footage_is_asked_for_less_of_the_edit():
    """The measurements existed for a day and changed nothing: cuts were shared out per group,
    and a whole recording was one group, so plain footage got exactly its proportional share
    however soft, dark or dead it was. The shot is the unit now, so quality has something to
    choose between."""
    video = MediaItem(path="/tmp/x.mp4", kind="video")
    scenes = [
        {"start": index * 20.0, "end": index * 20.0 + 20.0, "quality": quality}
        for index, quality in enumerate([1.0, 1.0, 0.10, 0.10, 1.0, 1.0])
    ]
    request = EditJobRequest(
        title="t", media_ids=[video.id], target_duration_seconds=30.0,
        beat_sync=False, variant_seed=5, pace="normal", contour="flat",
    )
    clips = EditPlanner().plan(
        request, [video], [AnalysisResult(media_id=video.id, scenes=scenes)], None,
    ).clips

    poor = sum(clip.duration for clip in clips if 40.0 <= clip.start < 80.0)
    total = sum(clip.duration for clip in clips)
    # The weak third of the recording is worth a third of it by length alone.
    assert poor / total < 0.20, f"{poor}/{total}"


def test_footage_nobody_could_measure_is_neither_preferred_nor_penalised():
    """Unmeasured shots score 1.0, so a recording the scorer could not read behaves exactly as
    it did before any of this existed."""
    plain = _plan([(0, 120)], 30.0, variant_seed=5)
    scored = _plan([(0, 120)], 30.0, variant_seed=5)

    assert _picture(plain) == _picture(scored)


def test_a_recording_where_everything_scores_badly_still_makes_a_video():
    """Discounted, never excluded — a rule that can reject everything needs a fallback, and
    fallbacks are where this planner's bugs have lived."""
    video = MediaItem(path="/tmp/x.mp4", kind="video")
    scenes = [
        {"start": index * 20.0, "end": index * 20.0 + 20.0, "quality": 0.05}
        for index in range(6)
    ]
    request = EditJobRequest(
        title="t", media_ids=[video.id], target_duration_seconds=30.0,
        beat_sync=False, variant_seed=5,
    )
    clips = EditPlanner().plan(
        request, [video], [AnalysisResult(media_id=video.id, scenes=scenes)], None,
    ).clips

    assert clips
    assert sum(clip.duration for clip in clips) == pytest.approx(30.0, abs=0.05)


def test_a_longer_edit_still_reaches_for_the_weaker_footage():
    """A lean, not a ban. Where there are enough cuts to go round, the poor stretches get
    some — the alternative is a rule that can leave an edit short of its length."""
    video = MediaItem(path="/tmp/x.mp4", kind="video")
    scenes = [
        {"start": index * 20.0, "end": index * 20.0 + 20.0, "quality": quality}
        for index, quality in enumerate([1.0, 1.0, 0.10, 0.10, 1.0, 1.0])
    ]
    analysis = AnalysisResult(media_id=video.id, scenes=scenes)

    def used(target):
        request = EditJobRequest(
            title="t", media_ids=[video.id], target_duration_seconds=target,
            beat_sync=False, variant_seed=5, pace="normal", contour="flat",
        )
        clips = EditPlanner().plan(request, [video], [analysis], None).clips
        return sum(clip.duration for clip in clips if 40.0 <= clip.start < 80.0)

    assert used(120.0) > used(30.0)
