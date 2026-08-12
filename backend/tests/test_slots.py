import pytest

from automated_video_editing_backend.services import slots


def test_the_levels_the_api_offers_are_the_levels_the_grid_implements():
    """`slots` deliberately knows nothing about the request models, so the two lists of levels
    are written twice and could drift apart in silence — an API accepting a pace the grid has
    no length for would fall back to `normal` and look like nothing happened."""
    from automated_video_editing_backend.core.models import EDIT_CONTOUR_LEVELS, EDIT_PACE_LEVELS

    assert list(slots.PACE_LEVELS) == list(EDIT_PACE_LEVELS)
    assert list(slots.CONTOUR_LEVELS) == list(EDIT_CONTOUR_LEVELS)
    assert set(slots.PACE_SECONDS) == set(EDIT_PACE_LEVELS)
    assert set(slots.ANALYTIC_CONTOURS) == set(EDIT_CONTOUR_LEVELS) - {"follow_energy"}


def test_pace_sets_how_many_cuts_an_edit_has():
    assert slots.slot_count(30.0, "fast") > slots.slot_count(30.0, "normal")
    assert slots.slot_count(30.0, "normal") > slots.slot_count(30.0, "cinematic")


def test_slots_always_add_up_to_the_running_time():
    for pace in slots.PACE_LEVELS:
        for contour in slots.ANALYTIC_CONTOURS:
            for total in (7.0, 15.0, 30.0, 90.0):
                grid = slots.build_slots(total, pace, contour)
                assert sum(grid) == pytest.approx(total, abs=1e-6), (pace, contour, total)
                assert all(duration > 0 for duration in grid)


def test_contours_shape_the_edit_rather_than_just_its_average():
    flat = slots.build_slots(60.0, "normal", "flat")
    accelerate = slots.build_slots(60.0, "normal", "accelerate")
    decelerate = slots.build_slots(60.0, "normal", "decelerate")
    arc = slots.build_slots(60.0, "normal", "arc")

    assert len(set(round(duration, 6) for duration in flat)) == 1
    # Accelerating opens on held shots and tightens; decelerating does the reverse.
    assert accelerate[0] > accelerate[-1]
    assert decelerate[0] < decelerate[-1]
    # An arc is held at both ends and quickest in the middle.
    assert arc[0] > arc[len(arc) // 2] < arc[-1]


def test_a_loud_passage_gets_quicker_cuts():
    """`follow_energy` takes its shape from the track: the cuts tighten where it does."""
    quiet_then_loud = [0.0] * 32 + [1.0] * 32
    grid = slots.build_slots(60.0, "normal", "follow_energy", energy=quiet_then_loud)

    assert grid[0] > grid[-1]
    assert sum(grid) == pytest.approx(60.0, abs=1e-6)


def test_cut_points_move_onto_the_beat():
    beats = [index * 0.75 for index in range(80)]
    grid = slots.build_slots(30.0, "normal", "flat", beats=beats)

    edge = 0.0
    for duration in grid[:-1]:
        edge += duration
        assert min(abs(edge - beat) for beat in beats) < 1e-6, edge
    assert sum(grid) == pytest.approx(30.0, abs=1e-6)


def test_an_awkward_beat_grid_does_not_collapse_a_cut():
    """A snap that would starve either neighbour is refused, so the shape survives contact
    with a beat grid that does not suit it."""
    grid = slots.build_slots(30.0, "fast", "flat", beats=[0.0, 0.05, 0.1, 29.9, 29.95])

    assert sum(grid) == pytest.approx(30.0, abs=1e-6)
    assert all(duration >= slots.MIN_SLOT_SECONDS - 1e-9 for duration in grid), grid


def test_coverage_is_reachable_at_every_pace():
    """`k_cap = N / CUTS_PER_PLACE` is not a rule of thumb — it is exactly the condition that
    lets every place in scope get its cuts, whatever the pace."""
    for pace in slots.PACE_LEVELS:
        for total in (10.0, 30.0, 120.0):
            count = slots.slot_count(total, pace)
            assert slots.place_capacity(total, pace) * slots.CUTS_PER_PLACE <= max(count, slots.CUTS_PER_PLACE)


def test_a_group_is_never_handed_cuts_it_cannot_fill():
    """Handing a place more cuts than its footage can fill is what made a two-second place
    show the same two seconds twice. Shares are decided by count; whether they fit is decided
    here, against the length of the particular cuts."""
    # Three groups asked for two cuts each, but the middle one holds two seconds.
    assignment = slots.assign_in_order([5.0] * 6, held=[60.0, 2.0, 60.0], targets=[2, 2, 2])

    assert len(assignment) == 6
    # It still appears — a short place contributes a short cut rather than vanishing.
    assert 1 in assignment
    # And it is not asked for a second cut it has no room for.
    assert assignment.count(1) == 1


def test_a_group_spends_what_it_has_and_hands_the_rest_on():
    """A group keeps taking cuts while it has usable footage left, and the last of them is
    shortened to whatever remains — twelve seconds yields a ten and a two, not one ten and
    two seconds thrown away. What it cannot cover goes to the next group."""
    assignment = slots.assign_in_order([10.0] * 4, held=[12.0, 40.0], targets=[3, 1])

    assert assignment.count(0) == 2
    assert assignment.count(1) == 2
    # Never out of order: a group is finished with before the next one starts.
    assert assignment == sorted(assignment)


def test_cuts_are_dropped_rather_than_replayed_when_the_footage_runs_out():
    """A slightly shorter edit beats one that jumps backwards. Repeating happens only where
    repeating is the point — a narration outlasting everything filmed."""
    assignment = slots.assign_in_order([10.0] * 5, held=[12.0], targets=[5], repeat=False)
    assert assignment == [0, 0]

    looped = slots.assign_in_order([10.0] * 5, held=[12.0], targets=[5], repeat=True)
    assert len(looped) == 5


def test_split_always_spends_every_cut_when_there_is_room():
    for count in range(1, 30):
        for buckets in range(1, 7):
            parts = slots.split_slots(count, buckets)
            assert sum(parts) == count, (count, buckets)
            # Largest remainder: no two parts differ by more than one.
            assert max(parts) - min(parts) <= 1


def test_gaps_spread_the_picks_and_a_seed_spreads_them_differently():
    import random

    even = slots.spread_gaps(40.0, 4, None)
    assert even == [10.0, 10.0, 10.0, 10.0]

    seeded = {tuple(slots.spread_gaps(40.0, 4, random.Random(seed))) for seed in range(8)}
    assert len(seeded) == 8
    for gaps in seeded:
        assert sum(gaps) == pytest.approx(40.0, abs=1e-9)
