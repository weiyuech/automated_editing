"""The blank timeline an edit is poured into.

Durations are decided here, from the running time and the chosen pace and contour, before any
footage is looked at. Everything downstream fills slots that already know how long they are.

The old order decided a cut's length while walking the source, which made length a property of
the footage rather than of the edit. Two consequences followed: a cut could not be shaped by
where it sat in the finished video, and the totals had to be renegotiated afterwards with
per-kind fills and top-ups — the machinery that carried every ordering bug this planner has
had. Slots sum to the running time by construction, so none of that is needed.
"""

from __future__ import annotations

import random
from collections.abc import Sequence

# Mean seconds per cut. The pace sets this; the contour varies cuts around it.
PACE_SECONDS: dict[str, float] = {"fast": 2.0, "normal": 5.0, "cinematic": 8.0}
PACE_LEVELS: tuple[str, ...] = ("fast", "normal", "cinematic")

# A cut shorter than this is a flash rather than a shot, whatever the contour asks for.
MIN_SLOT_SECONDS = 0.6
# A place needs roughly two cuts to register as somewhere you have been, which is what makes
# `k_cap = N/2` and therefore makes full coverage always reachable at any pace.
CUTS_PER_PLACE = 2

CONTOUR_LEVELS: tuple[str, ...] = ("flat", "accelerate", "decelerate", "arc", "follow_energy")
# Contours needing nothing but the position in the edit. `follow_energy` is absent because it
# is read from the music instead, and falls back to `arc` when there is no music to read.
ANALYTIC_CONTOURS: tuple[str, ...] = ("flat", "accelerate", "decelerate", "arc")

# Shape values are relative: only their ratios matter, since they are normalised to the running
# time. 0.4..1.6 gives a four-to-one spread between the longest and shortest cut, which reads
# as deliberate pacing rather than as a glitch.
_LOW, _HIGH = 0.4, 1.6


def contour_shape(contour: str, position: float) -> float:
    """How long the cut at `position` (0 at the start of the edit, 1 at the end) wants to be."""
    span = _HIGH - _LOW
    if contour == "accelerate":
        # Opens on held shots and tightens: the edit gathers speed.
        return _HIGH - span * position
    if contour == "decelerate":
        return _LOW + span * position
    if contour == "arc":
        # Held at both ends, quickest in the middle — a piece with a beginning and an end.
        return _LOW + span * abs(2.0 * position - 1.0)
    return 1.0


def slot_count(total_seconds: float, pace: str) -> int:
    """How many cuts a running time holds at this pace."""
    mean = PACE_SECONDS.get(pace, PACE_SECONDS["normal"])
    return max(1, round(total_seconds / mean))


def place_capacity(total_seconds: float, pace: str) -> int:
    """Most places an edit of this length can show without glimpsing them.

    Derived from the slot count rather than from a separate rule, so the two can never
    disagree: at `CUTS_PER_PLACE` cuts each, a place always gets enough of the edit to
    register.
    """
    return max(1, slot_count(total_seconds, pace) // CUTS_PER_PLACE)


def build_slots(
    total_seconds: float,
    pace: str,
    contour: str,
    beats: Sequence[float] | None = None,
    energy: Sequence[float] | None = None,
    longest_shot: float | None = None,
) -> list[float]:
    """The durations of every cut in the edit, in order, summing to `total_seconds`.

    `beats` and `energy` are both optional and independent. Without them the contour is
    analytic and the edit still has a shape; with them the cuts land on the music and can take
    their shape from it. Nothing else in the pipeline changes when either is missing.

    `longest_shot` is what the footage can actually supply in one unbroken take. A cut cannot
    span two shots, so asking for a longer one only produces a shorter one — at a slow pace,
    every cut in the edit, which is how a twenty-second target quietly became nineteen.
    """
    if total_seconds <= 0:
        return []
    count = slot_count(total_seconds, pace)
    weights = _weights(count, contour, energy)
    durations = _normalise(weights, total_seconds)
    if longest_shot:
        durations = _clamp(durations, longest_shot)
    if beats:
        # The ceiling travels into the snap rather than being applied before it. Snapping an
        # edge moves both the cut before it and the cut after, so a snap applied to already
        # clamped durations can put one back over the length of the take it has to come from.
        durations = snap_to_beats(durations, beats, total_seconds, longest_shot)
    return durations


def _clamp(durations: list[float], ceiling: float) -> list[float]:
    """Hold every cut within what one take can supply, giving the overflow to the cuts that
    still have room. The shape survives; only its extremes are pulled in."""
    if ceiling <= 0:
        return durations
    for _ in range(8):
        excess = sum(duration - ceiling for duration in durations if duration > ceiling)
        room = sum(ceiling - duration for duration in durations if duration < ceiling)
        if excess <= 1e-9 or room <= 1e-9:
            break
        share = min(1.0, excess / room)
        durations = [
            ceiling if duration >= ceiling else duration + (ceiling - duration) * share
            for duration in durations
        ]
    return [min(duration, ceiling) for duration in durations]


def _weights(count: int, contour: str, energy: Sequence[float] | None) -> list[float]:
    if contour == "follow_energy" and energy:
        # Loud passages get quick cuts, quiet ones get held shots.
        return [_HIGH - (_HIGH - _LOW) * _sample(energy, _position(index, count)) for index in range(count)]
    shape = "arc" if contour == "follow_energy" else contour
    return [contour_shape(shape, _position(index, count)) for index in range(count)]


def _position(index: int, count: int) -> float:
    """Where a cut sits in the edit, on 0..1. Sampled at the middle of the cut rather than at
    its start, so a contour is not lopsided by its first and last slot."""
    return (index + 0.5) / count if count > 0 else 0.0


def _sample(curve: Sequence[float], position: float) -> float:
    if not curve:
        return 0.5
    index = min(len(curve) - 1, max(0, int(position * len(curve))))
    return curve[index]


def _normalise(weights: list[float], total_seconds: float) -> list[float]:
    """Scale the shape to the running time, then hold every cut above the flash-frame floor.

    Raising a cut to the floor has to be paid for by the cuts that can afford it, or the edit
    silently runs long.
    """
    positive = [max(weight, 0.01) for weight in weights]
    scale = sum(positive)
    durations = [total_seconds * weight / scale for weight in positive]

    floor = min(MIN_SLOT_SECONDS, total_seconds / len(durations))
    deficit = sum(floor - duration for duration in durations if duration < floor)
    if deficit <= 0:
        return durations

    donors = sum(duration - floor for duration in durations if duration > floor)
    if donors <= 0:
        return [total_seconds / len(durations)] * len(durations)
    return [
        floor if duration < floor else duration - (duration - floor) * deficit / donors
        for duration in durations
    ]


def snap_to_beats(
    durations: list[float],
    beats: Sequence[float],
    total_seconds: float,
    ceiling: float | None = None,
) -> list[float]:
    """Move each cut point to the nearest beat, so cuts land on the music rather than near it.

    Cuts were previously beat-*sized* but not beat-*aligned*: lengths came from beat intervals
    while the cut points themselves fell wherever the previous cut ended. Landing on a beat was
    coincidence.

    A snap is refused when it would starve either neighbour below the floor, so the shape
    survives contact with an awkward beat grid instead of collapsing.
    """
    if len(durations) < 2 or not beats:
        return durations

    edges = [0.0]
    for duration in durations:
        edges.append(edges[-1] + duration)

    cap = ceiling if ceiling and ceiling > 0 else float("inf")
    ordered = sorted(beats)
    snapped = [0.0]
    for index in range(1, len(edges) - 1):
        remaining_slots = len(edges) - 1 - index
        low = max(snapped[-1] + MIN_SLOT_SECONDS, total_seconds - remaining_slots * cap)
        high = min(total_seconds - remaining_slots * MIN_SLOT_SECONDS, snapped[-1] + cap)
        if low > high:
            # The grid cannot honour both the floor and the ceiling here; the ceiling matters
            # more, because a cut longer than its take is simply cut short later anyway.
            low = high = min(max(edges[index], snapped[-1] + MIN_SLOT_SECONDS), snapped[-1] + cap)
        candidate = _nearest(ordered, edges[index])
        snapped.append(candidate if low <= candidate <= high else min(max(edges[index], low), high))
    snapped.append(total_seconds)
    return [snapped[index + 1] - snapped[index] for index in range(len(snapped) - 1)]


def _nearest(ordered: Sequence[float], value: float) -> float:
    from bisect import bisect_left

    position = bisect_left(ordered, value)
    if position == 0:
        return ordered[0]
    if position == len(ordered):
        return ordered[-1]
    before, after = ordered[position - 1], ordered[position]
    return before if value - before <= after - value else after


def split_slots(count: int, buckets: int, weights: Sequence[float] | None = None) -> list[int]:
    """Divide `count` cuts between `buckets`, in whole cuts, by the largest-remainder method.

    Slots are integers, which is the point of deciding durations first: dividing cuts between
    places is an integer partition with an obvious correctness condition, where dividing
    seconds was a floating-point negotiation that had to be reconciled afterwards.

    This is a *share*, not a promise. Whether a bucket can actually fill the cuts it is given
    depends on the length of those particular cuts, which is not known here — `assign_in_order`
    settles it when the cuts are handed out.
    """
    if buckets <= 0 or count <= 0:
        return [0] * max(0, buckets)
    shares = [max(0.0, share) for share in (list(weights) if weights else [1.0] * buckets)]
    if sum(shares) <= 0:
        shares = [1.0] * buckets

    scale = count / sum(shares)
    exact = [share * scale for share in shares]
    whole = [int(value) for value in exact]
    for index in sorted(
        range(buckets), key=lambda index: exact[index] - whole[index], reverse=True
    )[: count - sum(whole)]:
        whole[index] += 1
    return whole


def assign_in_order(
    portion: Sequence[float],
    held: Sequence[float],
    targets: Sequence[int],
    repeat: bool = False,
) -> list[int]:
    """Hand each cut, in order, to the group it should come from.

    Groups are asked for their share of the cuts, but a share is a target and not a promise:
    a group is left as soon as it has had its share **or** has no room for the cut in hand,
    whichever comes first. Counting alone cannot see the second case, because a share is
    estimated from the average cut while a shaped contour hands out the longest ones — which
    is how a run holding twelve seconds was given a twelve-second cut and an eight-second one.

    Walking in order is what keeps the contour honest: cut *i* of the grid stays cut *i* of the
    edit, so the shape reaches the screen in the order it was designed.
    """
    consumed = [0.0] * len(held)
    taken = [0] * len(held)
    assignment: list[int] = []
    group = 0
    for duration in portion:
        # A group is left once it has had its share, or once it has no usable footage left.
        # Not once the cut in hand is too long for it: a place holding two seconds should
        # contribute a two-second cut, and the placement shortens it. Refusing it there is
        # how a short place vanished from an edit meant to cover every place.
        while group < len(held) and (
            taken[group] >= targets[group] or held[group] - consumed[group] < MIN_SLOT_SECONDS
        ):
            group += 1
        chosen = group
        if chosen >= len(held):
            # Every share is spent; the remaining cuts go to whoever still has footage.
            chosen = next(
                (index for index in range(len(held)) if held[index] - consumed[index] >= MIN_SLOT_SECONDS),
                -1,
            )
        if chosen < 0:
            if not repeat:
                break
            consumed = [0.0] * len(held)
            taken = [0] * len(held)
            group, chosen = 0, 0
        assignment.append(chosen)
        consumed[chosen] += duration
        taken[chosen] += 1
    return assignment



def spread_gaps(slack: float, count: int, rng: random.Random | None) -> list[float]:
    """How much footage to skip before each cut, so the picks spread over the whole of it.

    Even gaps sample a group evenly; seeded gaps sample it differently for every output while
    still covering it end to end. This is the whole of what a seed does to the picture.

    Quality is deliberately *not* applied here. Gaps are decided before the walk begins, so
    they cannot know which stretch the cursor will be standing in when each is spent — an
    earlier attempt to weight them by quality moved the cuts around without avoiding anything,
    and poor footage still received exactly its share of the edit. Which footage is worth using
    is settled where the cuts are handed out, in `_groups`, not here.
    """
    if count <= 0 or slack <= 0:
        return [0.0] * max(0, count)
    if rng is None:
        return [slack / count] * count
    cuts = sorted(rng.random() for _ in range(count - 1))
    edges = [0.0, *cuts, 1.0]
    return [(edges[index + 1] - edges[index]) * slack for index in range(count)]
