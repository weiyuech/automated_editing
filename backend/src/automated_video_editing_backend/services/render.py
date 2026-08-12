from __future__ import annotations

import asyncio
import functools
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

from automated_video_editing_backend.core.models import EditTimeline
from automated_video_editing_backend.services import subtitles as subtitle_layer

# Music sits under narration at this level. amix's own normalisation is switched off, so this
# is the level you actually hear; with normalisation left on, ffmpeg halved both tracks and the
# narration came out at 0.5 while the bed fell to 0.14.
MUSIC_BED_VOLUME = 0.25
# Original camera audio is background too when it is kept at all.
ORIGINAL_AUDIO_BED_VOLUME = 0.35

# FFmpeg builds fetched by scripts/prepare_assets.py, which are chosen for having libass. The
# one Homebrew currently ships has neither libass nor libfreetype and cannot draw text at all,
# so "whatever is on PATH" is not a safe default when subtitles are wanted.
VENDOR_DIR = (
    Path(sys._MEIPASS) / "vendor" / "ffmpeg"
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
    else Path(__file__).resolve().parent.parent.parent.parent / "vendor" / "ffmpeg"
)


def _platform_key() -> str:
    if os.name == "nt":
        return "win64"
    if platform.system() == "Darwin":
        return f"darwin-{platform.machine()}"
    return "linux64"


@functools.lru_cache(maxsize=8)
def _has_libass(binary: str) -> bool:
    """Whether this FFmpeg was built with libass. Cached — it cannot change while we run."""
    try:
        result = subprocess.run(
            [binary, "-hide_banner", "-version"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "--enable-libass" in result.stdout


def _filter_argument(path: Path | str) -> str:
    r"""Quote a path so it survives being read as a filtergraph option value.

    A filtergraph is parsed before any filter sees it, and `:` separates one option from the
    next. On Windows every absolute path begins `C:\`, so an unescaped one is read as an option
    named `C` — which is not a parse error that names the path, it is a complaint about an
    unknown option. Backslashes are turned into forward slashes, which FFmpeg accepts on
    Windows and which removes the second source of escaping entirely.
    """
    text = str(path).replace("\\", "/")
    for character in ("\\", "'", ":", ",", ";", "[", "]"):
        text = text.replace(character, "\\" + character)
    return f"'{text}'"


class RenderService:
    def build_ffmpeg_args(
        self, timeline: EditTimeline, subtitle_path: Path | str | None = None
    ) -> list[str]:
        if not timeline.clips:
            raise ValueError("Timeline has no clips")

        width = int(timeline.output_width)
        height = int(timeline.output_height)
        crop_x = max(0.0, min(1.0, float(timeline.output_crop_x)))
        crop_y = max(0.0, min(1.0, float(timeline.output_crop_y)))
        args = [self.ffmpeg_binary(), "-y"]
        for clip in timeline.clips:
            if clip.kind == "image":
                # A still has no duration to trim to. Without looping it to the requested
                # length the export gets a single frame — a flash, not a held picture.
                args += ["-loop", "1", "-framerate", "30", "-t", f"{clip.duration:.3f}"]
            args += ["-i", clip.source_path]

        next_index = len(timeline.clips)
        bed_input_index = None
        if timeline.audio_bed:
            bed_input_index = next_index
            args += ["-i", timeline.audio_bed.source_path]
            next_index += 1
        music_input_index = next_index
        if timeline.music_path:
            args += ["-i", timeline.music_path]
            next_index += 1
        voiceover_input_index = next_index
        if timeline.voiceover_path:
            args += ["-i", timeline.voiceover_path]

        filter_parts = []
        for index, clip in enumerate(timeline.clips):
            if timeline.output_fit == "contain":
                picture_fit = (
                    f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                    f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black,"
                )
            else:
                picture_fit = (
                    f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                    f"crop={width}:{height}:(iw-ow)*{crop_x:.6f}:(ih-oh)*{crop_y:.6f},"
                )
            filter_parts.append(
                f"[{index}:v]"
                f"trim=start={clip.start:.3f}:duration={clip.duration:.3f},"
                "setpts=PTS-STARTPTS,"
                # An explicit preset fills and crops. With no preset, the native first-source
                # canvas is retained and differently shaped clips are fitted inside it instead
                # of being silently cropped to the old 16:9 default.
                f"{picture_fit}"
                "fps=30,setsar=1,format=yuv420p"
                f"[v{index}]"
            )
        concat_inputs = "".join(f"[v{index}]" for index in range(len(timeline.clips)))
        filter_parts.append(f"{concat_inputs}concat=n={len(timeline.clips)}:v=1:a=0[vout]")

        video_label = "[vout]"
        if subtitle_path is not None:
            # One filter over the finished picture, rather than anything baked into the cuts.
            # libass reads the script's PlayRes and lays it out for this frame, so the same
            # track works over any video.
            script = _filter_argument(subtitle_path)
            fonts = _filter_argument(subtitle_layer.FONT_DIR)
            filter_parts.append(f"[vout]ass=filename={script}:fontsdir={fonts}[vsub]")
            video_label = "[vsub]"

        sources: list[str] = []
        if timeline.audio_bed and bed_input_index is not None:
            bed = timeline.audio_bed
            steps = [f"[{bed_input_index}:a:0]atrim=start={bed.source_start:.3f}", "asetpts=PTS-STARTPTS"]
            if bed.timeline_start > 0:
                # Something was added ahead of the picture, so the sound moves with it and
                # stays in the relationship it was mixed with.
                delay_ms = int(round(bed.timeline_start * 1000))
                steps.append(f"adelay={delay_ms}:all=1")
            filter_parts.append(",".join(steps) + "[bed]")
            sources.append("[bed]")
        for index, clip in enumerate(timeline.clips):
            if clip.kind != "video" or not clip.include_audio:
                continue
            # Effect sound follows the effect clip, not the soundtrack. Giving every placed
            # effect its own delayed slice lets it be moved/reordered without touching the
            # uninterrupted bed that carries narration and the subtitle clock.
            steps = [
                f"[{index}:a:0]atrim=start={clip.start:.3f}:duration={clip.duration:.3f}",
                "asetpts=PTS-STARTPTS",
                f"volume={clip.audio_volume:.3f}",
            ]
            if clip.timeline_start > 0:
                delay_ms = int(round(clip.timeline_start * 1000))
                steps.append(f"adelay={delay_ms}:all=1")
            label = f"[fxa{index}]"
            filter_parts.append(",".join(steps) + label)
            sources.append(label)
        if timeline.include_original_audio:
            for index, clip in enumerate(timeline.clips):
                filter_parts.append(
                    f"[{index}:a]"
                    f"atrim=start={clip.start:.3f}:duration={clip.duration:.3f},"
                    "asetpts=PTS-STARTPTS,aresample=48000"
                    f"[oa{index}]"
                )
            original_inputs = "".join(f"[oa{index}]" for index in range(len(timeline.clips)))
            filter_parts.append(f"{original_inputs}concat=n={len(timeline.clips)}:v=0:a=1[origraw]")
            bed = ORIGINAL_AUDIO_BED_VOLUME if timeline.voiceover_path else 1.0
            filter_parts.append(f"[origraw]volume={bed}[orig]")
            sources.append("[orig]")
        if timeline.music_path:
            # Full volume on its own; ducked to a bed only when a voice shares the track.
            bed = MUSIC_BED_VOLUME if timeline.voiceover_path else 1.0
            filter_parts.append(f"[{music_input_index}:a:0]volume={bed}[bgm]")
            sources.append("[bgm]")
        if timeline.voiceover_path:
            steps = [f"[{voiceover_input_index}:a:0]volume=1.0"]
            if timeline.voiceover_start_seconds > 0:
                # The same number the subtitles are offset by. Reading it from one field is what
                # keeps the narration and its text on top of each other.
                steps.append(f"adelay={int(round(timeline.voiceover_start_seconds * 1000))}:all=1")
            filter_parts.append(",".join(steps) + "[vox]")
            sources.append("[vox]")

        if sources:
            if len(sources) > 1:
                mixed = "".join(sources)
                filter_parts.append(
                    f"{mixed}amix=inputs={len(sources)}:duration=longest"
                    ":dropout_transition=0:normalize=0[premix]"
                )
                filter_parts.append("[premix]alimiter=limit=0.95[mixed]")
            else:
                filter_parts.append(f"{sources[0]}anull[mixed]")
            # Padding the audio out to infinity makes -shortest land on the video instead. A
            # 5-second narration used to end the whole export at 5 seconds.
            filter_parts.append("[mixed]apad[aout]")

        args += ["-filter_complex", ";".join(filter_parts), "-map", video_label]
        if sources:
            args += ["-map", "[aout]", "-shortest"]
        else:
            args += ["-an"]

        args += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "20",
            "-pix_fmt", "yuv420p",
            str(timeline.output_path),
        ]
        return args

    def subtitle_offset(self, timeline: EditTimeline) -> float:
        """Where the audio the cues belong to begins in this output.

        Cues are always stored against their own audio, never against the timeline — but which
        audio that is depends on how the export was made, and there are two cases:

        *Auto-planned*: the narration is its own input, positioned by `voiceover_start_seconds`.

        *手动微调*: there is no separate narration. The sound is `audio_bed`, a slice of an
        earlier export's mixed soundtrack, and the cues that come with it are timed against that
        earlier export. The bed plays from `source_start` inside it and lands at `timeline_start`
        here, so a cue at time t in the original now falls at `t - source_start + timeline_start`.

        Reading both from the same field the audio itself uses is what makes it impossible for
        the text and the voice to disagree: move the sound, and this number moves with it.
        """
        if timeline.voiceover_path:
            return float(timeline.voiceover_start_seconds)
        bed = timeline.audio_bed
        if bed:
            return float(bed.timeline_start) - float(bed.source_start)
        return float(timeline.voiceover_start_seconds)

    def write_subtitle_script(self, timeline: EditTimeline) -> Path | None:
        """Write the subtitle layer beside the export, and return where it went.

        Kept rather than written to a temporary file and deleted. It is the layer: a text file
        that can be read, hand-corrected and burned over the same video again without going near
        the planner, and the quickest way to see what the narration was understood to say.
        """
        track = timeline.subtitles
        if not track or not track.cues:
            return None
        style = subtitle_layer.SubtitleStyle(
            font=track.font,
            size=track.size,
            side_margin=track.side_margin,
            bottom_margin=track.bottom_margin,
            outline=track.outline,
            shadow=track.shadow,
            primary_colour=track.primary_colour,
            outline_colour=track.outline_colour,
            max_lines=track.max_lines,
        )
        cues = [
            subtitle_layer.Cue(start=cue.start, end=cue.end, text=cue.text) for cue in track.cues
        ]
        offset = self.subtitle_offset(timeline)
        script = subtitle_layer.to_ass(
            cues, style, int(timeline.output_width), int(timeline.output_height), offset=offset,
        )
        path = Path(timeline.output_path).with_suffix(".ass")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_subtitle_sidecar(timeline, track, offset)
        path.write_text(script, encoding="utf-8")
        return path

    SIDECAR_SUFFIX = ".subtitles.json"

    def subtitle_sidecar_path(self, output_path: str | Path) -> Path:
        """Where an export's cue list lives. Idempotent: given the sidecar, returns it unchanged.

        Callers hold either the video or the recorded sidecar path, and `with_suffix` alone turns
        the second into `x.subtitles.subtitles.json` — a file that never exists, so the cues
        simply fail to load and 手动微调 comes out with no subtitles and no error.
        """
        path = Path(output_path)
        if path.name.endswith(self.SIDECAR_SUFFIX):
            return path
        return path.with_suffix(self.SIDECAR_SUFFIX)

    def _write_subtitle_sidecar(self, timeline, track, offset: float) -> None:
        """Record the cues in *this export's own* time, so a later re-cut can pick them up.

        Deliberately not the same numbers as the track carries. A track's cues are relative to
        its audio; written here they are shifted by `offset` into the finished video's timeline.
        That is what makes the file self-contained: 手动微调 sees "this video says these words at
        these times" and needs to know nothing about how the export was originally assembled —
        whether there was a voiceover input, where it sat, or what it was mixed with.

        The `.ass` cannot serve this purpose. It is a rendering of the layer, not the layer:
        already wrapped into lines for one frame size, with punctuation trimmed, and lossy to
        read back.
        """
        payload = {
            "version": 1,
            "video": Path(timeline.output_path).name,
            "has_voiceover": bool(
                timeline.voiceover_path
                or (timeline.audio_bed and timeline.audio_bed.has_voiceover)
            ),
            "font": track.font,
            "size": track.size,
            "side_margin": track.side_margin,
            "bottom_margin": track.bottom_margin,
            "outline": track.outline,
            "shadow": track.shadow,
            "primary_colour": track.primary_colour,
            "outline_colour": track.outline_colour,
            "max_lines": track.max_lines,
            "cues": [
                {"start": round(cue.start + offset, 3), "end": round(cue.end + offset, 3),
                 "text": cue.text}
                for cue in track.cues
            ],
        }
        target = self.subtitle_sidecar_path(timeline.output_path)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")

    def read_subtitle_sidecar(self, output_path: str | Path) -> dict | None:
        """The cue list saved beside an export, or None when it has none or cannot be read."""
        path = self.subtitle_sidecar_path(output_path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("cues") else None

    def master_output_path(self, timeline: EditTimeline) -> Path:
        """Where the subtitle-free copy of an export goes.

        Kept beside the delivered file rather than in a folder of its own, so nothing that
        already knows where exports live has to learn a new shape. What pairs the two is the
        group recorded on each media item, not this name — a name is something an operator can
        change, and grouping that breaks when a file is renamed is grouping that will break.
        """
        path = Path(timeline.output_path)
        return path.with_name(f"{path.stem} 母版{path.suffix}")

    async def render_master(self, timeline: EditTimeline) -> str | None:
        """The same edit without subtitles, for re-cutting in 手动微调.

        Text burned into the picture cannot be moved afterwards, so re-cutting a delivered file
        drags its subtitles along at the wrong times and chops them mid-word. Keeping a clean
        copy is what lets the picture underneath be swapped — a still, an effect clip, anything —
        while the subtitle layer is simply drawn again over the new arrangement.
        """
        if not (timeline.subtitles and timeline.subtitles.cues):
            return None
        target = self.master_output_path(timeline)
        args = self.build_ffmpeg_args(timeline, None)
        args[-1] = str(target)
        await self._run(args)
        # Give the master its own paired timing layer. Generated-media entries are rebuilt by
        # scanning after an app restart, so an in-memory pointer to the delivery's sidecar is
        # not durable. A paired file lets the clean master rediscover its words by filename.
        source_sidecar = self.subtitle_sidecar_path(timeline.output_path)
        target_sidecar = self.subtitle_sidecar_path(target)
        try:
            data = json.loads(source_sidecar.read_text(encoding="utf-8"))
            data["video"] = target.name
            target_sidecar.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass
        return str(target)

    async def render(self, timeline: EditTimeline) -> str:
        subtitle_path = self.write_subtitle_script(timeline)
        if subtitle_path is not None and not self.supports_subtitles():
            # Refused rather than rendered without them. An export that quietly comes out with no
            # text looks exactly like one where the narration had no timings, and the operator
            # would have no way to tell that the cause was the binary.
            raise RuntimeError(
                f"当前 FFmpeg 不能把字幕压进画面（缺少 libass）：{self.ffmpeg_binary()}。"
                "请运行 scripts/prepare_assets.py 获取可用的 FFmpeg。"
            )
        await self._run(self.build_ffmpeg_args(timeline, subtitle_path))
        return timeline.output_path

    async def _run(self, args: list[str]) -> None:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[-2000:])

    def probe_duration(self, path: str) -> float | None:
        """Length in seconds, or None when ffprobe cannot say."""
        try:
            result = subprocess.run(
                [self.ffprobe_binary(), "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        try:
            duration = float(result.stdout.strip())
        except ValueError:
            return None
        return duration if duration > 0 else None

    def probe_frame_size(self, path: str) -> tuple[int, int] | None:
        """Displayed width and height of the first video stream, including rotation."""
        try:
            result = subprocess.run(
                [
                    self.ffprobe_binary(), "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
                    "-of", "json", path,
                ],
                capture_output=True, text=True, timeout=30, check=False,
            )
            payload = json.loads(result.stdout or "{}")
        except (OSError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError):
            return None
        streams = payload.get("streams") or []
        if result.returncode != 0 or not streams:
            return None
        stream = streams[0]
        try:
            width, height = int(stream["width"]), int(stream["height"])
        except (KeyError, TypeError, ValueError):
            return None
        rotation = (stream.get("tags") or {}).get("rotate", 0)
        for side_data in stream.get("side_data_list") or []:
            if "rotation" in side_data:
                rotation = side_data["rotation"]
                break
        try:
            rotated = abs(int(float(rotation))) % 180 == 90
        except (TypeError, ValueError):
            rotated = False
        if rotated:
            width, height = height, width
        return (width, height) if width > 0 and height > 0 else None

    def has_audio_stream(self, path: str) -> bool | None:
        """Whether a source carries audio at all. `None` when that could not be established.

        The three answers are genuinely different and were previously two. Returning False for
        a failed probe made "this clip is silent" and "ffprobe did not run" the same reply, so a
        broken or missing ffprobe muted every source and told the operator their footage had no
        audio track — a statement about their footage that was really a statement about the
        tool. Answers that cannot be distinguished by the caller are how a failure goes unseen.
        """
        try:
            result = subprocess.run(
                [self.ffprobe_binary(), "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=index", "-of", "csv=p=0", path],
                capture_output=True, text=True, timeout=30, check=False,
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return bool(result.stdout.strip())

    def ffmpeg_binary(self) -> str:
        vendored = VENDOR_DIR / _platform_key() / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        candidates = [
            os.environ.get("FFMPEG_BIN"),
            # Ahead of PATH deliberately. The bundled build is the one known to carry libass;
            # the machine's own may not, and which one rendered an export should not depend on
            # what happens to be installed.
            str(vendored),
            shutil.which("ffmpeg"),
            "/opt/homebrew/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/usr/bin/ffmpeg",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise RuntimeError("FFmpeg is not installed or not visible to the app")

    def supports_subtitles(self) -> bool:
        """Whether the active binary can burn in text at all.

        Asked before rendering rather than discovered afterwards. Without libass the `ass` filter
        does not exist, and how FFmpeg reacts to being asked for it varies by build — the case
        that must not happen is the export completing with the text simply absent.
        """
        try:
            return _has_libass(self.ffmpeg_binary())
        except RuntimeError:
            return False

    def ffprobe_binary(self) -> str:
        vendored = VENDOR_DIR / _platform_key() / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        candidates = [
            os.environ.get("FFPROBE_BIN"),
            str(vendored),
            shutil.which("ffprobe"),
            "/opt/homebrew/bin/ffprobe",
            "/usr/local/bin/ffprobe",
            "/usr/bin/ffprobe",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).exists():
                return candidate
        raise RuntimeError("ffprobe is not installed or not visible to the app")
