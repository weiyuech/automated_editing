"""Choose one continuous music excerpt without changing any picture or narration timing.

FFmpeg decoding and librosa analysis run in bounded child processes. Cached features are
shared by different compositions; only the inexpensive offset scoring depends on cut times.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import sys
import tempfile
from zipfile import BadZipFile
from pathlib import Path

import numpy as np

from automated_video_editing_backend.core.paths import GENERATED_DIRS

LOGGER = logging.getLogger(__name__)
SAMPLE_RATE = 11025
HOP = 256
VERSION = 1
ANALYSIS_TIMEOUT = 60


def analyze_pcm(pcm: Path, target: Path):
    import librosa
    from scipy.signal import find_peaks

    y = np.fromfile(pcm, dtype="<i2").astype(np.float32) / 32768
    if not len(y):
        raise ValueError("音乐没有可解码的声音")
    onset = librosa.onset.onset_strength(y=y, sr=SAMPLE_RATE, hop_length=HOP, n_fft=1024, n_mels=48)
    rms = librosa.feature.rms(y=y, frame_length=1024, hop_length=HOP)[0]
    # Strong rhythmic attacks are the audible edit anchors. A full tempo/beat tracker
    # adds eager numba compilation and assumes regular tempo; neither is needed to
    # match fixed picture cuts against the actual music accents.
    peaks, _ = find_peaks(
        onset,
        distance=max(1, int(0.16 * SAMPLE_RATE / HOP)),
        prominence=max(float(np.percentile(onset, 75)) * 0.3, 1e-5),
    )
    # The parent owns this temporary file and atomically publishes it after success.
    with target.open("wb") as stream:
        np.savez_compressed(
            stream,
            onset=onset,
            rms=rms,
            beats=peaks * HOP / SAMPLE_RATE,
            duration=np.array(len(y) / SAMPLE_RATE),
        )


def choose_offset(features, duration: float, cuts: list[float]) -> dict:
    """Score a whole-track grid plus beat-aligned candidates. Earliest wins equal scores."""
    total = float(features["duration"])
    if not math.isfinite(total) or total <= 0 or duration <= 0:
        raise ValueError("音乐或画面时长无效")
    if total <= duration:
        return {"start": 0.0, "loop": total < duration, "score": 0.0}
    rms = np.asarray(features["rms"], dtype=float)
    onset = np.asarray(features["onset"], dtype=float)
    beats = np.asarray(features["beats"], dtype=float)
    if (
        not rms.size
        or not onset.size
        or not np.isfinite(rms).all()
        or not np.isfinite(onset).all()
        or not np.isfinite(beats).all()
    ):
        raise ValueError("音乐分析数据无效")
    room = total - duration
    cuts = sorted({float(t) for t in cuts if math.isfinite(t) and 0 < t < duration})
    # Very large trees need not form a beats × cuts matrix: sample cuts across the timeline.
    if len(cuts) > 128:
        cuts = [cuts[i] for i in np.linspace(0, len(cuts) - 1, 128, dtype=int)]
    candidates = [np.arange(0, room, 0.1), np.array([room])]
    for cut in [0, duration, *cuts[:32]]:
        offsets = beats - cut
        candidates.append(offsets[(offsets >= 0) & (offsets <= room)])
    starts = np.unique(np.concatenate(candidates))
    # Limit candidate growth for hour-long recordings; keep a uniform view of the whole song.
    if starts.size > 60000:
        starts = starts[np.linspace(0, starts.size - 1, 60000, dtype=int)]
    times = np.arange(rms.size) * HOP / SAMPLE_RATE
    threshold = max(float(rms.max()) * 0.06, 0.001)
    active = (rms >= threshold).astype(float)
    prefix = np.concatenate(([0.0], np.cumsum(active)))
    a = np.clip((starts * SAMPLE_RATE / HOP).astype(int), 0, rms.size - 1)
    b = np.clip(((starts + duration) * SAMPLE_RATE / HOP).astype(int), a + 1, rms.size)
    coverage = (prefix[b] - prefix[a]) / (b - a)
    onset_times = np.arange(onset.size) * HOP / SAMPLE_RATE
    strength = onset / max(float(np.percentile(onset, 95)), 1e-6)

    def beat_match(points):
        if not beats.size:
            return np.zeros_like(points)
        right = np.searchsorted(beats, points).clip(0, len(beats) - 1)
        left = np.maximum(0, right - 1)
        distance = np.minimum(np.abs(points - beats[right]), np.abs(points - beats[left]))
        return np.exp(-0.5 * (distance / 0.10) ** 2)

    match = np.zeros_like(starts)
    for cut in cuts:
        at = starts + cut
        # Both regular beats and actual strong attacks can support a visible cut.
        match += 0.7 * beat_match(at) + 0.3 * np.interp(at, onset_times, strength).clip(0, 1)
    if cuts:
        match /= len(cuts)
    endpoints = (beat_match(starts) + beat_match(starts + duration)) * 0.5
    # Prefer gentle exits to cutting through a loud passage. Avoid selecting silent windows.
    relative_rms = rms / max(float(np.percentile(rms, 95)), 1e-6)
    gentle_end = 1 - np.interp(starts + duration - 0.08, times, relative_rms).clip(0, 1)
    opening = np.interp(starts + min(0.4, duration / 4), times, relative_rms).clip(0, 1)
    scores = 3.0 * coverage + 1.8 * match + 0.65 * endpoints + 0.25 * gentle_end + 0.25 * opening
    best = int(np.argmax(scores))
    return {"start": float(starts[best]), "loop": False, "score": float(scores[best])}


async def _run(args, timeout, env=None):
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, env=env
    )
    try:
        _, stderr = await asyncio.wait_for(process.communicate(), timeout)
        if process.returncode:
            raise RuntimeError(stderr.decode(errors="replace")[-1000:] or "音乐分析失败")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _read_features(path):
    with np.load(path, allow_pickle=False) as data:
        features = {key: data[key] for key in ("duration", "rms", "onset", "beats")}
    duration = features["duration"]
    if duration.ndim != 0 or not np.isfinite(duration) or duration <= 0:
        raise ValueError("音乐缓存时长无效")
    for name in ("rms", "onset", "beats"):
        values = features[name]
        if values.ndim != 1 or not np.isfinite(values).all():
            raise ValueError("音乐缓存特征无效")
    if not features["rms"].size or not features["onset"].size:
        raise ValueError("音乐缓存为空")
    return features


async def features_for(path: str, ffmpeg: str, cache_dir: Path | None = None):
    source = Path(path).resolve()
    stat = source.stat()
    identity = [str(source), stat.st_size, stat.st_mtime_ns, VERSION]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    cache = cache_dir or GENERATED_DIRS["cache"] / "music-analysis"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (key + ".npz")
    if target.is_file():
        try:
            return await asyncio.to_thread(_read_features, target)
        except (OSError, ValueError, KeyError, BadZipFile):
            target.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="music-", dir=cache) as temporary:
        pcm = Path(temporary) / "audio.pcm"
        result = Path(temporary) / "features.npz"
        await _run(
            [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(source),
                "-vn",
                "-ac",
                "1",
                "-ar",
                str(SAMPLE_RATE),
                "-f",
                "s16le",
                str(pcm),
            ],
            40,
        )
        args = (
            [sys.executable, "--music-analysis-worker"]
            if getattr(sys, "frozen", False)
            else [sys.executable, "-m", __name__]
        )
        # Keep compilation cache writable in packaged installs and bound the BLAS thread pool.
        env = {
            **os.environ,
            "NUMBA_CACHE_DIR": str(cache / "numba"),
            "OPENBLAS_NUM_THREADS": "1",
            "OMP_NUM_THREADS": "1",
        }
        await _run([*args, str(pcm), str(result)], ANALYSIS_TIMEOUT, env)
        features = await asyncio.to_thread(_read_features, result)
        if source.stat().st_mtime_ns != stat.st_mtime_ns or source.stat().st_size != stat.st_size:
            raise ValueError("分析期间音乐文件已变化，请重试")
        result.replace(target)
        return features


def picture_cuts(timeline) -> list[float]:
    """Include inner shots even when a saved composition is one MP4 input."""
    from automated_video_editing_backend.services.composition_assets import manifest_path

    boundaries = set(timeline.planning_diagnostics.get("music_cut_times", []))
    boundaries.update(marker.timestamp for marker in timeline.markers)

    def visit(nodes, clip):
        for node in nodes:
            start = float(node["start"])
            if clip.start <= start < clip.start + clip.duration:
                boundaries.add(clip.timeline_start + start - clip.start)
            visit(node.get("children", []), clip)

    for clip in timeline.clips:
        boundaries.add(clip.timeline_start)
        try:
            manifest = json.loads(manifest_path(clip.source_path).read_text(encoding="utf-8"))
            visit(manifest.get("composition_tree", []), clip)
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return sorted(t - timeline.music_delay_seconds for t in boundaries)


async def prepare_music(timeline, renderer, progress=None):
    if not timeline.music_path:
        return
    total = renderer._timeline_duration(timeline)
    duration = timeline.music_duration_seconds or max(0, total - timeline.music_delay_seconds)
    if duration <= 0:
        return
    stat = Path(timeline.music_path).stat()
    cuts = picture_cuts(timeline)
    identity = [timeline.music_path, stat.st_size, stat.st_mtime_ns, duration, cuts, VERSION]
    previous = timeline.planning_diagnostics.get("music_selection", {})
    if previous.get("identity") == identity:
        return
    if progress:
        progress({"stage": "preparing", "message": "正在自动选取音乐片段"})
    timeline.music_duration_seconds = duration
    try:
        source_duration = await asyncio.to_thread(renderer.probe_duration, timeline.music_path)
        if source_duration is None or not math.isfinite(source_duration) or source_duration <= 0:
            raise ValueError("无法读取音乐时长")
        if source_duration > 3600:
            raise ValueError("音乐超过一小时，跳过节奏分析")
        if source_duration <= duration:
            selection = {"start": 0.0, "loop": source_duration < duration, "score": 0.0}
        else:
            features = await features_for(timeline.music_path, renderer.ffmpeg_binary())
            selection = await asyncio.to_thread(choose_offset, features, duration, cuts)
        timeline.music_start_seconds = selection["start"]
        timeline.music_loop = selection["loop"]
        timeline.music_evidence = "structured"
        timeline.planning_diagnostics["music_selection"] = {**selection, "identity": identity}
    except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as exc:
        LOGGER.warning("Music excerpt analysis failed: %s", exc)
        timeline.music_start_seconds = 0
        source_duration = await asyncio.to_thread(renderer.probe_duration, timeline.music_path)
        timeline.music_loop = bool(source_duration and 0 < source_duration < duration)
        timeline.music_evidence = "unreadable"
        timeline.planning_diagnostics["music_selection"] = {"identity": identity, "fallback": True}
        message = "音乐自动选段未完成，已从头播放并按成片时长截取。"
        if message not in timeline.warnings:
            timeline.warnings.append(message)


def worker_main():
    analyze_pcm(Path(sys.argv[-2]), Path(sys.argv[-1]))


if __name__ == "__main__":
    worker_main()
