"""Frame and subtitle presentation shared by controlled composition and manual refinement."""

from pathlib import Path
import json
from automated_video_editing_backend.core.models import (
    DEFAULT_OUTPUT_WIDTH,
    DEFAULT_OUTPUT_HEIGHT,
    EditJobRequest,
    MediaItem,
    SubtitleTrack,
    SubtitleCue,
)
from automated_video_editing_backend.services import subtitles


class EditPlanner:
    def source_frame(self, source_size: tuple[int, int] | None) -> tuple[int, int]:
        """Keep the source canvas, bounded only by the timeline model and encoder needs."""
        if not source_size or source_size[0] <= 0 or source_size[1] <= 0:
            return DEFAULT_OUTPUT_WIDTH, DEFAULT_OUTPUT_HEIGHT
        width, height = source_size
        scale = min(1.0, 7680 / max(width, height))
        width = max(16, int(width * scale) // 2 * 2)
        height = max(16, int(height * scale) // 2 * 2)
        return width, height

    def _subtitles(
        self,
        request: EditJobRequest,
        voiceover: MediaItem | None,
        width: int,
        height: int,
        warnings: list[str],
        voiceover_duration: float | None = None,
    ) -> SubtitleTrack | None:
        """Cues for the narration, or nothing plus a reason.

        Exact provider timings are preferred. If they cannot be trusted, reviewed narration text
        is estimated over the actual audio duration. Every way this can still come back empty
        says why, while the narration itself remains in the finished video.
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

        try:
            mapped = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            mapped = {}
        if isinstance(mapped, dict) and mapped.get("mapped_cues") is not None:
            font = self._subtitle_font(request, warnings)
            if font is None:
                return None
            if mapped.get("timing_quality") != "exact":
                warnings.append("部分旁白没有可靠逐字时间戳，按实际语音区间显示整句")
            style = subtitles.SubtitleStyle(
                font=font,
                size=subtitles.SIZE_PRESETS.get(
                    getattr(request, "subtitle_size", "medium"), subtitles.SIZE_PRESETS["medium"]
                ),
            )
            cues = []
            # Reflow reliable words for this canvas/font. Missing provider clocks retain
            # one utterance interval; never invent word timing from character counts.
            if mapped.get("whole_audio") and mapped.get("timing_quality") == "exact":
                cues = [
                    SubtitleCue(start=cue.start, end=cue.end, text=cue.text)
                    for cue in subtitles.cues_from_words(
                        mapped.get("words", []), style, width, height
                    )
                ]
            for slot in mapped.get("bindings", []):
                if slot.get("timing_quality") == "provider_words" and slot.get("words"):
                    local = subtitles.cues_from_words(slot["words"], style, width, height)
                    cues.extend(
                        SubtitleCue(
                            start=cue.start + slot["start"],
                            end=min(slot["actual_seconds"], cue.end) + slot["start"],
                            text=cue.text,
                        )
                        for cue in local
                    )
                else:
                    cues.append(
                        SubtitleCue(
                            start=slot["start"],
                            end=slot["start"] + slot["actual_seconds"],
                            text=slot["text"],
                        )
                    )
            return SubtitleTrack(
                cues=cues or [SubtitleCue.model_validate(cue) for cue in mapped["mapped_cues"]],
                font=font,
                size=style.size,
                timing_quality="exact" if mapped.get("timing_quality") == "exact" else "estimated",
            )

        raw_words, timing_quality, problem = subtitles.load_words_or_estimate(
            metadata_path,
            audio_duration_seconds=voiceover_duration,
        )
        if timing_quality is None:
            warnings.append(f"字幕已跳过：{problem}；成片仍保留旁白")
            return None
        if timing_quality == "estimated":
            warnings.append("旁白逐字时间不够可靠，已生成估算字幕")

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
            timing_quality=timing_quality,
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
