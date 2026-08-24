"""Automatic editorial families and timeline-portfolio selection.

Customers choose an outcome, not the knobs below.  The resolved values are nevertheless kept
on each job and in its diagnostics so a developer can reproduce and explain every output.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from automated_video_editing_backend.core.models import EditJobRequest, EditTimeline

FAMILY_LABELS = {
    "showcase": "完整展示",
    "dynamic": "动感巡游",
    "immersive": "沉浸参观",
}

# Coherent regions of the old parameter space. Randomness chooses within a family; it never
# combines values that contradict the customer's requested outcome.
FAMILY_POLICIES = {
    "showcase": {
        "pace": ("normal", "normal", "cinematic"),
        "contour_music": ("arc", "decelerate", "follow_energy"),
        "contour_silent": ("arc", "decelerate", "flat"),
        "footage_mix": ("dwell_heavy", "dwell_heavy", "balanced"),
        "emphasis": ("coverage",),
        "point_scope": ("all",),
        "start_rotation": (0,),
    },
    "dynamic": {
        "pace": ("fast", "fast", "normal"),
        "contour_music": ("follow_energy", "arc", "accelerate"),
        "contour_silent": ("arc", "accelerate"),
        "footage_mix": ("transit_heavy", "transit_heavy", "balanced"),
        "emphasis": ("target",),
        "point_scope": (0.5, 2 / 3, 5 / 6, 1.0),
        # Opening a route in its middle produces a circular reordering of source time. Variety
        # now comes from intervals/pacing/music while every output follows its recording.
        "start_rotation": (0,),
    },
    "immersive": {
        "pace": ("cinematic", "cinematic", "normal"),
        "contour_music": ("flat", "decelerate", "arc"),
        "contour_silent": ("flat", "decelerate"),
        "footage_mix": ("balanced", "dwell_heavy"),
        "emphasis": ("coverage",),
        "point_scope": ("all",),
        "start_rotation": (0,),
    },
}


@dataclass
class TimelineCandidate:
    slot: int
    request: EditJobRequest
    timeline: EditTimeline
    score: float
    components: dict[str, float]
    preset: str
    music_id: str | None


def smart_family(slot: int, count: int, has_points: bool, has_music: bool) -> str:
    """Allocate a smart batch across feasible editorial families.

    Slot zero is deliberately the safest general recommendation. The remainder is a stable
    weighted cycle rather than independent random choices, so ten outputs cannot accidentally
    become ten dynamic edits.
    """
    if count <= 1:
        return "showcase" if has_points else ("dynamic" if has_music else "immersive")
    if has_points:
        cycle = ("showcase", "showcase", "dynamic", "immersive")
    elif has_music:
        cycle = ("dynamic", "immersive", "dynamic", "showcase")
    else:
        cycle = ("immersive", "showcase", "immersive", "dynamic")
    return cycle[slot % len(cycle)]


def resolve_family_policy(
    request: EditJobRequest,
    family: str,
    rng: random.Random,
    *,
    has_points: bool,
    has_music: bool,
) -> None:
    policy = FAMILY_POLICIES[family]
    request.editorial_preset = family
    request.pace = rng.choice(policy["pace"])
    request.contour = rng.choice(policy["contour_music" if has_music else "contour_silent"])
    request.footage_mix = rng.choice(policy["footage_mix"]) if has_points else "balanced"
    request.emphasis = rng.choice(policy["emphasis"]) if has_points else "target"
    request.point_scope = rng.choice(policy["point_scope"]) if has_points else "all"
    request.recording_scope = "all"
    request.start_rotation = rng.choice(policy["start_rotation"]) if has_points else 0


# A window whose measured visual motion is at or below this is treated as dead/frozen. It sits
# just above the shot scorer's MOTION_DEAD floor, so slow but real movement is never mistaken
# for static — the heartbeat telemetry (below) is what rescues the rare slow-pan-over-blank case.
STATIC_MOTION_DEAD = 0.1
# The most of a candidate's score staticness may remove. Applied as a multiplier so the weighted
# taste terms are left exactly as they were; relative selection then favours a live candidate.
STATIC_PENALTY = 0.5


def score_timeline(
    timeline: EditTimeline,
    analyses,
    preset: str,
    music_analysis=None,
) -> tuple[float, dict[str, float]]:
    scenes = {
        analysis.media_id: list(analysis.scenes)
        for analysis in analyses
    }
    total = sum(max(0.0, clip.duration) for clip in timeline.clips)
    target = max(
        0.001,
        float(
            timeline.planning_diagnostics.get(
                "effective_duration_seconds", timeline.target_duration_seconds
            )
        ),
    )
    duration = max(0.0, 1.0 - abs(total - target) / target)

    weighted_quality = 0.0
    clip_scenes: list[dict] = []
    for clip in timeline.clips:
        scene = _clip_evidence(clip, scenes.get(clip.media_id, []))
        clip_scenes.append(scene)
        quality = float(scene.get("quality", 0.5))
        weighted_quality += quality * clip.duration
    technical = weighted_quality / total if total > 0 else 0.0

    available_points = {
        str(scene.get("label"))
        for analysis in analyses for scene in analysis.scenes if scene.get("label")
    }
    used_points = {clip.label for clip in timeline.clips if clip.label}
    coverage = len(used_points & available_points) / len(available_points) if available_points else 1.0

    source_integrity, chronological_integrity, temporal_flow = _timeline_integrity(timeline)
    continuity = temporal_flow if chronological_integrity else 0.0
    source_fingerprints = {
        (clip.media_id, round(clip.start, 1), round(clip.duration, 1)) for clip in timeline.clips
    }
    source_reuse = len(source_fingerprints) / len(timeline.clips) if timeline.clips else 0.0
    visual_repetition = _visual_non_repetition(clip_scenes)
    repetition = 0.5 * source_reuse + 0.5 * visual_repetition
    visual_flow = _visual_flow(clip_scenes)
    cut_stability = _cut_stability(clip_scenes)
    evidence_coverage = (
        sum(clip.duration for clip, scene in zip(timeline.clips, clip_scenes) if scene)
        / total if total > 0 else 0.0
    )

    dwell = sum(clip.duration for clip in timeline.clips if clip.footage == "dwell")
    transit = sum(clip.duration for clip in timeline.clips if clip.footage == "transit")
    known = dwell + transit
    dwell_share = dwell / known if known else 0.5
    transit_share = transit / known if known else 0.5
    activity = _duration_weighted(timeline, clip_scenes, "motion", 0.5)
    steadiness = _duration_weighted(timeline, clip_scenes, "steadiness", 0.5)
    mean_cut = total / len(timeline.clips) if timeline.clips else target
    if preset == "showcase":
        fidelity = 0.50 * coverage + 0.30 * dwell_share + 0.20 * continuity
        weights = (0.25, 0.18, 0.10, 0.10, 0.12, 0.05, 0.12, 0.08)
    elif preset == "dynamic":
        pace_fit = math.exp(-abs(mean_cut - 2.5) / 3.0)
        fidelity = 0.40 * transit_share + 0.35 * pace_fit + 0.25 * activity
        weights = (0.08, 0.16, 0.07, 0.14, 0.18, 0.18, 0.09, 0.10)
    else:
        pace_fit = math.exp(-abs(mean_cut - 7.0) / 5.0)
        fidelity = 0.45 * continuity + 0.35 * pace_fit + 0.20 * steadiness
        weights = (0.12, 0.18, 0.18, 0.08, 0.18, 0.04, 0.14, 0.08)

    music = _music_alignment(timeline, music_analysis)
    values = (
        coverage, technical, continuity, repetition, fidelity, music, visual_flow, cut_stability,
    )
    score = sum(weight * value for weight, value in zip(weights, values))
    components = {
        "coverage": round(coverage, 4),
        "technical": round(technical, 4),
        "continuity": round(continuity, 4),
        "non_repetition": round(repetition, 4),
        "preset_fidelity": round(fidelity, 4),
        "music_alignment": round(music, 4),
        "visual_flow": round(visual_flow, 4),
        "cut_stability": round(cut_stability, 4),
        "duration_accuracy": round(duration, 4),
        # These are constraints/diagnostics, not aesthetic weights. Batch creation asserts
        # both before a candidate can be selected.
        "source_integrity": round(source_integrity, 4),
        "chronological_integrity": round(chronological_integrity, 4),
        "temporal_flow": round(temporal_flow, 4),
        "analysis_coverage": round(evidence_coverage, 4),
    }
    # A frozen shot passes every weighted taste term (a locked-off frame is sharp, well exposed
    # and maximally "steady"), so staticness is applied as a layered multiplier, not a weight:
    # lively footage keeps ~1.0 and nothing else in the sum is touched, while a static-heavy
    # candidate is pulled down so a live one wins the slot. Built from the per-window motion the
    # analysis already computes; footage with no motion evidence is left unchanged.
    static_fraction = _timeline_static_fraction(timeline, scenes)
    liveliness = round(1.0 - STATIC_PENALTY * static_fraction, 6)
    components["static_fraction"] = round(static_fraction, 4)
    components["liveliness"] = liveliness
    # Duration is a universal correctness term rather than a preset-specific taste weight.
    return round((0.9 * score + 0.1 * duration) * liveliness, 6), components


def _clip_evidence(clip, scenes: list[dict]) -> dict:
    """Local measurements for the exact source moment used by one timeline clip."""
    candidates = [
        scene for scene in scenes
        if float(scene.get("start", 0.0)) <= clip.start + 1e-6
        and clip.start < float(scene.get("end", 0.0)) - 1e-6
    ]
    if not candidates:
        return {}
    scene = max(candidates, key=lambda item: float(item.get("quality", 0.5)))
    profiles = [
        item for item in (scene.get("quality_profile") or [])
        if float(item.get("start", 0.0)) <= clip.start + 1e-6
        and clip.start < float(item.get("end", 0.0)) - 1e-6
    ]
    local = max(profiles, key=lambda item: float(item.get("quality", 0.5)), default={})
    evidence = dict(scene)
    evidence.update(local)
    evidence["_at_detected_boundary"] = bool(
        scene.get("from_scene_detector") or "boundary_score" in scene
    ) and abs(clip.start - float(scene.get("start", 0.0))) <= 0.08
    return evidence


def _clip_static_fraction(clip, scenes: list[dict]) -> float | None:
    """Share of one clip's span whose measured visual motion is dead.

    Reads the per-window ``quality_profile`` across the clip's whole span (not just its start),
    so a frozen patch inside an otherwise-live clip is counted rather than hidden behind its
    liveliest window — which is what ``_clip_evidence``'s ``max()`` does. Falls back to the
    clip's scene-level motion when no sub-window profile exists, and returns ``None`` when there
    is no motion evidence at all.
    """
    clip_start = clip.start
    clip_end = clip.start + max(0.0, clip.duration)
    covered = 0.0
    dead = 0.0
    for scene in scenes:
        for window in scene.get("quality_profile") or []:
            overlap = max(
                0.0,
                min(clip_end, float(window.get("end", 0.0)))
                - max(clip_start, float(window.get("start", 0.0))),
            )
            if overlap <= 0.0:
                continue
            covered += overlap
            if float(window.get("motion", 1.0)) <= STATIC_MOTION_DEAD:
                dead += overlap
    if covered > 0.0:
        return dead / covered
    evidence = _clip_evidence(clip, scenes)
    if "motion" in evidence:
        return 1.0 if float(evidence["motion"]) <= STATIC_MOTION_DEAD else 0.0
    return None


def _timeline_static_fraction(timeline: EditTimeline, scenes: dict) -> float:
    """Duration-weighted share of the timeline that is visually dead.

    Only clips that carry motion evidence contribute, so imported footage with no analysis
    neither penalises nor dilutes — those timelines score exactly as before.
    """
    total = 0.0
    dead = 0.0
    for clip in timeline.clips:
        duration = max(0.0, clip.duration)
        if duration <= 0.0:
            continue
        fraction = _clip_static_fraction(clip, scenes.get(clip.media_id, []))
        if fraction is None:
            continue
        total += duration
        dead += duration * fraction
    return dead / total if total > 0.0 else 0.0


def _timeline_integrity(timeline: EditTimeline) -> tuple[float, float, float]:
    clips = timeline.clips
    if not clips:
        return 0.0, 0.0, 0.0
    source_integrity = 1.0 if len({clip.media_id for clip in clips}) == 1 else 0.0
    chronological = source_integrity == 1.0 and all(
        first.start + first.duration <= second.start + 1e-6
        for first, second in pairwise(clips)
    )
    if len(clips) < 2:
        flow = 1.0
    else:
        values = []
        for first, second in pairwise(clips):
            if first.media_id != second.media_id or second.start + 1e-6 < first.start + first.duration:
                values.append(0.0)
                continue
            gap = max(0.0, second.start - (first.start + first.duration))
            scale = max(1.0, (first.duration + second.duration) / 2.0)
            values.append(math.exp(-gap / (3.0 * scale)))
        # Forward order is the contract. Smaller skipped gaps are an aesthetic preference,
        # not a requirement, so even a wide but chronological selection keeps half credit.
        flow = 0.5 + 0.5 * (sum(values) / len(values) if values else 1.0)
    return source_integrity, (1.0 if chronological else 0.0), flow if chronological else 0.0


def _duration_weighted(timeline, evidence: list[dict], field: str, neutral: float) -> float:
    total = sum(max(0.0, clip.duration) for clip in timeline.clips)
    if total <= 0:
        return neutral
    return sum(
        max(0.0, clip.duration) * float(scene.get(field, neutral))
        for clip, scene in zip(timeline.clips, evidence)
    ) / total


def _visual_non_repetition(scenes: list[dict]) -> float:
    """How many visually distinct perceptual-hash clusters survive in the timeline."""
    hashes = [int(scene["fingerprint"]) for scene in scenes if "fingerprint" in scene]
    if len(hashes) < 2:
        return 0.5
    representatives: list[int] = []
    for value in hashes:
        if not any((value ^ prior).bit_count() <= 8 for prior in representatives):
            representatives.append(value)
    return len(representatives) / len(hashes)


def _visual_flow(scenes: list[dict]) -> float:
    """Compatibility between adjacent colour/motion states, not sameness of content."""
    pairs = []
    for left, right in pairwise(scenes):
        left_colour = left.get("colour") or []
        right_colour = right.get("colour") or []
        if len(left_colour) == 3 and len(right_colour) == 3:
            distance = math.sqrt(sum((float(a) - float(b)) ** 2 for a, b in zip(left_colour, right_colour)))
            colour = math.exp(-distance / 45.0)
        else:
            colour = 0.5
        if "motion" in left and "motion" in right:
            motion = max(0.0, 1.0 - abs(float(left["motion"]) - float(right["motion"])))
        else:
            motion = 0.5
        pairs.append(0.7 * colour + 0.3 * motion)
    return sum(pairs) / len(pairs) if pairs else 0.5


def _cut_stability(scenes: list[dict]) -> float:
    if len(scenes) < 2:
        return float(scenes[0].get("steadiness", 0.5)) if scenes else 0.5
    values = []
    for left, right in pairwise(scenes):
        steadiness = (
            float(left.get("steadiness", 0.5)) + float(right.get("steadiness", 0.5))
        ) / 2.0
        # Boundary confidence belongs only to a clip that really starts at a detector cut.
        # Robot timestamps and sampled positions inside one long take fall back to local
        # steadiness rather than inheriting a scene-level score they did not earn.
        boundary = (
            float(right.get("boundary_score", steadiness))
            if right.get("_at_detected_boundary") else steadiness
        )
        values.append(0.65 * steadiness + 0.35 * boundary)
    return sum(values) / len(values)


def _music_alignment(timeline: EditTimeline, music) -> float:
    if (
        music is None or music.evidence != "structured"
        or not music.beats or len(timeline.clips) < 2
    ):
        return 0.5
    start = timeline.music_start_seconds
    duration = timeline.music_duration_seconds or timeline.target_duration_seconds
    beats = [value - start for value in music.beats if start <= value <= start + duration]
    accents = [
        value - start for value in music.accent_times
        if start <= value <= start + duration
    ]
    events = beats if timeline.editorial_preset != "dynamic" else sorted({*beats, *accents})
    if not events:
        return 0.25
    edges, cursor = [], 0.0
    for clip in timeline.clips[:-1]:
        cursor += clip.duration
        edges.append(cursor)
    matches = [
        math.exp(-((min(abs(edge - event) for event in events) / 0.14) ** 2))
        for edge in edges
    ]
    return sum(matches) / len(matches) if matches else 0.5


def diversity(left: TimelineCandidate, right: TimelineCandidate) -> float:
    left_intervals = _interval_bins(left.timeline)
    right_intervals = _interval_bins(right.timeline)
    union = left_intervals | right_intervals
    source = 1.0 - (len(left_intervals & right_intervals) / len(union) if union else 1.0)

    left_points = {clip.label for clip in left.timeline.clips if clip.label}
    right_points = {clip.label for clip in right.timeline.clips if clip.label}
    point_union = left_points | right_points
    points = 1.0 - (len(left_points & right_points) / len(point_union) if point_union else 1.0)

    opening = 1.0 if _opening(left.timeline) != _opening(right.timeline) else 0.0
    preset = 1.0 if left.preset != right.preset else 0.0
    music = 0.0
    if left.music_id != right.music_id:
        music = 1.0
    elif left.timeline.music_duration_seconds:
        gap = abs(left.timeline.music_start_seconds - right.timeline.music_start_seconds)
        music = min(1.0, gap / max(1.0, left.timeline.music_duration_seconds))
    return 0.45 * source + 0.20 * points + 0.15 * opening + 0.10 * preset + 0.10 * music


def select_slot_candidate(
    candidates: list[TimelineCandidate], selected: list[TimelineCandidate],
) -> TimelineCandidate:
    if not selected:
        return max(candidates, key=lambda item: item.score)
    best_quality = max(item.score for item in candidates)
    eligible = [item for item in candidates if item.score >= best_quality - 0.15] or candidates
    non_duplicates = [
        item for item in eligible
        if all(diversity(item, prior) > 1e-6 for prior in selected)
    ]
    if non_duplicates:
        eligible = non_duplicates
    return max(
        eligible,
        key=lambda item: 0.75 * item.score + 0.25 * min(diversity(item, prior) for prior in selected),
    )


def _interval_bins(timeline: EditTimeline) -> set[tuple[str, int]]:
    bins: set[tuple[str, int]] = set()
    for clip in timeline.clips:
        first = math.floor(clip.start)
        last = max(first, math.ceil(clip.start + clip.duration) - 1)
        bins.update((clip.media_id, second) for second in range(first, last + 1))
    return bins


def _opening(timeline: EditTimeline) -> tuple[Any, ...]:
    if not timeline.clips:
        return ()
    clip = timeline.clips[0]
    return clip.media_id, clip.label, round(clip.start)
