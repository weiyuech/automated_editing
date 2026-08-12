"""The planner's invariants, asserted across the whole configuration space rather than at
hand-picked points.

Example-based tests check the cases someone thought of. This one generates them — every pace,
contour, mix, emphasis, scope and rotation, against footage shaped to be awkward on purpose:
sub-second shots, wildly uneven points, routes that double back, spans running past the end of
the file. It has found two ordering bugs that the example tests missed, both because their
data was too tidy: a route that revisits a point, and a point reached on a second attempt.

Kept deliberately fast enough to run with everything else.
"""
import random
import sys

from automated_video_editing_backend.core.models import (
    EDIT_CONTOUR_LEVELS, EDIT_EMPHASIS_LEVELS, EDIT_PACE_LEVELS,
    FOOTAGE_MIX_LEVELS, AnalysisResult, EditJobRequest, MediaItem,
)
from automated_video_editing_backend.services.timeline import EditPlanner

FAILURES = []


def note(case, msg):
    FAILURES.append(f"{case}: {msg}")


def random_scenes(rng):
    """A cruise run, or an ordinary import, with awkward shapes on purpose."""
    style = rng.choice(["cruise", "cruise_uneven", "plain", "tiny", "one_shot"])
    if style == "plain":
        n = rng.randint(1, 6)
        cur, out = 0.0, []
        for _ in range(n):
            span = rng.uniform(1.0, 40.0)
            out.append({"start": cur, "end": cur + span})
            cur += span
        return out
    if style == "one_shot":
        return [{"start": 0.0, "end": rng.uniform(5.0, 300.0)}]
    if style == "tiny":
        return [{"start": i * 1.0, "end": i * 1.0 + rng.uniform(0.6, 1.2),
                 "kind": "dwell", "label": f"p{i}"} for i in range(rng.randint(2, 8))]
    points = rng.randint(1, 14)
    # Routes that come back to a point they already filmed. Labels then repeat
    # non-consecutively, which is what broke the ordering when places were pooled by name.
    order = [rng.randint(1, max(1, points // 2)) if rng.random() < 0.3 else p
             for p in range(1, points + 1)]
    cur, out = 0.0, []
    for p in order:
        t = rng.uniform(0.8, 30.0) if style == "cruise_uneven" else 12.0
        d = rng.uniform(0.8, 20.0) if style == "cruise_uneven" else 8.0
        kind = rng.choice(["transit", "failed", "skipped"]) if style == "cruise_uneven" else "transit"
        out.append({"start": cur, "end": cur + t, "kind": kind, "label": f"p{p}"})
        out.append({"start": cur + t, "end": cur + t + d, "kind": "dwell", "label": f"p{p}"})
        cur += t + d
    return out


def check(case, tl, scenes, target, vo, req_rotation=0):
    clips = tl.clips
    if not clips:
        total = sum(s["end"] - s["start"] for s in scenes)
        if total > 2.0 and target > 2.0:
            note(case, f"no clips at all (footage {total:.1f}s, target {target}s)")
        return

    total = sum(c.duration for c in clips)
    ceiling = max(target, vo or 0.0)
    if total > ceiling + 0.05:
        note(case, f"ran long: {total:.2f}s > {ceiling:.2f}s")

    # The planner scopes before it measures, so whether it looped is its own call, not one
    # the caller can recompute from the raw scene list.
    looping = any("画面循环" in w for w in tl.warnings)
    spans = [(round(c.start, 4), round(c.duration, 4), c.source_path) for c in clips]
    if not looping and len(spans) != len(set(spans)):
        note(case, "duplicate clip while not looping")
    if looping:
        # Repeating the picture is the whole point when a narration outlasts the footage, so
        # the ordering rules below do not apply.
        return

    for c in clips:
        if c.duration <= 0:
            note(case, f"non-positive duration {c.duration}")
        host = [s for s in scenes if s["start"] - 1e-6 <= c.start < s["end"] + 1e-6]
        if not host:
            note(case, f"clip start {c.start:.2f} outside every shot")
        elif c.start + c.duration > max(h["end"] for h in host) + 1e-4:
            note(case, f"clip {c.start:.2f}+{c.duration:.2f} overruns its shot end")

    # With no rotation the edit must run forward end to end. Rotation is the one dimension
    # whose purpose is to break that, so it is excluded rather than excused.
    if getattr(req_rotation, "value", req_rotation) == 0:
        starts = [c.start for c in clips]
        if any(starts[i] > starts[i + 1] + 1e-6 for i in range(len(starts) - 1)):
            note(case, "output runs backwards without rotation")




def main(iterations=1200):
    rng = random.Random(20260808)
    for i in range(iterations):
        scenes = random_scenes(rng)
        target = rng.choice([5.0, 10.0, 15.0, 30.0, 60.0, 120.0, 180.0])
        vo = rng.choice([None, None, None, 8.0, 45.0, 200.0])
        beats = sorted(rng.uniform(0, 300) for _ in range(rng.randint(0, 200))) if rng.random() < 0.5 else []
        energy = [rng.random() for _ in range(64)] if rng.random() < 0.5 else []
        video = MediaItem(path="/tmp/s.mp4", kind="video")
        music = MediaItem(path="/tmp/m.mp3", kind="audio") if beats else None
        req = EditJobRequest(
            title="t", media_ids=[video.id],
            music_media_id=music.id if music else None,
            target_duration_seconds=target,
            beat_sync=bool(beats),
            variant_seed=rng.choice([None, rng.randrange(2**31)]),
            pace=rng.choice(EDIT_PACE_LEVELS),
            contour=rng.choice(EDIT_CONTOUR_LEVELS),
            footage_mix=rng.choice(FOOTAGE_MIX_LEVELS),
            emphasis=rng.choice(EDIT_EMPHASIS_LEVELS),
            point_scope=rng.choice(["all", 0.5, 2 / 3, 5 / 6, 1.0]),
            recording_scope="all",
            start_rotation=rng.randrange(4),
        )
        analysis = AnalysisResult(media_id=video.id, scenes=scenes, beats=beats, energy=energy)
        case = (f"#{i} pace={req.pace} cont={req.contour} mix={req.footage_mix} "
                f"emph={req.emphasis} scope={req.point_scope} rot={req.start_rotation} "
                f"T={target} vo={vo} shots={len(scenes)} beats={len(beats)}")
        try:
            tl = EditPlanner().plan(req, [video], [analysis], music, None, voiceover_duration=vo)
        except Exception as exc:
            note(case, f"RAISED {type(exc).__name__}: {exc}")
            continue
        check(case, tl, scenes, target, vo, req.start_rotation)

    print(f"ran {iterations} configurations")
    if not FAILURES:
        print("no invariant violations")
        return 0
    from collections import Counter
    kinds = Counter(f.split(": ", 1)[1].split(":")[0].split(" (")[0] for f in FAILURES)
    print(f"{len(FAILURES)} violations across {len(kinds)} kinds:")
    for kind, n in kinds.most_common():
        print(f"  {n:5}  {kind}")
    print("\nexamples:")
    for f in FAILURES[:6]:
        print("  " + f)
    return 1


if __name__ == "__main__":
    sys.exit(main())


def test_the_planner_holds_its_invariants_across_the_configuration_space():
    """Fails with the offending configuration named, so a regression says which one."""
    FAILURES.clear()
    main()
    assert not FAILURES, "\n".join(FAILURES[:10])
