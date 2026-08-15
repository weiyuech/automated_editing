from __future__ import annotations

import math
import random
from pathlib import Path
from typing import NamedTuple

from automated_video_editing_backend.core.models import (
    DEFAULT_OUTPUT_HEIGHT,
    DEFAULT_OUTPUT_WIDTH,
    MIN_SCOPE_FRACTION,
    AnalysisResult,
    EditJobRequest,
    EditTimeline,
    MediaItem,
    MusicAnalysis,
    SubtitleCue,
    SubtitleTrack,
    TimelineClip,
)
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services import slots, subtitles
from automated_video_editing_backend.services.semantic import SemanticAlignment

EPSILON = 0.05
# Kept for the no-analysis fallback, where a source with no detected scenes still has to yield
# something usable.
FALLBACK_SHOT_SECONDS = 6.0
# How much of each output is drawn from each sort of footage. Multipliers on how much of that
# footage exists, not quotas, so a lean can never ask for more of something than was filmed.
#
# Parked shots are not automatically the better ones — a glide down an aisle is often the most
# watchable thing in a run — so the batch leans different ways on different outputs rather than
# settling the question once. Failed and skipped legs are pushed down everywhere but never to
# zero: a run where everything failed must still produce a video.
FOOTAGE_MIXES: dict[str, dict[str, float]] = {
    "dwell_heavy": {"dwell": 3.0, "transit": 1.0, "unknown": 2.0, "failed": 0.3, "skipped": 0.3},
    "balanced": {"dwell": 2.0, "transit": 2.0, "unknown": 2.0, "failed": 0.5, "skipped": 0.5},
    "transit_heavy": {"dwell": 1.0, "transit": 3.0, "unknown": 2.0, "failed": 0.5, "skipped": 0.5},
}
NEUTRAL_WEIGHT = 1.0
# The least a stretch of footage can be discounted to. Above zero because a recording where
# everything scores badly must still produce a video, and because these measurements are
# thresholds someone chose rather than facts — a wrong one should cost footage its share, not
# its existence.
QUALITY_FLOOR = 0.15


class Shot(NamedTuple):
    """One continuous run of source footage, and what the robot was doing while filming it."""

    media: MediaItem
    start: float
    end: float
    footage: str = "unknown"
    label: str = ""
    order: int = 0
    # How usable this stretch is, measured rather than assumed. 1.0 when nothing could be
    # measured, so unmeasured footage is neither preferred nor penalised.
    quality: float = 1.0

    @property
    def length(self) -> float:
        return max(0.0, self.end - self.start)


class Placed(NamedTuple):
    """A slot after it has been given somewhere to come from."""

    shot: Shot
    start: float
    duration: float


class EditPlanner:
    """Builds the timeline in the order the decisions actually depend on each other.

    The running time is fixed first, then the shape of the edit as a list of slot durations,
    and only then is footage chosen to fill those slots. Deciding durations while walking the
    source — the previous order — made a cut's length a property of the footage rather than of
    the edit, and left the totals to be reconciled afterwards by machinery that carried every
    ordering bug this planner has had.
    """

    def plan(
        self,
        request: EditJobRequest,
        media_items: list[MediaItem],
        analyses: list[AnalysisResult],
        music: MediaItem | None,
        voiceover: MediaItem | None = None,
        voiceover_duration: float | None = None,
        source_size: tuple[int, int] | None = None,
        music_analysis: MusicAnalysis | None = None,
        semantic_alignment: SemanticAlignment | None = None,
    ) -> EditTimeline:
        warnings: list[str] = []
        analysis_by_media = {item.media_id: item for item in analyses}
        shots = self._shots(media_items, analysis_by_media)

        pace = request.pace or "normal"
        contour = self._contour(request.contour or "flat", music, request.beat_sync, warnings)
        seed = request.variant_seed
        rng = random.Random(seed) if seed is not None else None

        target = float(getattr(request, "target_duration_seconds", 30.0) or 30.0)
        # Duration belongs to the complete assigned recording. Scoping first used to choose
        # enough points for a 30-second request, discover a 45-second narration afterwards,
        # then loop those points while unused forward footage remained outside the scope.
        duration = self._running_time(shots, target, voiceover_duration, warnings)
        scoped = self._scope(shots, request, duration, pace, rng, warnings)

        music_view = self._music_view(
            music_analysis or self._legacy_music(analyses),
            duration,
            request.music_window_rank,
            request.editorial_preset or "smart",
        ) if music else None
        beats = music_view["cut_events"] if request.beat_sync and music_view else []
        energy = music_view["energy"] if contour == "follow_energy" and music_view else []
        longest = max((shot.length for shot in scoped), default=0.0)
        grid = slots.build_slots(duration, pace, contour, beats, energy, longest)

        semantic_diagnostics = (
            semantic_alignment.diagnostics() if semantic_alignment is not None
            else SemanticAlignment(reason="not evaluated").diagnostics()
        )
        placed: list[Placed] | None = None
        if semantic_alignment is not None and semantic_alignment.active:
            semantic_rng = self._copy_rng(rng)
            semantic_warnings: list[str] = []
            semantic_scoped = self._scope(
                shots,
                request,
                duration,
                pace,
                semantic_rng,
                semantic_warnings,
                required_labels=semantic_alignment.mandatory_labels,
            )
            semantic_longest = max((shot.length for shot in semantic_scoped), default=0.0)
            semantic_grid = slots.build_slots(
                duration, pace, contour, beats, energy, semantic_longest
            )
            placed, problem = self._fill_semantic(
                semantic_grid,
                semantic_scoped,
                semantic_alignment,
                request,
                semantic_rng,
            )
            semantic_diagnostics.update({"applied": placed is not None, "fallback_reason": problem})
            if placed is not None:
                warnings.extend(item for item in semantic_warnings if item not in warnings)
        if placed is None:
            placed = self._fill(grid, scoped, request, rng, warnings)
            semantic_diagnostics.setdefault("applied", False)
        self._report_shortfall(placed, duration, pace, warnings)
        output = generated_path("exports", request.output_name)
        # Frame the picture before subtitle cues are wrapped. Both the renderer and libass then
        # see the same canvas, so switching to portrait cannot clip text laid out for landscape.
        if request.output_aspect_ratio == "9:16":
            width, height = DEFAULT_OUTPUT_HEIGHT, DEFAULT_OUTPUT_WIDTH
            output_fit = "cover"
        elif request.output_aspect_ratio == "16:9":
            width, height = DEFAULT_OUTPUT_WIDTH, DEFAULT_OUTPUT_HEIGHT
            output_fit = "cover"
        else:
            width, height = self.source_frame(source_size)
            output_fit = "contain"
        return EditTimeline(
            title=request.title,
            clips=self._clips(placed),
            music_path=music.path if music else None,
            music_start_seconds=float(music_view["start"]) if music_view else 0.0,
            music_duration_seconds=duration if music else None,
            music_loop=bool(music_view and music_view["loop"]),
            music_evidence=(music_view["evidence"] if music_view else "none"),
            editorial_preset=request.editorial_preset or "smart",
            voiceover_path=voiceover.path if voiceover else None,
            subtitles=self._subtitles(request, voiceover, width, height, warnings),
            output_width=width,
            output_height=height,
            output_fit=output_fit,
            output_crop_x=request.output_crop_x if request.output_crop_x is not None else 0.5,
            output_crop_y=request.output_crop_y if request.output_crop_y is not None else 0.5,
            target_duration_seconds=target,
            output_path=str(output),
            mute_original_audio=request.mute_original_audio,
            beat_sync=request.beat_sync,
            planning_diagnostics={
                "planner_version": 2,
                "effective_duration_seconds": round(duration, 3),
                "editorial_preset": request.editorial_preset or "legacy",
                "resolved_policy": {
                    "pace": request.pace,
                    "contour": request.contour,
                    "footage_mix": request.footage_mix,
                    "emphasis": request.emphasis,
                    "point_scope": request.point_scope,
                    "recording_scope": request.recording_scope,
                    "start_rotation": request.start_rotation,
                },
                "music": ({
                    "evidence": music_view["evidence"],
                    "source_duration_seconds": music_view["source_duration"],
                    "start_seconds": music_view["start"],
                    "duration_seconds": duration,
                    "window_rank": request.music_window_rank,
                    "beat_count": len(music_view["beats"]),
                    "onset_count": len(music_view["onsets"]),
                    "accent_count": len(music_view["accents"]),
                    "cut_event_count": len(beats),
                    "loop": music_view["loop"],
                } if music_view else {"evidence": "none"}),
                "semantic_alignment": semantic_diagnostics,
            },
            warnings=warnings,
        )

    def source_frame(self, source_size: tuple[int, int] | None) -> tuple[int, int]:
        """Keep the source canvas, bounded only by the timeline model and encoder needs."""
        if not source_size or source_size[0] <= 0 or source_size[1] <= 0:
            return DEFAULT_OUTPUT_WIDTH, DEFAULT_OUTPUT_HEIGHT
        width, height = source_size
        scale = min(1.0, 7680 / max(width, height))
        width = max(16, int(width * scale) // 2 * 2)
        height = max(16, int(height * scale) // 2 * 2)
        return width, height

    # ── the subtitle layer ──────────────────────────────────────────────────────────────

    def _subtitles(
        self,
        request: EditJobRequest,
        voiceover: MediaItem | None,
        width: int,
        height: int,
        warnings: list[str],
    ) -> SubtitleTrack | None:
        """Cues for the narration, or nothing plus a reason.

        Subtitles are the spoken words, so there is nothing to write without a voiceover and
        nothing to time it by without the provider's per-word timestamps. Every way this can
        come back empty says which one it was: "no narration was chosen" and "the narration has
        no timestamps" call for different actions from the operator, and a single silent
        no-subtitles outcome would leave them guessing which had happened.
        """
        if not getattr(request, "subtitles", False):
            return None
        if voiceover is None:
            warnings.append("勾选了字幕，但这次没有配音，字幕已跳过")
            return None

        # tts.py records where it put the timings. Falling back to the paired stem covers a
        # voiceover that was picked up by a library scan rather than generated in this session.
        metadata_path = (voiceover.metadata or {}).get("metadata_path")
        if not metadata_path:
            metadata_path = str(Path(voiceover.path).with_suffix(".json"))

        raw_words, problem = subtitles.load_words(metadata_path)
        if problem:
            warnings.append(f"字幕已跳过：{problem}")
            return None

        font = self._subtitle_font(request, warnings)
        if font is None:
            return None
        style = subtitles.SubtitleStyle(
            font=font,
            size=subtitles.SIZE_PRESETS.get(
                getattr(request, "subtitle_size", "medium"), subtitles.SIZE_PRESETS["medium"]
            ),
        )
        cues = subtitles.cues_from_words(raw_words, style, width, height)
        if not cues:
            warnings.append("字幕已跳过：配音时间戳里没有可用的文字")
            return None
        return SubtitleTrack(
            cues=[SubtitleCue(start=cue.start, end=cue.end, text=cue.text) for cue in cues],
            font=style.font,
            size=style.size,
            side_margin=style.side_margin,
            bottom_margin=style.bottom_margin,
            outline=style.outline,
            shadow=style.shadow,
            max_lines=style.max_lines,
        )

    def _subtitle_font(self, request: EditJobRequest, warnings: list[str]) -> str | None:
        """The font to use, falling back only to one that is really on disk.

        A font named in a style but absent from the build is not an error libass reports — it
        renders in whatever the system offers instead. Rather than let that through, an
        unavailable choice is said out loud and swapped for one that exists.
        """
        wanted = getattr(request, "subtitle_font", subtitles.DEFAULT_FONT)
        missing = set(subtitles.missing_fonts())
        available = [key for key in subtitles.BUNDLED_FONTS if key not in missing]
        if not available:
            warnings.append("字幕已跳过：没有找到任何内置字体，请运行 scripts/prepare_assets.py")
            return None
        if wanted not in subtitles.BUNDLED_FONTS:
            warnings.append(f"字体 {wanted} 不认识，已改用 {available[0]}")
            return available[0]
        if wanted in missing:
            warnings.append(f"字体文件缺失（{wanted}），已改用 {available[0]}")
            return available[0]
        return wanted

    # ── stage 0: what there is ──────────────────────────────────────────────────────────

    def _shots(
        self,
        media_items: list[MediaItem],
        analysis_by_media: dict[str, AnalysisResult],
    ) -> list[Shot]:
        """Every continuous run of usable footage, in the order it was filmed.

        Shots are spans, not cuts. Where they come from a cruise they carry the point they
        belong to and what the robot was doing; an ordinary import carries neither, and every
        dimension that needs them simply does not apply.
        """
        shots: list[Shot] = []
        for media in media_items:
            analysis = analysis_by_media.get(media.id)
            scenes = analysis.scenes if analysis else []
            if not scenes:
                shots.append(Shot(media, 0.0, FALLBACK_SHOT_SECONDS, order=len(shots)))
                continue
            for scene in scenes:
                start = float(scene.get("start", 0.0))
                end = float(scene.get("end", start + FALLBACK_SHOT_SECONDS))
                if end - start < slots.MIN_SLOT_SECONDS:
                    continue
                # Long continuous takes carry a local quality profile. Treat those windows as
                # selectable source runs without pretending they are PySceneDetect cuts: this
                # is what lets two candidates using different moments receive different
                # quality/colour/hash evidence while all placement invariants stay unchanged.
                profile = [
                    item for item in (scene.get("quality_profile") or [])
                    if float(item.get("end", 0.0)) - float(item.get("start", 0.0))
                    >= slots.MIN_SLOT_SECONDS
                ]
                portions = profile or [{
                    "start": start,
                    "end": end,
                    "quality": scene.get("quality", 1.0),
                }]
                for portion in portions:
                    shots.append(Shot(
                        media,
                        float(portion.get("start", start)),
                        float(portion.get("end", end)),
                        str(scene.get("kind") or "unknown"),
                        str(scene.get("label") or ""),
                        len(shots),
                        float(portion.get("quality", scene.get("quality", 1.0)) or 1.0),
                    ))
        return shots

    # ── stage 1: how long the edit runs ─────────────────────────────────────────────────

    def _running_time(
        self,
        shots: list[Shot],
        target: float,
        voiceover_duration: float | None,
        warnings: list[str],
    ) -> float:
        available = sum(shot.length for shot in shots)
        duration = min(available, target)
        if available + EPSILON < target:
            warnings.append(f"素材只够 {available:.0f} 秒，短于目标时长 {target:.0f} 秒")

        # A narration is never cut off mid-sentence. The target gives way first; the picture
        # cycles only when the narration outlasts the complete assigned recording.
        if voiceover_duration and available > 0:
            if voiceover_duration > duration + EPSILON:
                if voiceover_duration > available + EPSILON:
                    warnings.append(f"画面循环 {voiceover_duration - available:.0f} 秒以配合旁白")
                else:
                    warnings.append(
                        f"旁白长于目标时长，成片延长至 {voiceover_duration:.0f} 秒"
                    )
                duration = voiceover_duration
            elif voiceover_duration + EPSILON < duration:
                warnings.append(f"旁白比画面短 {duration - voiceover_duration:.0f} 秒，尾部无人声")
        return duration

    # ── stage 2: which places and recordings are in play ────────────────────────────────

    def _scope(
        self,
        shots: list[Shot],
        request: EditJobRequest,
        target: float,
        pace: str,
        rng: random.Random | None,
        warnings: list[str],
        required_labels: set[str] | None = None,
    ) -> list[Shot]:
        """Narrow the footage to the recordings and places this edit uses.

        Scope is capped by the running time, not chosen freely: showing k places in T seconds
        gives each T/k, and below a couple of cuts each they are glimpsed rather than shown.
        A cinematic thirty seconds holds one place; a fast one holds seven.
        """
        scoped = self._scoped_by(
            shots, lambda shot: shot.media.id, request.recording_scope, target, pace, rng, None,
        )
        return self._scoped_by(
            scoped,
            lambda shot: shot.label,
            request.point_scope,
            target,
            pace,
            rng,
            warnings,
            required_labels,
        )

    def _scoped_by(
        self,
        shots: list[Shot],
        key,
        scope: str | None,
        target: float,
        pace: str,
        rng: random.Random | None,
        warnings: list[str] | None,
        required: set[str] | None = None,
    ) -> list[Shot]:
        buckets: dict[str, list[Shot]] = {}
        for shot in shots:
            buckets.setdefault(key(shot), []).append(shot)
        names = [name for name in buckets if name]
        if len(names) < 2 or scope in (None, "all"):
            return shots

        fraction = min(1.0, max(MIN_SCOPE_FRACTION, float(scope)))
        capacity = slots.place_capacity(target, pace)
        wanted = max(1, round(fraction * min(len(names), capacity)))
        if wanted >= len(names):
            return shots

        chosen = self._spread(names, wanted, rng)
        if required:
            chosen = list(dict.fromkeys([*chosen, *(name for name in names if name in required)]))
        # A thin draw would leave the picture looping while unused places sit outside the
        # scope, so the selection widens until it can actually cover the running time.
        chosen = self._widen(chosen, names, buckets, target, pace)
        if warnings is not None and len(chosen) < len(names):
            warnings.append(f"本条只用了 {len(chosen)}/{len(names)} 个点位")
        keep = set(chosen)
        return [shot for shot in shots if key(shot) in keep or not key(shot)]

    def _copy_rng(self, rng: random.Random | None) -> random.Random | None:
        if rng is None:
            return None
        copied = random.Random()
        copied.setstate(rng.getstate())
        return copied

    def _spread(self, names: list[str], wanted: int, rng: random.Random | None) -> list[str]:
        """Take `wanted` names spread across the whole list, not from its front."""
        stride = len(names) / wanted
        picked: list[str] = []
        for index in range(wanted):
            low = int(index * stride)
            high = min(len(names), max(low + 1, int((index + 1) * stride)))
            picked.append(names[rng.randrange(low, high) if rng else low])
        return picked

    def _widen(
        self,
        chosen: list[str],
        names: list[str],
        buckets: dict[str, list[Shot]],
        target: float,
        pace: str,
    ) -> list[str]:
        """Grow a scope until it can comfortably cover the running time.

        Comfortably, not exactly. A scope holding precisely the target has no slack: a cut
        landing near the end of a run gets shortened to what is left, and enough of those add
        up to an edit visibly short of its length.

        The slack needed is about half a cut per run of footage — that is what a cut landing
        awkwardly costs — so it scales with the pace rather than with the target. A flat
        percentage of the target got this wrong in both directions: too little slack for held
        shots, and so much for quick ones that it widened every scope back to the same size and
        flattened the dimension it was protecting.
        """
        def held(selection: list[str]) -> float:
            return sum(shot.length for name in selection for shot in buckets[name])

        cut = slots.PACE_SECONDS.get(pace, slots.PACE_SECONDS["normal"])
        remaining = [name for name in names if name not in set(chosen)]
        remaining.sort(key=lambda name: sum(shot.length for shot in buckets[name]), reverse=True)
        for name in [None, *remaining]:
            if name is not None:
                chosen.append(name)
            if held(chosen) + EPSILON >= target + len(chosen) * cut:
                break
        return [name for name in names if name in set(chosen)]

    # ── stage 3 inputs: what the music has to say ───────────────────────────────────────

    def _contour(self, contour: str, music, beat_sync: bool, warnings: list[str]) -> str:
        """A contour that needs music it does not have falls back rather than doing nothing."""
        if contour == "follow_energy" and not (music and beat_sync):
            warnings.append("没有音乐节拍，跟随强度已改为弧线节奏")
            return "arc"
        return contour

    def _beats(self, analyses: list[AnalysisResult]) -> list[float]:
        return next((analysis.beats for analysis in analyses if len(analysis.beats) >= 2), [])

    def _energy(self, analyses: list[AnalysisResult]) -> list[float]:
        return next((analysis.energy for analysis in analyses if analysis.energy), [])

    def _legacy_music(self, analyses: list[AnalysisResult]) -> MusicAnalysis:
        """Lift old AnalysisResult music fields into the richer internal shape."""
        found = next((item for item in analyses if item.beats or item.energy), None)
        if found is None:
            return MusicAnalysis(evidence="unreadable")
        return MusicAnalysis(
            duration_seconds=found.music_duration_seconds,
            tempo_bpm=found.tempo_bpm,
            beats=found.beats,
            onset_times=found.onset_times,
            accent_times=found.accent_times,
            onset_strength=found.onset_strength,
            energy=found.energy,
            section_boundaries=found.section_boundaries,
            beat_reliability=found.beat_reliability,
            evidence=(
                found.music_evidence
                if found.music_evidence != "none"
                else ("structured" if len(found.beats) >= 2 else "ambient")
            ),
        )

    def _music_view(
        self, music: MusicAnalysis, duration: float, rank: int, preset: str = "smart",
    ) -> dict:
        """The exact excerpt used by both planning and rendering.

        Candidate starts are musical boundaries and windows ending on a boundary.  Rank zero
        chooses the strongest coherent window; later ranks create real batch variety without
        moving to arbitrary seconds in the middle of a phrase.
        """
        source_duration = music.duration_seconds
        if source_duration <= 0:
            # Compatibility for hand-built/old test analyses that contain timestamped beats
            # but did not record the source duration.
            source_duration = max([duration, *music.beats, *music.onset_times], default=duration)
        if source_duration <= duration + EPSILON:
            beats = self._repeat_events(music.beats, source_duration, duration)
            onsets = self._repeat_events(music.onset_times, source_duration, duration)
            accents = self._repeat_events(music.accent_times, source_duration, duration)
            return {
                "start": 0.0,
                "source_duration": source_duration,
                "beats": beats,
                "onsets": onsets,
                "accents": accents,
                "cut_events": self._cut_events(music.evidence, preset, beats, accents),
                "energy": self._repeat_curve(music.energy, source_duration, duration),
                "evidence": music.evidence,
                "loop": source_duration + EPSILON < duration,
            }

        latest = source_duration - duration
        boundaries = music.section_boundaries or [0.0, source_duration]
        starts = {0.0, latest}
        for boundary in boundaries:
            starts.add(max(0.0, min(latest, boundary)))
            starts.add(max(0.0, min(latest, boundary - duration)))
        candidates = sorted(starts)
        scored = sorted(
            candidates,
            key=lambda start: (-self._music_window_score(music, start, duration, preset), start),
        )
        chosen = scored[rank % len(scored)] if scored else 0.0
        beats = [
            round(value - chosen, 6)
            for value in music.beats if chosen <= value <= chosen + duration
        ]
        onsets = [
            round(value - chosen, 6)
            for value in music.onset_times if chosen <= value <= chosen + duration
        ]
        accents = [
            round(value - chosen, 6)
            for value in music.accent_times if chosen <= value <= chosen + duration
        ]
        return {
            "start": round(chosen, 3),
            "source_duration": source_duration,
            "beats": beats,
            "onsets": onsets,
            "accents": accents,
            "cut_events": self._cut_events(music.evidence, preset, beats, accents),
            "energy": self._slice_curve(music.energy, source_duration, chosen, duration),
            "evidence": music.evidence,
            "loop": False,
        }

    def _music_window_score(
        self, music: MusicAnalysis, start: float, duration: float, preset: str = "smart",
    ) -> float:
        end = start + duration
        boundaries = music.section_boundaries or [0.0, music.duration_seconds]
        start_gap = min((abs(start - value) for value in boundaries), default=duration)
        end_gap = min((abs(end - value) for value in boundaries), default=duration)
        boundary = (math.exp(-start_gap / 2.0) + math.exp(-end_gap / 2.0)) / 2.0
        beats = sum(1 for value in music.beats if start <= value <= end)
        accents_in_window = sum(1 for value in music.accent_times if start <= value <= end)
        density = min(1.0, beats / max(1.0, duration / 2.0))
        accents = min(1.0, accents_in_window / max(1.0, duration / 3.0))
        energy = self._curve_window(music.energy, music.duration_seconds, start, duration)
        onset = self._curve_window(music.onset_strength, music.duration_seconds, start, duration)
        dynamic = (max(energy) - min(energy)) if energy else 0.0
        punch = sum(onset) / len(onset) if onset else 0.0
        if preset == "dynamic":
            return (
                0.25 * boundary + 0.18 * density + 0.17 * accents
                + 0.20 * punch + 0.12 * dynamic + 0.08 * music.beat_reliability
            )
        if preset == "immersive":
            return (
                0.42 * boundary + 0.17 * (1.0 - density) + 0.12 * (1.0 - accents)
                + 0.15 * (1.0 - dynamic) + 0.08 * (1.0 - punch)
                + 0.06 * music.beat_reliability
            )
        return (
            0.40 * boundary + 0.18 * density + 0.12 * accents
            + 0.10 * punch + 0.08 * dynamic + 0.12 * music.beat_reliability
        )

    def _cut_events(
        self, evidence: str, preset: str, beats: list[float], accents: list[float],
    ) -> list[float]:
        if evidence != "structured":
            return []
        values = beats if preset != "dynamic" else [*beats, *accents]
        return sorted({round(value, 6) for value in values if value >= 0})

    def _curve_window(
        self, curve: list[float], source_duration: float, start: float, duration: float,
    ) -> list[float]:
        if not curve or source_duration <= 0:
            return []
        left = max(0, min(len(curve) - 1, int(start / source_duration * len(curve))))
        right = max(
            left + 1,
            min(len(curve), math.ceil((start + duration) / source_duration * len(curve))),
        )
        return curve[left:right]

    def _slice_curve(
        self, curve: list[float], source_duration: float, start: float, duration: float,
    ) -> list[float]:
        part = self._curve_window(curve, source_duration, start, duration)
        if not part:
            return []
        low, high = min(part), max(part)
        if high - low <= EPSILON:
            return [0.5] * len(part)
        return [(value - low) / (high - low) for value in part]

    def _repeat_events(self, values: list[float], cycle: float, duration: float) -> list[float]:
        if cycle <= 0:
            return [value for value in values if value <= duration]
        repeated: list[float] = []
        offset = 0.0
        while offset < duration:
            repeated.extend(value + offset for value in values if value + offset <= duration)
            offset += cycle
        return repeated

    def _repeat_curve(self, curve: list[float], cycle: float, duration: float) -> list[float]:
        if not curve or cycle <= 0 or cycle >= duration:
            return curve
        repeats = max(1, math.ceil(duration / cycle))
        return (curve * repeats)[: max(len(curve), int(len(curve) * duration / cycle))]

    # ── stage 4: filling the slots ──────────────────────────────────────────────────────

    def _fill(
        self,
        grid: list[float],
        shots: list[Shot],
        request: EditJobRequest,
        rng: random.Random | None,
        warnings: list[str],
    ) -> list[Placed]:
        """Give every slot somewhere to come from.

        Slots are whole things, so this is an integer partition rather than a negotiation over
        seconds: the cuts divide between places, then between sorts of footage within a place,
        and only the source position inside a group is a continuous choice.
        """
        if not grid or not shots:
            return []

        # The picture repeats only when the edit genuinely outruns the footage — a narration
        # longer than everything filmed. Everywhere else running out is a bug, not a mode, so
        # it is a decision made once here rather than a fallback buried in the placement.
        repeat = sum(grid) > sum(shot.length for shot in shots) + EPSILON

        places = self._places(grid, shots, request, warnings)
        groups = self._groups(grid, places, request)
        assignment = slots.assign_in_order(
            grid,
            [sum(shot.length for shot in group) for group, _target in groups],
            [target for _group, target in groups],
            repeat,
        )

        portions: dict[int, list[float]] = {}
        for duration, index in zip(grid, assignment):
            portions.setdefault(index, []).append(duration)

        # Emitted group by group, in the order the edit visits them. Sorting the finished
        # clips by filming order instead would put the route back the way it was shot and
        # silently undo the rotation, which is the one dimension whose whole purpose is to
        # break that order.
        # By group index, not by the order groups happened to be first assigned. Once every
        # group has had its share the leftover cuts go to whoever still has footage, which can
        # be a group the walk has already passed — and taking them in assignment order would
        # then put a place the edit had left back after the one that followed it.
        placed: list[Placed] = []
        for index in sorted(portions):
            placed.extend(self._place_run(portions[index], groups[index][0], rng, repeat))
        return placed

    def _fill_semantic(
        self,
        grid: list[float],
        shots: list[Shot],
        alignment: SemanticAlignment,
        request: EditJobRequest,
        rng: random.Random | None,
    ) -> tuple[list[Placed] | None, str]:
        """Fill the established global rhythm inside monotonic semantic source windows.

        This is a constrained wrapper around ``_fill``, not a second selector. Each chapter
        still uses the existing quality, dwell/transit and seeded placement machinery. If a
        described point cannot carry its whole chapter without crossing or replaying, the
        wrapper returns no result and the caller invokes the unchanged planner.
        """
        records = self._semantic_slot_records(grid, alignment)
        groups: list[tuple[str | None, list[float]]] = []
        for label, duration in records:
            if groups and groups[-1][0] == label:
                groups[-1][1].append(duration)
            else:
                groups.append((label, [duration]))

        placed: list[Placed] = []
        cursor = 0.0
        source_end = max((shot.end for shot in shots), default=0.0)
        for group_index, (label, portion) in enumerate(groups):
            candidates = self._source_after(shots, cursor, label)
            wanted = sum(portion)
            # `_fill` normally spreads cuts across the complete candidate range. That is good
            # for a finished edit, but not for an intermediate semantic chapter: spreading the
            # final matched point to the end of the recording can consume footage needed by a
            # shorter narration's ordinary picture-led tail. Cap this chapter early enough to
            # reserve the raw seconds still required by every later chapter. The final group is
            # untouched and therefore keeps normal seeded spreading and diversity.
            future_wanted = sum(
                sum(later_portion) for _later_label, later_portion in groups[group_index + 1:]
            )
            if future_wanted > EPSILON:
                candidates = self._source_through(
                    candidates, max(cursor, source_end - future_wanted)
                )
            held = sum(shot.length for shot in candidates)
            if held + EPSILON < wanted:
                kind = f"point {label}" if label else "remaining forward footage"
                return None, f"{kind} holds {held:.2f}s for a {wanted:.2f}s chapter"

            local = self._fill(portion, candidates, request, rng, [])
            actual = sum(item.duration for item in local)
            if actual + max(EPSILON, wanted * 0.02) < wanted:
                kind = f"point {label}" if label else "remaining forward footage"
                return None, f"{kind} could place only {actual:.2f}/{wanted:.2f}s"
            if placed and local and placed[-1].start + placed[-1].duration > local[0].start + 1e-6:
                return None, "semantic chapters would move backwards in source time"
            placed.extend(local)
            if local:
                cursor = local[-1].start + local[-1].duration

        if not placed:
            return None, "semantic schedule produced no clips"
        planned = sum(item.duration for item in placed)
        if planned + max(EPSILON, sum(grid) * 0.02) < sum(grid):
            return None, f"semantic schedule is {sum(grid) - planned:.2f}s short"
        return placed, ""

    def _semantic_slot_records(
        self, grid: list[float], alignment: SemanticAlignment
    ) -> list[tuple[str | None, float]]:
        """Split the global cut grid at narration chapters, then give each piece a point.

        Neutral narration immediately before a matched chapter borrows that upcoming point as
        a visual subject. It is not counted as a semantic match in diagnostics; this merely
        prevents a generic opening sentence from consuming footage beyond the point it is
        about to introduce. The post-narration tail remains neutral and uses ordinary forward
        footage.
        """
        boundaries = [
            chapter.end
            for chapter in alignment.chapters[:-1]
            if 0 < chapter.end < sum(grid)
        ]
        durations = self._split_grid(grid, boundaries)
        edges: list[tuple[float, float]] = []
        cursor = 0.0
        for duration in durations:
            edges.append((cursor, cursor + duration))
            cursor += duration

        def chapter_at(position: float):
            return next(
                (
                    chapter for chapter in alignment.chapters
                    if chapter.start - 1e-6 <= position < chapter.end + 1e-6
                ),
                None,
            )

        records: list[tuple[str | None, float]] = []
        for start, end in edges:
            chapter = chapter_at((start + end) / 2.0)
            label = chapter.label if chapter else None
            if chapter is not None and label is None and start < alignment.narration_duration:
                label = next(
                    (
                        later.label for later in alignment.chapters
                        if later.start >= chapter.end - 1e-6 and later.label
                    ),
                    None,
                )
            records.append((label, end - start))
        return records

    def _split_grid(self, grid: list[float], boundaries: list[float]) -> list[float]:
        if not boundaries:
            return grid
        total = sum(grid)
        edges = [0.0]
        for duration in grid:
            edges.append(edges[-1] + duration)
        for boundary in sorted(set(boundaries)):
            if boundary <= EPSILON or boundary >= total - EPSILON:
                continue
            nearest = min(edges, key=lambda edge: abs(edge - boundary))
            # A nearby rhythmic edge already expresses the sentence change. Otherwise the
            # meaning boundary is more important than preserving one oversized music slot.
            if abs(nearest - boundary) > 0.35:
                edges.append(boundary)
        edges = sorted({round(edge, 9) for edge in edges})
        return [edges[index + 1] - edges[index] for index in range(len(edges) - 1)]

    def _source_after(
        self, shots: list[Shot], cursor: float, label: str | None
    ) -> list[Shot]:
        out: list[Shot] = []
        for shot in sorted(shots, key=lambda item: (item.start, item.order)):
            if label is not None and shot.label != label:
                continue
            start = max(shot.start, cursor)
            if shot.end - start < slots.MIN_SLOT_SECONDS:
                continue
            out.append(shot._replace(start=start))
        return out

    def _source_through(self, shots: list[Shot], limit: float) -> list[Shot]:
        """Clip a candidate list at a forward reservation boundary."""
        out: list[Shot] = []
        for shot in shots:
            end = min(shot.end, limit)
            if end - shot.start >= slots.MIN_SLOT_SECONDS:
                out.append(shot._replace(end=end))
        return out

    def _groups(
        self,
        grid: list[float],
        places: list[tuple[str, list[Shot]]],
        request: EditJobRequest,
    ) -> list[tuple[list[Shot], int]]:
        """Every run of footage a cut could come from, in the order the edit visits them,
        each with the number of cuts it is asked for.

        Two divisions, both in whole cuts: the edit's cuts between the places it visits, then
        each place's cuts between the sorts of footage shot there.
        """
        weights = FOOTAGE_MIXES.get(request.footage_mix or "balanced", FOOTAGE_MIXES["balanced"])
        per_place = slots.split_slots(
            len(grid), len(places), self._place_weights(places, request),
        )

        groups: list[tuple[list[Shot], int]] = []
        for (_label, shots), count in zip(places, per_place):
            # One group per shot, in filming order.
            #
            # Grouping by sort of footage was what let an edit run backwards inside a visit: a
            # point reached on a second attempt films travelling, parked, travelling, parked,
            # and pooling by sort puts the later travelling stretch before the earlier parked
            # one. A shot is contiguous by definition, so this cannot happen at all.
            #
            # It is also what lets a measurement matter. Cuts are shared out per group, so
            # while a whole recording was one group there was nothing for quality to choose
            # between — plain footage received exactly its proportional share however poor it
            # was. Per shot, a soft or dead stretch is simply asked for fewer cuts.
            if len(shots) < 2:
                groups.append((shots, count))
                continue
            demand = [
                weights.get(shot.footage, NEUTRAL_WEIGHT) * self._worth([shot])
                for shot in shots
            ]
            for shot, share in zip(shots, slots.split_slots(count, len(shots), demand)):
                groups.append(([shot], share))
        return groups

    def _places(
        self,
        grid: list[float],
        shots: list[Shot],
        request: EditJobRequest,
        warnings: list[str],
    ) -> list[tuple[str, list[Shot]]]:
        """The places the edit visits, in the order it visits them.

        Rotation is the one dimension that deliberately breaks filming order: the same route
        opened at its fifth point reads as a different video, which is worth more than the
        chronology costs. It applies only where there are places to rotate.
        """
        # A *visit*, not a label. A route that returns to a point later films it twice, and the
        # two occasions are two places in the edit's journey with other places between them.
        # Pooling them by name put every cut from the first visit's approach before every cut
        # from it, including the ones filmed an hour later — the edit jumped backwards inside
        # what it was calling one place. Runs of consecutive shots keep the journey in order,
        # and collapse to exactly one group per label when nothing is revisited, which is the
        # ordinary case.
        ordered: list[tuple[str, list[Shot]]] = []
        for shot in sorted(shots, key=lambda item: item.order):
            if ordered and ordered[-1][0] == shot.label:
                ordered[-1][1].append(shot)
            else:
                ordered.append((shot.label, [shot]))

        # Showing every place means showing each of them properly, so the count is capped at
        # what the cuts allow — two each. Asked to cover more places than that, an edit does
        # not cover them, it glimpses them; it says how many it could take instead.
        if (request.emphasis or "target") == "coverage":
            capacity = max(1, len(grid) // slots.CUTS_PER_PLACE)
            named = [item for item in ordered if item[0]]
            if len(named) > capacity:
                # Counted in points for the operator, who thinks in points, while the edit
                # works in visits.
                points = len({label for label, _shots in named})
                warnings.append(f"目标时长只够覆盖 {min(capacity, points)}/{points} 个点位")
                stride = len(named) / capacity
                keep = {id(named[min(len(named) - 1, int(index * stride))][1]) for index in range(capacity)}
                ordered = [item for item in ordered if id(item[1]) in keep or not item[0]]

        rotation = request.start_rotation or 0
        if rotation and len(ordered) > 1 and any(label for label, _ in ordered):
            rotation %= len(ordered)
            if rotation:
                warnings.append(f"从第 {rotation + 1} 个点位开始")
                ordered = ordered[rotation:] + ordered[:rotation]
        return ordered

    def _place_weights(self, places, request: EditJobRequest) -> list[float] | None:
        """Coverage gives every place the same number of cuts; target gives them cuts in
        proportion to how much *usable* footage was filmed there."""
        if (request.emphasis or "target") == "coverage":
            return None
        return [self._worth(group) for _label, group in places]

    def _worth(self, shots: list[Shot]) -> float:
        """Seconds of footage, discounted by how usable those seconds are.

        This is where measurement reaches the edit. Cuts are handed out in proportion to worth
        rather than to length, so a stretch that is soft, blown out, dead still or filmed
        mid-lurch is asked for less of — and a good one for more.

        Discounted, never excluded. A run of poor footage still has to make a video, and
        quality that could not be measured scores 1.0, so unmeasured footage behaves exactly as
        it did before any of this existed.
        """
        return sum(shot.length * max(QUALITY_FLOOR, min(1.0, shot.quality)) for shot in shots)

    def _place_run(
        self,
        portion: list[float],
        group: list[Shot],
        rng: random.Random | None,
        repeat: bool,
    ) -> list[Placed]:
        """Walk a group of shots once, taking each cut in turn.

        Walking forward is what guarantees no cut is ever taken twice and none reads as a jump
        backwards: the cursor only advances. The unused footage is spent as gaps between the
        cuts, so they spread over the whole group rather than crowding its opening, and a seed
        spends those gaps differently for every output.

        When the cursor runs out of footage the remaining cuts are dropped rather than
        satisfied by replaying earlier ones — a slightly shorter edit is always better than one
        that jumps backwards. `repeat` is set only where repetition is the point, which is a
        narration outlasting everything that was filmed.
        """
        if not portion or not group:
            return []
        held = sum(shot.length for shot in group)
        gaps = slots.spread_gaps(max(0.0, held - sum(portion)), len(portion), rng)

        placed: list[Placed] = []
        index = 0
        offset = 0.0
        for duration, gap in zip(portion, gaps):
            offset += gap
            # Move on only once a shot has no usable room left. Skipping ahead to find a shot
            # that fits the whole cut abandons whatever is left of this one, and enough of
            # those remainders add up to an edit that runs out before it reaches its length.
            while index < len(group) and group[index].length - offset < slots.MIN_SLOT_SECONDS:
                offset = max(0.0, offset - group[index].length)
                index += 1
            if index >= len(group):
                if not repeat:
                    break
                index, offset = 0, 0.0
            shot = group[index]
            # A cut cannot span two shots, so one landing on a boundary is shortened to fit.
            take = min(duration, shot.length - offset)
            placed.append(Placed(shot, shot.start + offset, take))
            offset += take
        return placed

    def _report_shortfall(
        self,
        placed: list[Placed],
        duration: float,
        pace: str,
        warnings: list[str],
    ) -> None:
        """Say so when the footage could not carry the requested length.

        A cut cannot span two shots, so one asked to run longer than the shot it comes from is
        shortened to fit. At a slow pace on short shots that is not an edge case — an
        eight-second cut simply cannot be taken from a six-second shot, and no ordering of the
        cuts changes it. Shortening quietly would leave an operator wondering why a
        twenty-second setting produced sixteen seconds; the honest answer is that the footage
        is cut too short for the pace, and a quicker one would fill it.
        """
        total = sum(item.duration for item in placed)
        if not placed or total + max(0.5, duration * 0.05) >= duration:
            return
        hint = "，可改用更快的节奏" if pace != "fast" else ""
        warnings.append(f"成片 {total:.0f} 秒，短于目标 {duration:.0f} 秒：镜头长度不足以支撑此节奏{hint}")

    # ── stage 5: the timeline itself ────────────────────────────────────────────────────

    def _clips(self, placed: list[Placed]) -> list[TimelineClip]:
        clips: list[TimelineClip] = []
        timeline_start = 0.0
        for item in placed:
            clips.append(TimelineClip(
                media_id=item.shot.media.id,
                source_path=item.shot.media.path,
                start=item.start,
                duration=item.duration,
                timeline_start=timeline_start,
                footage=item.shot.footage,
                label=item.shot.label,
            ))
            timeline_start += item.duration
        return clips
