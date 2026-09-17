from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
from itertools import pairwise
from pathlib import Path
from typing import Any

from automated_video_editing_backend.core.models import AnalysisResult, MediaItem, MusicAnalysis
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.capture import read_sidecar, sidecar_path
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.scoring import score_shots

# A detected scene can be several minutes of continuous robot footage. Three measurements for
# that whole span cannot distinguish the clean, steady part from the lurch half a minute later,
# and four candidate timelines then receive the same score however different their source
# moments are. Keep scene boundaries intact, but measure them in local windows. Twelve seconds
# is long enough to carry the longest normal/cinematic cut and short enough to expose changes
# along an aisle.
QUALITY_PROFILE_SECONDS = 12.0


@lru_cache(maxsize=1)
def _has_pyav() -> bool:
    """Whether frames can be read from the container directly. Asked once."""
    if os.environ.get("AVE_EXTERNAL_MEDIA_TOOLS") == "1":
        return False
    try:
        import av  # noqa: F401
    except Exception:
        return False
    return True


class AnalysisService:
    def __init__(self) -> None:
        # A ten-clip job asked for the same song ten times and got ten identical answers, at
        # roughly a second of librosa each. The answer is a few hundred floats, so it is held
        # in memory rather than written anywhere: nothing to clean up, no storage to grow, and
        # it disappears with the process. Keyed on size and mtime so an edited file re-reads.
        self._music_cache: dict[tuple[str, int, int], MusicAnalysis] = {}
        # The same amnesia, far more expensive: scene detection is a subprocess launch and a
        # full decode, around ten seconds a clip, and it ran once per job. A hundred outputs of
        # one recording analysed that recording a hundred times — some seventeen minutes spent
        # re-deriving an answer that cannot have changed. Scenes depend on the file, not on the
        # job, so they are held per file.
        self._scene_cache: dict[tuple, tuple[list[dict[str, Any]], list[str]]] = {}

    async def analyze_video(self, media: MediaItem, music: MediaItem | None = None) -> AnalysisResult:
        warnings: list[str] = []
        scenes = self.detect_scenes(Path(media.path), warnings)
        _lift_static_windows_with_gimbal(Path(media.path), scenes)
        music_result = self.analyze_music(Path(music.path), warnings) if music else None
        return AnalysisResult(
            media_id=media.id,
            scenes=scenes,
            beats=music_result.beats if music_result else [],
            energy=music_result.energy if music_result else [],
            music_duration_seconds=music_result.duration_seconds if music_result else 0.0,
            tempo_bpm=music_result.tempo_bpm if music_result else None,
            onset_times=music_result.onset_times if music_result else [],
            accent_times=music_result.accent_times if music_result else [],
            onset_strength=music_result.onset_strength if music_result else [],
            section_boundaries=music_result.section_boundaries if music_result else [],
            beat_reliability=music_result.beat_reliability if music_result else 0.0,
            music_evidence=music_result.evidence if music_result else "none",
            warnings=warnings,
        )

    def analyze_music(self, audio_path: Path, warnings: list[str]) -> MusicAnalysis:
        """Reusable musical structure and dynamics, read from the track in one pass.

        The old result was just beats plus 64 loudness buckets.  That was enough to make cuts
        move, but not enough to decide whether a track was rhythmic, distinguish strong accents
        from ordinary beats, find coherent excerpt boundaries, or crop a long song without
        stretching its whole energy curve across a short video.
        """
        try:
            stat = audio_path.stat()
            key = (str(audio_path.resolve()), stat.st_size, stat.st_mtime_ns)
        except OSError:
            key = None
        if key is not None and key in self._music_cache:
            return self._music_cache[key]

        result = self._read_music(audio_path, warnings)
        if key is not None:
            self._music_cache[key] = result
        return result

    def _read_music(self, audio_path: Path, warnings: list[str]) -> MusicAnalysis:
        cache_dir = generated_path("cache", "numba")
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("NUMBA_CACHE_DIR", str(cache_dir))

        try:
            import librosa
        except Exception as exc:
            warnings.append(f"librosa unavailable: {exc}")
            return MusicAnalysis(evidence="unreadable")

        try:
            y, sr = librosa.load(str(audio_path), mono=True)
        except Exception as exc:
            warnings.append(f"Music could not be read: {exc}")
            return MusicAnalysis(evidence="unreadable")

        duration = float(librosa.get_duration(y=y, sr=sr))
        hop_length = 512
        onset_envelope = []
        onset_values: list[float] = []
        onset_times: list[float] = []
        accent_times: list[float] = []
        try:
            onset_envelope = librosa.onset.onset_strength(y=y, sr=sr, hop_length=hop_length)
            onset_values = self._normalise_curve(
                [float(value) for value in onset_envelope], buckets=256,
            )
            onset_frames = librosa.onset.onset_detect(
                onset_envelope=onset_envelope, sr=sr, hop_length=hop_length, backtrack=False,
            )
            onset_times = [
                float(value) for value in librosa.frames_to_time(
                    onset_frames, sr=sr, hop_length=hop_length,
                )
            ]
            accent_times = self._accent_times(
                onset_frames, onset_envelope, sr, hop_length, librosa,
            )
        except Exception as exc:
            warnings.append(f"Onset analysis failed: {exc}")

        beats: list[float] = []
        beat_frames = []
        tempo_bpm: float | None = None
        try:
            tempo, beat_frames = librosa.beat.beat_track(
                y=y,
                sr=sr,
                hop_length=hop_length,
                onset_envelope=onset_envelope if len(onset_envelope) else None,
            )
            tempo_value = float(tempo.flat[0] if hasattr(tempo, "flat") else tempo)
            tempo_bpm = tempo_value if math.isfinite(tempo_value) and tempo_value > 0 else None
            beats = [
                float(t) for t in librosa.frames_to_time(
                    beat_frames, sr=sr, hop_length=hop_length,
                )
            ]
        except Exception as exc:
            warnings.append(f"Beat detection failed: {exc}")

        energy: list[float] = []
        try:
            rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
            energy = self._normalise_curve([float(value) for value in rms], buckets=256)
        except Exception as exc:
            warnings.append(f"Loudness analysis failed: {exc}")

        sections = self._music_sections(
            librosa, y, sr, duration, warnings, beat_frames, hop_length,
        )
        reliability = self._beat_reliability(beats, onset_envelope, sr, hop_length, duration)
        evidence = "structured" if len(beats) >= 4 and reliability >= 0.28 else "ambient"
        return MusicAnalysis(
            duration_seconds=max(0.0, duration),
            tempo_bpm=tempo_bpm,
            beats=beats,
            onset_times=onset_times,
            accent_times=accent_times,
            onset_strength=onset_values,
            energy=energy,
            section_boundaries=sections,
            beat_reliability=reliability,
            evidence=evidence,
        )

    def _accent_times(self, frames, envelope, sr: int, hop: int, librosa) -> list[float]:
        """Strong, separated attacks suitable as optional edit points.

        ``onset_detect`` deliberately finds note-level events. A dense instrumental track can
        have dozens per second, and giving all of them to a dynamic edit makes a weak hi-hat as
        influential as the downbeat. Keep the upper-strength band and enforce a small temporal
        separation; ordinary beats remain available independently.
        """
        if len(frames) == 0 or len(envelope) == 0:
            return []
        import numpy as np

        strengths = np.asarray([
            float(envelope[min(len(envelope) - 1, max(0, int(frame)))])
            for frame in frames
        ])
        positive = strengths[strengths > 0]
        if len(positive) == 0:
            return []
        threshold = float(np.quantile(positive, 0.7))
        ranked = sorted(
            (
                float(librosa.frames_to_time(frame, sr=sr, hop_length=hop)),
                float(strength),
            )
            for frame, strength in zip(frames, strengths)
            if strength >= threshold
        )
        kept: list[tuple[float, float]] = []
        for moment, strength in ranked:
            if kept and moment - kept[-1][0] < 0.18:
                if strength > kept[-1][1]:
                    kept[-1] = (moment, strength)
                continue
            kept.append((moment, strength))
        return [round(moment, 6) for moment, _strength in kept]

    def _music_sections(
        self,
        librosa,
        y,
        sr: int,
        duration: float,
        warnings: list[str],
        beat_frames=None,
        hop_length: int = 512,
    ) -> list[float]:
        """Coarse musical boundaries from beat-synchronous harmony and timbre.

        Section finding is intentionally conservative. It supplies good excerpt candidates,
        not a claim that a boundary is a verse or chorus. Tracks too short to contain several
        sections simply expose their beginning and end.
        """
        if duration <= 0:
            return []
        try:
            import numpy as np

            hop = hop_length
            chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=hop)
            mfcc = librosa.feature.mfcc(y=y, sr=sr, hop_length=hop, n_mfcc=13)
            frames_available = min(chroma.shape[1], mfcc.shape[1])
            if frames_available < 2:
                return [0.0, duration]
            features = np.vstack([
                librosa.util.normalize(chroma[:, :frames_available], axis=1),
                librosa.util.normalize(mfcc[:, :frames_available], axis=1),
            ])
            fixed = librosa.util.fix_frames(
                beat_frames if beat_frames is not None else [],
                x_min=0,
                x_max=frames_available - 1,
            )
            # Structured tracks are clustered beat by beat, as the official librosa
            # segmentation recipe recommends. Sparse/ambient tracks fall back to the raw
            # feature clock rather than inventing a beat grid they do not have.
            if len(fixed) >= 4:
                data = librosa.util.sync(features, fixed, aggregate=np.median)
                clock = fixed
            else:
                data = features
                clock = np.arange(frames_available)
            count = max(2, min(8, round(duration / 18.0) + 1))
            boundaries = librosa.segment.agglomerative(data, k=min(count, data.shape[1]))
            source_frames = [clock[min(len(clock) - 1, int(index))] for index in boundaries]
            times = [
                float(value) for value in librosa.frames_to_time(
                    source_frames, sr=sr, hop_length=hop,
                )
            ]
            return self._dedupe_boundaries([0.0, *times, duration], duration)
        except Exception as exc:
            warnings.append(f"Music section analysis failed: {exc}")
            return [0.0, duration]

    def _dedupe_boundaries(self, values: list[float], duration: float) -> list[float]:
        ordered = sorted(max(0.0, min(duration, float(value))) for value in values)
        kept: list[float] = []
        for value in ordered:
            if not kept or value - kept[-1] >= 1.0:
                kept.append(round(value, 3))
        if not kept or kept[0] > 0:
            kept.insert(0, 0.0)
        if duration - kept[-1] >= 0.5:
            kept.append(round(duration, 3))
        return kept

    def _beat_reliability(self, beats, onset, sr: int, hop: int, duration: float) -> float:
        if len(beats) < 2 or duration <= 0 or len(onset) == 0:
            return 0.0
        intervals = [beats[index + 1] - beats[index] for index in range(len(beats) - 1)]
        mean = sum(intervals) / len(intervals)
        if mean <= 0:
            return 0.0
        variance = sum((value - mean) ** 2 for value in intervals) / len(intervals)
        regularity = max(0.0, 1.0 - math.sqrt(variance) / mean)
        indexed = [
            float(onset[min(len(onset) - 1, max(0, round(beat * sr / hop)))])
            for beat in beats
        ]
        peak = max((float(value) for value in onset), default=0.0)
        strength = (sum(indexed) / len(indexed) / peak) if peak > 0 else 0.0
        density = min(1.0, len(beats) / max(1.0, duration / 2.0))
        return round(max(0.0, min(1.0, 0.5 * regularity + 0.35 * strength + 0.15 * density)), 4)

    def _normalise_curve(self, values: list[float], buckets: int = 64) -> list[float]:
        """Flatten a frame-level curve to a short, evenly sampled 0..1 envelope.

        Short because it is read at a handful of positions and a full frame series would be
        thousands of floats travelling through every analysis result for no gain.
        """
        if not values:
            return []
        size = max(1, len(values) // buckets)
        coarse = [
            sum(values[index:index + size]) / len(values[index:index + size])
            for index in range(0, len(values), size)
        ]
        low, high = min(coarse), max(coarse)
        if high - low <= 0:
            return [0.5] * len(coarse)
        return [(value - low) / (high - low) for value in coarse]

    def detect_scenes(self, video_path: Path, warnings: list[str]) -> list[dict[str, Any]]:
        key = self._source_key(video_path)
        cached = self._scene_cache.get(key) if key else None
        if cached is not None:
            scenes, notes = cached
            warnings.extend(notes)
            # Copied out, because the planner is free to read what it is given and a shared
            # list would let one job's reading leak into the next.
            return [dict(scene) for scene in scenes]

        notes: list[str] = []
        scene_input = self._scene_input(video_path, notes)
        scenes = self._detect_scenes_with_pyscenedetect(scene_input, notes)
        if not scenes:
            notes.append("PySceneDetect returned no scenes; using OpenCV fallback")
            scenes = self._detect_scenes_with_opencv(scene_input, notes)
        scenes = self._merge_capture_markers(video_path, scenes, notes)
        # The proxy has the same clock as the source.  In the external-tools edition it is also
        # the decoder compatibility layer for quality sampling, so score the exact input that
        # scene detection could read instead of reopening the troublesome camera container.
        self._score_scenes(scene_input, scenes, notes)
        if key:
            self._scene_cache[key] = (scenes, notes)
        warnings.extend(notes)
        return [dict(scene) for scene in scenes]

    def _score_scenes(
        self,
        video_path: Path,
        scenes: list[dict[str, Any]],
        warnings: list[str],
    ) -> None:
        """Attach each shot's measurements to it, so selection can prefer the usable ones.

        Runs beside detection and is cached with it: the numbers depend on the file, and a
        hundred outputs of one recording need them computed once.
        """
        windows: list[tuple[float, float]] = []
        owners: list[int] = []
        for index, scene in enumerate(scenes):
            start = float(scene.get("start", 0.0))
            end = float(scene.get("end", start))
            cursor = start
            while end - cursor > QUALITY_PROFILE_SECONDS:
                windows.append((cursor, cursor + QUALITY_PROFILE_SECONDS))
                owners.append(index)
                cursor += QUALITY_PROFILE_SECONDS
            if end - cursor >= 0.5:
                windows.append((cursor, end))
                owners.append(index)

        scores, problem = score_shots(video_path, windows)
        if problem:
            warnings.append(problem)
        profiles: list[list[dict[str, Any]]] = [[] for _scene in scenes]
        for span, owner, score in zip(windows, owners, scores):
            profiles[owner].append({
                "start": round(span[0], 3),
                "end": round(span[1], 3),
                "quality": round(score.quality, 4),
                "sharpness": round(score.sharpness, 4),
                "exposure": round(score.exposure, 4),
                "motion": round(score.motion, 4),
                "steadiness": round(score.steadiness, 4),
                "colour": [round(value, 2) for value in score.colour],
                "fingerprint": score.fingerprint,
            })

        for scene, profile in zip(scenes, profiles):
            if not profile:
                continue
            scene["quality_profile"] = profile
            total = sum(max(0.0, item["end"] - item["start"]) for item in profile) or 1.0
            for field in ("quality", "sharpness", "exposure", "motion", "steadiness"):
                scene[field] = round(sum(
                    float(item[field]) * max(0.0, item["end"] - item["start"])
                    for item in profile
                ) / total, 4)
            middle = profile[len(profile) // 2]
            scene["colour"] = list(middle["colour"])
            scene["fingerprint"] = middle["fingerprint"]
        if scores:
            weak = sum(1 for score in scores if score.quality < 0.35)
            if weak:
                warnings.append(f"{weak}/{len(scores)} 个画面区间质量偏低，已在选择时降权")

    def _source_key(self, video_path: Path) -> tuple | None:
        """What makes one analysis of a file different from another.

        The sidecar counts as much as the video: a cruise writes its spans after the recording
        already exists, so a file analysed before that lands has no points and must be read
        again once it does.
        """
        try:
            stat = video_path.stat()
        except OSError:
            return None
        sidecar = sidecar_path(video_path)
        try:
            side = sidecar.stat().st_mtime_ns
        except OSError:
            side = 0
        return (str(video_path.resolve()), stat.st_size, stat.st_mtime_ns, side)

    def _merge_capture_markers(
        self,
        video_path: Path,
        scenes: list[dict[str, Any]],
        warnings: list[str],
    ) -> list[dict[str, Any]]:
        """Fold robot-recorded ground truth into the detected scene list.

        Markers and spans are not detected, they are reported: the robot knew the exact
        second it reached each point. Detection can only guess from pixels, and guesses
        badly when the robot glides between two similar-looking spots with no hard cut.

        Both are kept. Robot boundaries are guaranteed; detected cuts subdivide the long
        stretches in between, so beat-syncing still has somewhere to cut.

        Spans are preferred over markers when the recording has them, because a marker is a
        single instant and a span has two edges — only the latter can say where a dwell
        ended and travelling resumed. Recordings made before spans were written, and every
        manual capture, still take the marker path.
        """
        sidecar = read_sidecar(video_path)
        if not sidecar:
            return scenes

        timeline = sidecar.get("recording_timeline")
        if timeline:
            merged = self._merge_cruise_segments(scenes, [], warnings, timeline)
            if merged:
                return merged

        segments = sidecar.get("segments") or []
        if segments:
            merged = self._merge_cruise_segments(scenes, segments, warnings)
            if merged:
                return merged

        markers = sidecar.get("markers") or []
        marker_points = sorted(
            {
                round(float(marker["timestamp"]), 3): str(marker.get("label") or "")
                for marker in markers
                if isinstance(marker, dict) and marker.get("timestamp") is not None
            }.items()
        )
        if not marker_points:
            return scenes

        duration = max((float(scene.get("end", 0.0)) for scene in scenes), default=0.0)
        last_marker = marker_points[-1][0]
        if duration <= last_marker:
            duration = last_marker + 5.0

        cuts = {0.0, duration}
        cuts.update(float(scene.get("start", 0.0)) for scene in scenes)
        cuts.update(timestamp for timestamp, _ in marker_points if 0.0 < timestamp < duration)
        ordered = sorted(value for value in cuts if 0.0 <= value <= duration)

        labels = dict(marker_points)
        merged: list[dict[str, Any]] = []
        for index, start in enumerate(ordered[:-1]):
            end = ordered[index + 1]
            if end - start < 0.5:
                continue
            label = labels.get(start)
            if label is None:
                # Inherit the label of the marker this stretch belongs to.
                previous = [t for t, _ in marker_points if t <= start]
                label = labels.get(previous[-1]) if previous else None
            merged.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    **self._boundary_evidence(scenes, start),
                    **({"label": label} if label else {}),
                    "from_marker": start in labels,
                }
            )

        warnings.append(f"Merged {len(marker_points)} capture marker(s) into the scene list")
        return merged or scenes

    def _merge_cruise_segments(
        self,
        scenes: list[dict[str, Any]],
        segments: list[Any],
        warnings: list[str],
        recording_timeline: list[dict] | None = None,
    ) -> list[dict[str, Any]]:
        """Cut the recording along the cruise's own spans and label what each stretch is.

        Every scene that comes out of here carries a `kind` — whether the robot was parked,
        travelling, or working a point that failed — which is the distinction the planner
        could not previously draw at all, since the sidecar recorded arrivals and no
        departures.
        """
        spans = self._cruise_spans(segments)
        if recording_timeline is not None:
            spans = [
                (start, end, str(s.get("kind", "unknown")), str(s.get("label", "")))
                for s in recording_timeline if isinstance(s, dict)
                and (start := self._timestamp_value(s.get("start"))) is not None
                and (end := self._timestamp_value(s.get("end"))) is not None and end > start
            ]
        if not spans:
            return []

        finite_ends = [end for _, end, _, _ in spans if math.isfinite(end)]
        duration = max(
            max((float(scene.get("end", 0.0)) for scene in scenes), default=0.0),
            max(finite_ends, default=0.0),
        )
        if duration <= 0:
            return []

        edges = {start for start, _, _, _ in spans}
        edges.update(end for end in finite_ends)

        cuts = {0.0, duration}
        cuts.update(edge for edge in edges if 0.0 < edge < duration)
        cuts.update(float(scene.get("start", 0.0)) for scene in scenes)
        ordered = sorted(value for value in cuts if 0.0 <= value <= duration)

        merged: list[dict[str, Any]] = []
        for index, start in enumerate(ordered[:-1]):
            end = ordered[index + 1]
            if end <= start:
                continue
            # Classified on the midpoint: a boundary belongs to both neighbours, the middle
            # of a stretch belongs to exactly one.
            kind, label = self._span_at((start + end) / 2.0, spans)
            merged.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    **self._boundary_evidence(scenes, start),
                    "kind": kind,
                    **({"label": label} if label else {}),
                    "from_marker": start in edges,
                }
            )

        if merged:
            dwell = sum(1 for scene in merged if scene["kind"] == "dwell")
            warnings.append(
                f"Merged {len(spans)} cruise span(s) into the scene list "
                f"({dwell} parked, {len(merged) - dwell} travelling or failed)"
            )
        return merged

    def _boundary_evidence(self, scenes: list[dict[str, Any]], start: float) -> dict[str, Any]:
        """Carry detector evidence through semantic point/marker subdivision.

        Robot timestamps add boundaries but do not replace visual ones. The earlier merge
        rebuilt every scene with ``score=1`` and silently erased PySceneDetect's content,
        colour, edge and adaptive-ratio metrics. Candidate scoring then claimed confidence at
        every robot arrival and none at the real visual cuts. Only an actual detector boundary
        receives these fields; a robot-only division deliberately remains unscored.
        """
        source = next(
            (
                scene for scene in scenes
                if abs(float(scene.get("start", 0.0)) - start) <= 0.05
            ),
            None,
        )
        if source is None:
            return {}
        fields = {
            key: source[key]
            for key in ("score", "boundary_score", "boundary_metrics")
            if key in source
        }
        fields["from_scene_detector"] = True
        return fields

    def _cruise_spans(self, segments: list[Any]) -> list[tuple[float, float, str, str]]:
        """Turn recorded cruise segments into (start, end, kind, label) spans.

        A dwell whose departure was never recorded — the run died mid-point — is left
        open-ended rather than dropped, so the footage after it is still classified.
        """
        spans: list[tuple[float, float, str, str]] = []
        for segment in segments:
            if not isinstance(segment, dict):
                continue
            path_name = str(segment.get("path_name") or "")
            goal_id = segment.get("goal_id")
            label = f"{path_name}#{goal_id}" if path_name and goal_id is not None else path_name
            status = str(segment.get("status") or "")
            transit_start = self._timestamp_value(segment.get("transit_start_seconds"))
            arrived_at = self._timestamp_value(segment.get("arrived_at_seconds"))
            departed_at = self._timestamp_value(segment.get("departed_at_seconds"))

            if status == "arrived" and arrived_at is not None:
                if transit_start is not None and arrived_at > transit_start:
                    spans.append((transit_start, arrived_at, "transit", label))
                end = departed_at if departed_at is not None and departed_at > arrived_at else math.inf
                spans.append((arrived_at, end, "dwell", label))
            elif status in {"failed", "skipped"}:
                start = transit_start if transit_start is not None else arrived_at
                if start is None:
                    continue
                end = departed_at if departed_at is not None and departed_at > start else math.inf
                spans.append((start, end, status, label))
        return sorted(spans)

    def _span_at(self, timestamp: float, spans: list[tuple[float, float, str, str]]) -> tuple[str, str]:
        """Which span a moment falls in. Later spans win, so an open-ended one yields to
        whatever the robot did next."""
        found = ("unknown", "")
        for start, end, kind, label in spans:
            if start <= timestamp < end:
                found = (kind, label)
        return found

    def detect_beats(self, audio_path: Path, warnings: list[str]) -> list[float]:
        """Beat times only. Kept because callers and tests ask for exactly this."""
        return self.analyze_music(audio_path, warnings).beats

    def _scene_input(self, video_path: Path, warnings: list[str]) -> Path:
        """What to run detection over: the recording itself where possible.

        The proxy is a full transcode of every source, and it existed for one reason — the
        OpenCV backend could not get a usable timestamp out of these files. PyAV reads them
        from the container and has no such trouble, so where PyAV is present the recording is
        read directly and the transcode is skipped entirely.

        It remains the fallback, because a build without PyAV is back to the decoder that
        needed it.  That build always uses the cached proxy: checking timestamps alone is not
        enough, because OpenCV can report a plausible clock and still fail its first frame read.
        """
        if _has_pyav():
            return video_path
        try:
            proxy = self._create_analysis_proxy(video_path)
            warnings.append(f"Using decoder-compatible analysis proxy: {proxy.name}")
            return proxy
        except Exception as exc:
            warnings.append(f"Analysis proxy could not be created: {exc}")
        return video_path

    def _timestamps_need_repair(self, video_path: Path) -> bool:
        ffprobe = RenderService().ffprobe_binary()
        command = [
            ffprobe,
            "-hide_banner",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pkt_pts_time,best_effort_timestamp_time,pkt_dts_time",
            "-read_intervals",
            "%+#250",
            "-of",
            "json",
            str(video_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ffprobe failed")

        frames = json.loads(result.stdout or "{}").get("frames", [])
        if not frames:
            return True

        fps = self._probe_fps(video_path)
        frame_duration = 1 / fps
        previous: float | None = None
        for frame in frames:
            pts = self._timestamp_value(frame.get("pkt_pts_time"))
            best = self._timestamp_value(frame.get("best_effort_timestamp_time"))
            dts = self._timestamp_value(frame.get("pkt_dts_time"))
            if pts is None:
                return True
            timestamp = pts if pts is not None else best if best is not None else dts
            if timestamp is None:
                return True
            if previous is not None and timestamp <= previous + frame_duration * 0.1:
                return True
            previous = timestamp
        return False

    def _create_analysis_proxy(self, video_path: Path) -> Path:
        proxy_dir = generated_path("cache", "proxies", self._video_signature(video_path))
        proxy_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = proxy_dir / "analysis.mp4"
        if proxy_path.exists() and proxy_path.stat().st_size > 0:
            return proxy_path

        fps = self._probe_fps(video_path)
        command = [
            RenderService().ffmpeg_binary(),
            "-y",
            "-fflags",
            "+genpts",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            f"fps={fps:.6f},setpts=N/({fps:.6f}*TB),format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "22",
            "-movflags",
            "+faststart",
            str(proxy_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-2000:] or "ffmpeg proxy generation failed")
        return proxy_path

    def _detect_scenes_with_pyscenedetect(self, video_path: Path, warnings: list[str]) -> list[dict[str, Any]]:
        command = (
            [sys.executable, "--scene-detect-worker", str(video_path)]
            if getattr(sys, "frozen", False)
            else [
                sys.executable,
                "-m",
                "automated_video_editing_backend.services.scene_detect_worker",
                str(video_path),
            ]
        )
        env = os.environ.copy()
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False, env=env)
        if result.returncode != 0:
            warnings.append(f"PySceneDetect failed: {(result.stderr or result.stdout).strip()[-1000:]}")
            return []
        try:
            scenes = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            warnings.append(f"PySceneDetect returned invalid JSON: {exc}")
            return []
        return [scene for scene in scenes if scene.get("end", 0) - scene.get("start", 0) >= 0.5]

    def _detect_scenes_with_opencv(self, video_path: Path, warnings: list[str]) -> list[dict[str, Any]]:
        try:
            import cv2
        except Exception as exc:
            warnings.append(f"OpenCV unavailable: {exc}")
            return self._fallback_scene()

        capture = cv2.VideoCapture(str(video_path))
        try:
            if not capture.isOpened():
                warnings.append(f"Video could not be opened: {video_path.name}")
                return self._fallback_scene()

            fps = self._safe_float(capture.get(cv2.CAP_PROP_FPS), default=30.0)
            frame_count = self._safe_float(capture.get(cv2.CAP_PROP_FRAME_COUNT), default=0.0)
            duration = max(1.0, frame_count / fps) if frame_count > 0 else 6.0

            probe_interval = max(1, int(fps * 0.5))
            cuts = [0.0]
            previous_gray = None
            frame_index = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                if frame_index % probe_interval != 0:
                    frame_index += 1
                    continue

                small = cv2.resize(frame, (96, 54))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                if previous_gray is not None:
                    diff = float(cv2.absdiff(gray, previous_gray).mean())
                    timestamp = frame_index / fps
                    if diff >= 28 and timestamp - cuts[-1] >= 1.0:
                        cuts.append(timestamp)
                previous_gray = gray
                frame_index += 1

            return self._scenes_from_cuts(cuts, duration)
        except Exception as exc:
            warnings.append(f"Scene detection failed: {exc}")
            return self._fallback_scene()
        finally:
            capture.release()

    def _probe_fps(self, video_path: Path) -> float:
        ffprobe = RenderService().ffprobe_binary()
        command = [
            ffprobe,
            "-hide_banner",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(video_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            return 30.0
        streams = json.loads(result.stdout or "{}").get("streams", [])
        if not streams:
            return 30.0
        for key in ["avg_frame_rate", "r_frame_rate"]:
            parsed = self._rate_value(streams[0].get(key))
            if parsed:
                return parsed
        return 30.0

    def _video_signature(self, video_path: Path) -> str:
        stat = video_path.stat()
        payload = f"{video_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
        return sha256(payload.encode("utf-8")).hexdigest()[:24]

    def _timestamp_value(self, value: Any) -> float | None:
        if value in {None, "N/A", ""}:
            return None
        try:
            parsed = float(value)
        except Exception:
            return None
        return parsed if math.isfinite(parsed) else None

    def _rate_value(self, value: Any) -> float | None:
        if not value or value == "0/0":
            return None
        try:
            parsed = float(Fraction(str(value)))
        except Exception:
            return None
        if parsed <= 0 or not math.isfinite(parsed):
            return None
        return parsed

    def _safe_float(self, value: float, default: float) -> float:
        try:
            parsed = float(value)
        except Exception:
            return default
        if not math.isfinite(parsed) or parsed <= 0:
            return default
        return parsed

    def _scenes_from_cuts(self, cuts: list[float], duration: float) -> list[dict[str, Any]]:
        cuts = [cut for cut in cuts if 0 <= cut < duration]
        if not cuts or cuts[0] != 0.0:
            cuts.insert(0, 0.0)
        if duration - cuts[-1] < 1.0 and len(cuts) > 1:
            cuts.pop()

        scenes: list[dict[str, Any]] = []
        for index, start in enumerate(cuts[:8]):
            end = cuts[index + 1] if index + 1 < len(cuts) else duration
            if end - start >= 0.5:
                scenes.append({"start": round(start, 3), "end": round(end, 3), "score": 1.0})
        return scenes or [{"start": 0.0, "end": round(min(duration, 6.0), 3), "score": 1.0}]

    def _fallback_scene(self) -> list[dict[str, Any]]:
        return [{"start": 0.0, "end": 6.0, "score": 1.0}]


# Telemetry rescue: a slow pan over a plain surface can look static to frame differencing even
# though the camera is physically moving. In that one case the gimbal track is stronger evidence
# and lifts the window out of the static band. A still gimbal never erases genuine subject motion.
GIMBAL_SIDECAR_SUFFIX = ".gimbal.json"
_GIMBAL_STATIC_MOTION = 0.12   # visual motion at/below this is considered static
_GIMBAL_MOVING_DEG_S = 1.0     # measured gimbal travel above this is considered moving
_GIMBAL_LIFTED_MOTION = 0.35   # clears the editorial scorer's dead/static band


def _read_gimbal_track(video_path: Path) -> list[tuple[float, float, float]]:
    """The recorded (video_time, yaw, pitch) samples beside a cruise video, or [] if none."""
    sidecar = Path(str(video_path) + GIMBAL_SIDECAR_SUFFIX)
    if not sidecar.exists():
        return []
    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    track: list[tuple[float, float, float]] = []
    for item in (data.get("samples") if isinstance(data, dict) else None) or []:
        try:
            track.append((float(item[0]), float(item[1]), float(item[2])))
        except (TypeError, ValueError, IndexError):
            continue
    track.sort(key=lambda row: row[0])
    return track


def _gimbal_rate(track: list[tuple[float, float, float]], start: float, end: float) -> float:
    """Average yaw+pitch travel (deg/s) over [start, end] from the recorded gimbal track."""
    window = [row for row in track if start - 1e-6 <= row[0] <= end + 1e-6]
    if len(window) < 2:
        return 0.0
    travel = sum(abs(b[1] - a[1]) + abs(b[2] - a[2]) for a, b in pairwise(window))
    span = max(1e-3, window[-1][0] - window[0][0])
    return travel / span


def _lift_static_windows_with_gimbal(video_path: Path, scenes: list[dict[str, Any]]) -> None:
    """Rescue visually static windows only when the physical gimbal was really moving."""
    track = _read_gimbal_track(video_path)
    if not track:
        return
    for scene in scenes:
        profile = scene.get("quality_profile") or []
        changed = False
        for window in profile:
            visual_static = float(window.get("motion", 1.0)) <= _GIMBAL_STATIC_MOTION
            rate = _gimbal_rate(track, float(window.get("start", 0.0)), float(window.get("end", 0.0)))
            if visual_static and rate >= _GIMBAL_MOVING_DEG_S:
                window["motion"] = max(
                    _GIMBAL_LIFTED_MOTION,
                    float(window.get("motion", 0.0)),
                )
                changed = True
        if changed and profile:
            total = sum(max(0.0, w["end"] - w["start"]) for w in profile) or 1.0
            scene["motion"] = round(
                sum(float(w["motion"]) * max(0.0, w["end"] - w["start"]) for w in profile) / total, 4
            )
