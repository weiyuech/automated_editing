from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from fractions import Fraction
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Any

from automated_video_editing_backend.core.models import AnalysisResult, MediaItem
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.capture import read_sidecar, sidecar_path
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.scoring import score_shots


@lru_cache(maxsize=1)
def _has_pyav() -> bool:
    """Whether frames can be read from the container directly. Asked once."""
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
        self._music_cache: dict[tuple[str, int, int], tuple[list[float], list[float]]] = {}
        # The same amnesia, far more expensive: scene detection is a subprocess launch and a
        # full decode, around ten seconds a clip, and it ran once per job. A hundred outputs of
        # one recording analysed that recording a hundred times — some seventeen minutes spent
        # re-deriving an answer that cannot have changed. Scenes depend on the file, not on the
        # job, so they are held per file.
        self._scene_cache: dict[tuple, tuple[list[dict[str, Any]], list[str]]] = {}

    async def analyze_video(self, media: MediaItem, music: MediaItem | None = None) -> AnalysisResult:
        warnings: list[str] = []
        scenes = self.detect_scenes(Path(media.path), warnings)
        beats, energy = self.analyze_music(Path(music.path), warnings) if music else ([], [])
        return AnalysisResult(
            media_id=media.id, scenes=scenes, beats=beats, energy=energy, warnings=warnings,
        )

    def analyze_music(self, audio_path: Path, warnings: list[str]) -> tuple[list[float], list[float]]:
        """Beat times and a loudness curve, read from the track in one pass.

        Both come from the same decode, so asking for them together costs no more than asking
        for either. They are used in different places and are independently optional: beats
        move cut points onto the music, loudness lets cut length follow it.
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

    def _read_music(self, audio_path: Path, warnings: list[str]) -> tuple[list[float], list[float]]:
        cache_dir = generated_path("cache", "numba")
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("NUMBA_CACHE_DIR", str(cache_dir))

        try:
            import librosa
        except Exception as exc:
            warnings.append(f"librosa unavailable: {exc}")
            return [], []

        try:
            y, sr = librosa.load(str(audio_path), mono=True)
        except Exception as exc:
            warnings.append(f"Music could not be read: {exc}")
            return [], []

        beats: list[float] = []
        try:
            _tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
            beats = [float(t) for t in librosa.frames_to_time(beat_frames, sr=sr)]
        except Exception as exc:
            warnings.append(f"Beat detection failed: {exc}")

        energy: list[float] = []
        try:
            rms = librosa.feature.rms(y=y)[0]
            energy = self._normalise_curve([float(value) for value in rms])
        except Exception as exc:
            warnings.append(f"Loudness analysis failed: {exc}")

        return beats, energy

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
        self._score_scenes(video_path, scenes, notes)
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
        spans = [(float(scene.get("start", 0.0)), float(scene.get("end", 0.0))) for scene in scenes]
        scores, problem = score_shots(video_path, spans)
        if problem:
            warnings.append(problem)
        for scene, score in zip(scenes, scores):
            scene["quality"] = round(score.quality, 4)
            scene["sharpness"] = round(score.sharpness, 4)
            scene["exposure"] = round(score.exposure, 4)
            scene["motion"] = round(score.motion, 4)
            scene["steadiness"] = round(score.steadiness, 4)
            scene["colour"] = [round(value, 2) for value in score.colour]
            scene["fingerprint"] = score.fingerprint
        if scores:
            weak = sum(1 for score in scores if score.quality < 0.35)
            if weak:
                warnings.append(f"{weak}/{len(scores)} 个镜头质量偏低，已在选择时降权")

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
                    "score": 1.0,
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
    ) -> list[dict[str, Any]]:
        """Cut the recording along the cruise's own spans and label what each stretch is.

        Every scene that comes out of here carries a `kind` — whether the robot was parked,
        travelling, or working a point that failed — which is the distinction the planner
        could not previously draw at all, since the sidecar recorded arrivals and no
        departures.
        """
        spans = self._cruise_spans(segments)
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
            if end - start < 0.5:
                continue
            # Classified on the midpoint: a boundary belongs to both neighbours, the middle
            # of a stretch belongs to exactly one.
            kind, label = self._span_at((start + end) / 2.0, spans)
            merged.append(
                {
                    "start": round(start, 3),
                    "end": round(end, 3),
                    "score": 1.0,
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
        return self.analyze_music(audio_path, warnings)[0]

    def _scene_input(self, video_path: Path, warnings: list[str]) -> Path:
        """What to run detection over: the recording itself where possible.

        The proxy is a full transcode of every source, and it existed for one reason — the
        OpenCV backend could not get a usable timestamp out of these files. PyAV reads them
        from the container and has no such trouble, so where PyAV is present the recording is
        read directly and the transcode is skipped entirely.

        It remains the fallback, because a machine without PyAV is back to the decoder that
        needed it.
        """
        if _has_pyav():
            return video_path
        try:
            if self._timestamps_need_repair(video_path):
                proxy = self._create_analysis_proxy(video_path)
                warnings.append(f"Using timestamp-repaired analysis proxy: {proxy.name}")
                return proxy
        except Exception as exc:
            warnings.append(f"Timestamp preflight failed: {exc}")
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
