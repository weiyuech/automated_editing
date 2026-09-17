# Archived camerawork strategy: directional v1

This document preserves the automatic-camerawork strategy that was retired on 2026-09-16.
It is historical reference only. The application and the standalone robot console no longer
run this policy.

## Former action weights

Each travel-time action used to be selected independently:

- 50%: one adaptive yaw/pitch move;
- 30%: a two-leg left/right ping-pong move;
- 20%: return once to the saved anchor, then immediately select another action.

Those were per-action probabilities, not measured shares of recording time. A long sweep and a
short anchor return counted as one draw each, so the resulting video could not reliably spend
50%, 30%, and 20% of its time in those states.

## Former horizontal-direction rule

Yaw follows the robot protocol: positive is left and negative is right. When the current yaw was
positive, the adaptive branch normally moved broadly to the right; when it was negative, the
rule was mirrored.

- 80% branch: move 60%–90% of the available distance toward the opposite side;
- 20% branch: continue 10%–30% farther toward the current side;
- at yaw 0°, choose a side and move 40%–80% of that side's available distance;
- if outward room was at most 10° and inward room was greater than 10°, force the inward branch.

The two-leg ping-pong action generated separate left and right endpoints and did not use those
distance fractions.

## Former region check

The configured yaw and pitch ranges were split at their midpoints. After the directional rule
had already selected yaw, a helper kept that yaw and changed pitch only when needed to cross at
least one midpoint. It therefore did **not** choose uniformly among the other three regions.

## Archived source excerpt

The following is the retired selector code from commit `e460db6`. It is kept here as inert
Markdown reference; production does not import or execute it. The complete original file can be
recovered with:

```bash
git show e460db6:backend/src/automated_video_editing_backend/services/cruise.py
```

```python
_CAMERAWORK_MODES = (
    ("wander", 50.0),
    ("pingpong", 30.0),
    ("anchor", 20.0),
)
_CW_OPPOSITE_PROB = 0.80
_CW_OPPOSITE_DISTANCE = (0.60, 0.90)
_CW_OUTWARD_DISTANCE = (0.10, 0.30)
_CW_CENTER_DISTANCE = (0.40, 0.80)
_CW_DIRECTION_ROOM_DEG = 10.0

def _pick_camerawork_mode(self) -> str:
    roll = random.uniform(0.0, sum(weight for _name, weight in _CAMERAWORK_MODES))
    cumulative = 0.0
    for name, weight in _CAMERAWORK_MODES:
        cumulative += weight
        if roll <= cumulative:
            return name
    return _CAMERAWORK_MODES[0][0]

def _adaptive_yaw_target(self, current, config):
    low, high = config.yaw_min, config.yaw_max
    current = _clamp(current, low, high)
    if current > 0:  # physically left
        inward_room = current - low
        outward_room = high - current
        choose_opposite = inward_room > _CW_DIRECTION_ROOM_DEG and (
            outward_room <= _CW_DIRECTION_ROOM_DEG
            or random.random() < _CW_OPPOSITE_PROB
        )
        if choose_opposite:
            distance = max(1.0, random.uniform(*_CW_OPPOSITE_DISTANCE) * inward_room)
            return round(_clamp(current - distance, low, high))
        distance = max(1.0, random.uniform(*_CW_OUTWARD_DISTANCE) * outward_room)
        return round(_clamp(current + distance, low, high))

    if current < 0:  # physically right
        inward_room = high - current
        outward_room = current - low
        choose_opposite = inward_room > _CW_DIRECTION_ROOM_DEG and (
            outward_room <= _CW_DIRECTION_ROOM_DEG
            or random.random() < _CW_OPPOSITE_PROB
        )
        if choose_opposite:
            distance = max(1.0, random.uniform(*_CW_OPPOSITE_DISTANCE) * inward_room)
            return round(_clamp(current + distance, low, high))
        distance = max(1.0, random.uniform(*_CW_OUTWARD_DISTANCE) * outward_room)
        return round(_clamp(current - distance, low, high))

    right_room = current - low
    left_room = high - current
    choose_right = right_room > _CW_DIRECTION_ROOM_DEG and (
        left_room <= _CW_DIRECTION_ROOM_DEG or random.random() < 0.5
    )
    if choose_right:
        distance = max(1.0, random.uniform(*_CW_CENTER_DISTANCE) * right_room)
        return round(_clamp(current - distance, low, high))
    distance = max(1.0, random.uniform(*_CW_CENTER_DISTANCE) * left_room)
    return round(_clamp(current + distance, low, high))

def _pingpong_poses(self, current_yaw, config):
    midpoint = (config.yaw_min + config.yaw_max) / 2.0
    quarter = (config.yaw_max - config.yaw_min) * 0.25
    right_high = min(0.0, midpoint - quarter)
    if right_high <= config.yaw_min:
        right_high = config.yaw_min + quarter
    left_low = max(0.0, midpoint + quarter)
    if left_low >= config.yaw_max:
        left_low = config.yaw_max - quarter
    right = (
        round(random.uniform(config.yaw_min, right_high)),
        random.randint(config.pitch_min, config.pitch_max),
    )
    left = (
        round(random.uniform(left_low, config.yaw_max)),
        random.randint(config.pitch_min, config.pitch_max),
    )
    if current_yaw > 0:
        return right, left
    if current_yaw < 0:
        return left, right
    return (right, left) if random.random() < 0.5 else (left, right)
```

## Why it was replaced

The policy combined three separate probability layers, special edge handling, ping-pong state,
and a post-selection region repair. That made the actual camera rhythm difficult to predict and
the advertised 50/30/20 values easy to misread as time percentages.

The active policy is intentionally smaller: choose a yaw×pitch quadrant, choose the next one
uniformly from the other three, and use the anchor as one separately timed fifth state. See the
root README and `docs/cruise_capture.md` for the current behavior.
