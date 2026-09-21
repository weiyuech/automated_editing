"""Exercise bundled native libraries before a full Windows installer is published."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path


async def check_runtime() -> None:
    import numpy as np
    import soundfile
    import soxr

    from automated_video_editing_backend.services.music_selection import features_for, choose_offset
    from automated_video_editing_backend.services.render import RenderService, _filter_argument
    from automated_video_editing_backend.services.semantic import BgeOnnxEmbedder
    from automated_video_editing_backend.services.subtitles import FONT_DIR, PREVIEW_DIR, missing_fonts

    renderer = RenderService()
    ffmpeg, ffprobe = renderer.ffmpeg_binary(), renderer.ffprobe_binary()
    if getattr(sys, "frozen", False):
        bundle = Path(sys._MEIPASS).resolve()
        for executable in (ffmpeg, ffprobe):
            if not Path(executable).resolve().is_relative_to(bundle):
                raise RuntimeError(f"Tool came from outside the bundle: {executable}")
    if missing_fonts() or len(list(PREVIEW_DIR.glob("*.woff2"))) < 3:
        raise RuntimeError("Bundled fonts or font previews are incomplete")

    model = BgeOnnxEmbedder()
    scores = model.similarity(["酒店客房", "汽车展厅"], ["酒店客房"])
    if scores is None or scores.shape != (2, 1) or not np.isfinite(scores).all():
        raise RuntimeError(model.problem or "Bundled BAAI inference failed")

    with tempfile.TemporaryDirectory(prefix="ave-runtime-check-") as temporary:
        root = Path(temporary)
        audio = root / "music.wav"
        samples = np.sin(2 * np.pi * 440 * np.arange(44100 * 4) / 44100).astype(np.float32) * .2
        # Exercise libsndfile and libsoxr DLLs as well as the actual frozen librosa worker.
        soundfile.write(audio, soxr.resample(samples, 44100, 22050), 22050)
        features = await features_for(str(audio), ffmpeg, root / "analysis")
        selection = choose_offset(features, 2, [1])
        if not 0 <= selection["start"] <= 2.01:
            raise RuntimeError("Bundled music analysis returned an invalid selection")

        subtitles = root / "sample.srt"
        subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\n自动剪辑 字幕检查\n", encoding="utf-8")
        video = root / "sample.mp4"
        subprocess.run([
            ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=320x180:d=1:r=10",
            "-vf", f"subtitles='{_filter_argument(subtitles)}':fontsdir='{_filter_argument(FONT_DIR)}'",
            "-c:v", "libx264", "-threads", "2", "-pix_fmt", "yuv420p", str(video),
        ], check=True, timeout=45, capture_output=True)
        probe = subprocess.run([
            ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video),
        ], check=True, timeout=15, capture_output=True, text=True)
        if float(json.loads(probe.stdout)["format"]["duration"]) < .9:
            raise RuntimeError("Bundled subtitle render failed")
    print("Runtime check passed: BAAI/ONNX, tokenizer, librosa worker, audio DLLs, fonts, FFmpeg/FFprobe.")


def main() -> None:
    asyncio.run(check_runtime())
