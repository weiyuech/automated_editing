"""What each shot is worth, measured rather than assumed.

Selection used to be purely positional, so a shot that was out of focus, blown out, filmed
mid-lurch or completely dead was as likely to be used as a good one. These are the cheapest
measurements that separate them, taken from frames the analysis pass already has to decode.

Everything here is arithmetic on pixels. No model, no weights to download, nothing to install
— which also means nothing here can say whether a shot is *beautiful*, only whether it is
usable. That is deliberate: usable is most of the gap, and it can be checked by eye against
the numbers.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any, NamedTuple

# Frames taken per shot. Three is enough to catch a shot that is soft or dark throughout
# without paying for a full decode, and lets motion be measured as the change between them.
SAMPLES_PER_SHOT = 3
# Long side the samples are scaled to. Sharpness and motion are compared between shots of one
# recording, so they must be measured at one size or a resolution change would read as a
# quality change.
WORK_WIDTH = 320

# Nothing is ever excluded outright — a run of poor footage must still produce a video, and a
# rule that can fail needs a fallback, which is where this planner's bugs have lived. A weak
# shot is priced down to this and stays eligible.
FLOOR = 0.05
# How far one bad measurement may drag a shot down on its own. Well above zero, because these
# thresholds are uncalibrated guesses and a wrong one should cost a shot its ranking rather
# than remove it from the recording.
TERM_FLOOR = 0.3

# How much a frame is blurred before it is compared with the next one. Enough to erase texture
# and keep shape.
#
# Without it the measurement is dominated by things that shimmer. Real footage of water read 26
# on a plain frame difference, of which only 5 was anything moving — the rest was sparkle on
# the surface. Judged raw, still water on a tripod looks like a camera being shaken.
MOTION_BLUR = 21

# Mean absolute difference between *neighbouring, blurred* frames, on 0–255 grey.
#
# Motion is a band, not a direction. A locked-off shot of an empty aisle is as unusable as a
# violent pan: one is unwatchable and the other is boring, and scoring only one end fills the
# edit with the other.
MOTION_DEAD = 0.5
MOTION_IDEAL_LOW = 1.2
MOTION_IDEAL_HIGH = 20.0
MOTION_VIOLENT = 40.0

# How far the whole frame slides between neighbours, in pixels at the working width — camera
# shake specifically, as distinct from a subject moving inside a held frame.
#
# This is the measurement that actually separates them, and a frame difference barely can. On
# one clip and a deliberately jittered copy of it, the plain difference rose 27 → 33 while the
# frame shift went 1.0 → 5.0. Anything built on the difference alone would have called the
# shaky version steady.
SHAKE_STEADY = 1.6
SHAKE_ROUGH = 6.0
# How sure the alignment must be before its answer is used at all. Real footage, steady or
# shaken, correlates at 0.33 upwards; frames with no structure in common answer confidently
# wrong at 0.03. Below this the reading is discarded rather than believed.
SHIFT_CONFIDENCE = 0.15

# Fraction of pixels crushed to black or blown to white before a shot is considered damaged.
CLIP_TOLERANCE = 0.02
CLIP_RUINED = 0.30


class ShotScore(NamedTuple):
    """One shot's measurements. `quality` is the summary; the rest are kept for the parts of
    selection that compare shots against each other rather than judging them alone."""

    quality: float          # 0..1, how usable this shot is on its own
    sharpness: float        # 0..1, relative to the sharpest shot in this recording
    exposure: float         # 0..1, 1 = nothing clipped
    motion: float           # 0..1, peaks inside the band above
    steadiness: float       # 0..1, 1 = the frame is not sliding about
    colour: tuple[float, float, float]   # mean L,a,b — for continuity between neighbours
    fingerprint: int        # 64-bit perceptual hash — for "have we shown this already"

    @staticmethod
    def neutral() -> ShotScore:
        """What an unmeasurable shot is worth: neither preferred nor penalised."""
        return ShotScore(1.0, 1.0, 1.0, 1.0, 1.0, (0.0, 0.0, 0.0), 0)


def score_shots(video_path: Path, spans: list[tuple[float, float]]) -> tuple[list[ShotScore], str]:
    """Measure every shot of one recording, in one pass over the file.

    Returns a score per span and, when something went wrong, a reason. The reason is not
    decoration: the previous decoder failed on every file in this project and said so only by
    returning nothing, which is how it went unnoticed. A scorer that cannot see must say so.

    The fully bundled edition decodes the original container with PyAV.  The external-tools
    edition deliberately does not ship PyAV's prebuilt FFmpeg libraries; AnalysisService gives
    this scorer a timestamp-repaired proxy and OpenCV walks that proxy sequentially instead.
    Both paths collect the same neighbouring-frame samples and feed the same measurements.
    """
    if not spans:
        return [], ""
    try:
        import cv2
    except Exception as exc:
        return [ShotScore.neutral()] * len(spans), f"镜头质量分析不可用：{exc}"

    try:
        av = None
        if os.environ.get("AVE_EXTERNAL_MEDIA_TOOLS") != "1":
            try:
                import av
            except Exception:
                pass
        frames = (
            _sample_frames(av, cv2, video_path, spans)
            if av is not None
            else _sample_frames_opencv(cv2, video_path, spans)
        )
    except Exception as exc:
        return [ShotScore.neutral()] * len(spans), f"镜头质量分析失败：{exc}"

    missing = sum(1 for group in frames if not group)
    raw = [_measure(cv2, group) if group else None for group in frames]
    note = f"{missing}/{len(spans)} 个镜头无法取样，按中性计分" if missing else ""
    return _combine(raw), note


def _sample_frames(av, cv2, video_path: Path, spans: list[tuple[float, float]]) -> list[list[Any]]:
    """Frames for every shot, gathered in one forward pass.

    Sequential rather than seek-per-shot: seeking is the operation decoders are least reliable
    at, and a single ordered walk asks nothing of the container that plain playback does not.
    Frames are only converted at the positions actually wanted, so most of the file costs a
    decode and nothing more.
    """
    wanted: list[tuple[float, int]] = []
    for index, (start, end) in enumerate(spans):
        for sample in range(SAMPLES_PER_SHOT):
            # Inside the shot rather than at its edges, where a transition may still be
            # resolving and would be measured as blur.
            wanted.append((start + (end - start) * (sample + 1) / (SAMPLES_PER_SHOT + 1), index))
    wanted.sort()

    # Each sample is a *pair* of consecutive frames, because motion is what changes between
    # one frame and the next. Differencing two samples seconds apart measures how unalike two
    # distant moments are, which on any real footage is near-total and reads as a violent pan
    # — real handheld footage scored 40 that way against a 34 "violent" threshold, while the
    # true frame-to-frame motion was 26. It made the metric report the opposite of the truth.
    collected: list[list[list[Any]]] = [[] for _ in spans]
    awaiting: list[int] = []
    cursor = 0
    with av.open(str(video_path)) as container:
        stream = container.streams.video[0]
        stream.thread_type = "AUTO"
        for frame in container.decode(stream):
            if cursor >= len(wanted) and not awaiting:
                break
            moment = float(frame.time or 0.0)
            picture = None

            if awaiting:
                picture = _shrink(cv2, frame.to_ndarray(format="bgr24"))
                for index in awaiting:
                    collected[index][-1].append(picture)
                awaiting = []

            # One frame may answer several wanted positions when shots are very short.
            while cursor < len(wanted) and wanted[cursor][0] <= moment:
                if picture is None:
                    picture = _shrink(cv2, frame.to_ndarray(format="bgr24"))
                index = wanted[cursor][1]
                collected[index].append([picture])
                awaiting.append(index)
                cursor += 1
    return collected


def _sample_frames_opencv(
    cv2, video_path: Path, spans: list[tuple[float, float]]
) -> list[list[Any]]:
    """The same forward-only sampler for a decoder-compatible constant-rate proxy.

    Seeking is intentionally avoided here too.  The proxy was made with an explicit frame rate
    and regenerated timestamps, so frame index divided by FPS is the stable clock that the
    original robot recording could not provide through OpenCV.
    """
    wanted: list[tuple[float, int]] = []
    for index, (start, end) in enumerate(spans):
        for sample in range(SAMPLES_PER_SHOT):
            wanted.append((start + (end - start) * (sample + 1) / (SAMPLES_PER_SHOT + 1), index))
    wanted.sort()

    collected: list[list[list[Any]]] = [[] for _ in spans]
    awaiting: list[int] = []
    cursor = 0
    frame_index = 0
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"OpenCV could not open {video_path.name}")
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
        if not math.isfinite(fps) or fps <= 0:
            raise RuntimeError(f"OpenCV could not read the frame rate of {video_path.name}")
        while cursor < len(wanted) or awaiting:
            ok, frame = capture.read()
            if not ok:
                break
            moment = frame_index / fps
            frame_index += 1
            picture = None

            if awaiting:
                picture = _shrink(cv2, frame)
                for index in awaiting:
                    collected[index][-1].append(picture)
                awaiting = []

            while cursor < len(wanted) and wanted[cursor][0] <= moment:
                if picture is None:
                    picture = _shrink(cv2, frame)
                index = wanted[cursor][1]
                collected[index].append([picture])
                awaiting.append(index)
                cursor += 1
    finally:
        capture.release()
    return collected


def _measure(cv2, bursts: list[list[Any]]) -> dict[str, Any] | None:
    """Raw numbers for one shot from its sampled bursts of consecutive frames."""
    if not bursts:
        return None
    leads = [burst[0] for burst in bursts]
    greys = [cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) for frame in leads]
    # Typical, not extreme, and the same choice for both. Taking the best sharpness let one
    # crisp frame in three carry a shot that is soft for most of its length, while taking the
    # worst clipping let one bright frame condemn a shot that is fine for most of it — two
    # opposite biases, neither of them argued for. A score for a whole shot should describe
    # what most of it looks like.
    sharp = _median([float(cv2.Laplacian(grey, cv2.CV_64F).var()) for grey in greys])
    clipped = _median([_clipped(grey) for grey in greys])

    # Only within a burst: neighbouring frames, so this is the camera and the subject moving
    # rather than the scene having changed. Blurred first, so it is movement being measured
    # and not the surface of the water.
    steps, shakes = [], []
    for burst in bursts:
        if len(burst) < 2:
            continue
        first = cv2.GaussianBlur(cv2.cvtColor(burst[0], cv2.COLOR_BGR2GRAY), (MOTION_BLUR, MOTION_BLUR), 0)
        second = cv2.GaussianBlur(cv2.cvtColor(burst[1], cv2.COLOR_BGR2GRAY), (MOTION_BLUR, MOTION_BLUR), 0)
        steps.append(float(cv2.absdiff(first, second).mean()))
        shakes.append(_frame_shift(cv2, first, second))
    motion = sum(steps) / len(steps) if steps else 0.0
    shake = sum(shakes) / len(shakes) if shakes else 0.0

    middle = leads[len(leads) // 2]
    lab = cv2.cvtColor(middle, cv2.COLOR_BGR2LAB).reshape(-1, 3).mean(axis=0)
    return {
        "sharp": sharp,
        "clipped": clipped,
        "motion": motion,
        "shake": shake,
        "colour": (float(lab[0]), float(lab[1]), float(lab[2])),
        "fingerprint": _fingerprint(cv2, greys[len(greys) // 2]),
    }


def _frame_shift(cv2, first, second) -> float:
    """How far the whole frame moved between two neighbours, in pixels.

    Phase correlation finds the single translation that best aligns them, which is what a
    camera being shaken produces and what a subject moving inside a still frame does not.

    It also returns how strongly it believes that translation, and that number matters: given
    two frames with no shared structure it still answers, and answers nonsense. Pure noise
    reported a ninety-seven pixel slide at a confidence of 0.03, where real footage — steady or
    shaken — sits between 0.33 and 0.47. A reading nobody stands behind is not a measurement,
    so an unconvinced correlation is reported as "no shake seen" rather than as violent shake.
    """
    import numpy as np

    try:
        (dx, dy), response = cv2.phaseCorrelate(np.float32(first), np.float32(second))
    except Exception:
        return 0.0
    if not math.isfinite(response) or response < SHIFT_CONFIDENCE:
        return 0.0
    shift = float((dx * dx + dy * dy) ** 0.5)
    return shift if math.isfinite(shift) else 0.0


def _shrink(cv2, frame):
    height, width = frame.shape[:2]
    if width <= WORK_WIDTH:
        return frame
    return cv2.resize(frame, (WORK_WIDTH, max(1, round(height * WORK_WIDTH / width))))


def _clipped(grey) -> float:
    total = grey.size or 1
    return float(((grey <= 2).sum() + (grey >= 253).sum()) / total)


def _fingerprint(cv2, grey) -> int:
    """Difference hash: 64 bits saying how brightness changes left-to-right.

    Robust to exposure and scale, which is what "is this the same shelf again" needs — two
    frames of one place differ in a few bits, two different places in dozens.
    """
    small = cv2.resize(grey, (9, 8))
    bits = 0
    for row in range(8):
        for column in range(8):
            bits = (bits << 1) | int(small[row][column] < small[row][column + 1])
    return bits


def _combine(raw: list[dict[str, Any] | None]) -> list[ShotScore]:
    """Turn raw measurements into scores, judged against this recording rather than a constant.

    **Relative, because absolute thresholds cannot be calibrated here.** Sharpness has no
    fixed scale — it depends on the lens, the resolution and how much detail the subject
    happens to contain. Nor does clipping: a night shot is mostly black and a shot through a
    window is partly white, and neither is broken. A constant tuned on one camera's footage
    would pass everything from it and reject everything from another.

    Comparing each shot with the rest of its own recording asks the question that can actually
    be answered — *is this shot poor for this footage* — and has the property that matters when
    a whole recording is soft or harshly lit: the least bad shots still score well, because a
    video has to be made either way.

    Motion keeps an absolute band. Its zero is real: no difference between frames means nothing
    moved, whatever the camera.
    """
    measured = [item for item in raw if item]
    best_sharp = max((item["sharp"] for item in measured), default=0.0)
    typical_clip = _median([item["clipped"] for item in measured])

    scores: list[ShotScore] = []
    for item in raw:
        if item is None:
            scores.append(ShotScore.neutral())
            continue
        sharpness = _ramp(item["sharp"] / best_sharp, 0.18, 0.55) if best_sharp > 0 else 1.0
        # Judged against what this recording usually looks like, with a generous allowance
        # before "worse than usual" counts at all.
        allowed = max(CLIP_TOLERANCE, typical_clip * 1.5)
        exposure = 1.0 - _ramp(item["clipped"], allowed, allowed + CLIP_RUINED)
        motion = _band(item["motion"], MOTION_DEAD, MOTION_IDEAL_LOW, MOTION_IDEAL_HIGH, MOTION_VIOLENT)
        steadiness = 1.0 - _ramp(item.get("shake", 0.0), SHAKE_STEADY, SHAKE_ROUGH)
        scores.append(ShotScore(
            quality=_blend(sharpness, exposure, motion, steadiness),
            sharpness=sharpness,
            exposure=exposure,
            motion=motion,
            steadiness=steadiness,
            colour=item["colour"],
            fingerprint=item["fingerprint"],
        ))
    return scores


def _blend(sharpness: float, exposure: float, motion: float, steadiness: float = 1.0) -> float:
    """One number from three, weighted, and gentle about it.

    A plain product lets any single measurement annihilate a shot: one metric reading zero puts
    the total on the floor however good the others are. That is too brittle for numbers nobody
    has calibrated against this camera — a mis-set threshold would not degrade, it would reject
    the whole recording. Each term is floored before it contributes, so a bad one costs a shot
    its place in the ranking without removing it from consideration.
    """
    terms = ((sharpness, 0.3), (exposure, 0.2), (motion, 0.25), (steadiness, 0.25))
    total = 1.0
    for value, weight in terms:
        total *= max(TERM_FLOOR, min(1.0, value)) ** weight
    return max(FLOOR, total)


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _ramp(value: float, low: float, high: float) -> float:
    """0 below `low`, 1 above `high`, smooth between. Soft edges rather than a threshold, so a
    shot a hair either side of a boundary is not treated as a different kind of thing."""
    if high <= low:
        return 1.0 if value >= high else 0.0
    span = min(1.0, max(0.0, (value - low) / (high - low)))
    return span * span * (3.0 - 2.0 * span)


def _band(value: float, dead: float, low: float, high: float, violent: float) -> float:
    """1 inside [low, high], falling to 0 at `dead` below and `violent` above."""
    if value < low:
        return _ramp(value, dead, low)
    if value > high:
        return 1.0 - _ramp(value, high, violent)
    return 1.0


def colour_distance(first: tuple[float, float, float], second: tuple[float, float, float]) -> float:
    """How far apart two shots look, in Lab, where distance is roughly perceptual.

    Used between neighbours: a dim interior cut against a bright window is the jolt that reads
    as unpolished, and it is a difference in lightness far more than in content.
    """
    return math.sqrt(sum((a - b) ** 2 for a, b in zip(first, second)))


def fingerprint_distance(first: int, second: int) -> int:
    """Bits differing between two hashes, 0–64. Small means the same thing filmed twice."""
    return int((first ^ second).bit_count())
