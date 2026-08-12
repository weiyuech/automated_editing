"""Tests for the subtitle layer.

The bug this feature actually shipped with, twice over, was not a wrong number — it was a render
that succeeded and looked plausible while doing the wrong thing. FFmpeg exited 0 with text on
screen in a font nobody chose, because libass answers a font name it cannot find by quietly
substituting a system one. Nothing that inspects the arguments, the cue list or the exit code
catches that. So the tests that matter here go and look at the pixels.
"""

from __future__ import annotations

import asyncio
import itertools
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest

from automated_video_editing_backend.core.models import (
    EditTimeline,
    SubtitleCue,
    SubtitleTrack,
    TimelineClip,
)
from automated_video_editing_backend.services import subtitles
from automated_video_editing_backend.services.render import RenderService, _filter_argument

NARRATION = "机器人从大厅出发，缓缓驶过展区。前方是新品体验台，这里陈列着今年的旗舰产品。"


def _words(text: str = NARRATION, step_ms: int = 190, hold_ms: int = 180):
    """One provider-shaped record per character, which is how Volcengine reports Chinese."""
    return [
        {"word": char, "start_time": index * step_ms, "end_time": index * step_ms + hold_ms}
        for index, char in enumerate(text)
    ]


@pytest.fixture(scope="module")
def ffmpeg() -> str:
    """The binary for the pixel tests, or a skip naming what is missing.

    Skipping rather than failing, because these need assets a checkout does not carry — see
    `test_every_offered_font_is_really_installed`, which is the one that fails when they are
    absent. Fonts are checked here too so that an unprepared tree reports "run the script" once,
    instead of four tests failing with a plausible-looking accusation of font substitution.
    """
    binary = RenderService().ffmpeg_binary()
    if not RenderService().supports_subtitles():
        pytest.skip(f"{binary} has no libass; run scripts/prepare_assets.py")
    if subtitles.missing_fonts():
        pytest.skip(f"fonts not fetched: {subtitles.missing_fonts()}; run scripts/prepare_assets.py")
    return binary


@pytest.fixture
def fonts_present() -> None:
    """Skip anything that needs the fetched fonts, so only the guard test reports them missing."""
    if subtitles.missing_fonts():
        pytest.skip("fonts not fetched; run scripts/prepare_assets.py")


@pytest.fixture(scope="module")
def source(tmp_path_factory, ffmpeg) -> str:
    """A flat grey clip. Flat so that any pixel that changes is text, not the picture."""
    path = tmp_path_factory.mktemp("subs") / "src.mp4"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi",
         "-i", "color=c=gray:size=640x360:rate=25:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )
    return str(path)


def _render(tmp_path, source: str, ffmpeg: str, name: str, *, font: str | None,
            width: int = 640, height: int = 360) -> np.ndarray:
    """Render one clip and return a frame from inside the cue, as pixels."""
    track = None
    if font is not None:
        track = SubtitleTrack(
            cues=[SubtitleCue(start=0.2, end=2.8, text="机器人开始巡航")], font=font
        )
    timeline = EditTimeline(
        title=name,
        output_path=str(tmp_path / f"{name}.mp4"),
        output_width=width,
        output_height=height,
        clips=[TimelineClip(media_id="m", source_path=source, start=0, duration=2.9,
                            timeline_start=0)],
        subtitles=track,
    )
    out = asyncio.run(RenderService().render(timeline))
    frame = tmp_path / f"{name}.png"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-ss", "1.5", "-i", out,
         "-frames:v", "1", "-update", "1", str(frame)],
        check=True,
    )
    image = cv2.imread(str(frame), cv2.IMREAD_GRAYSCALE)
    assert image is not None, f"could not read back {frame}"
    return image


def test_subtitles_reach_the_picture_and_not_merely_the_arguments(tmp_path, source, ffmpeg):
    """The export must differ from the same export without subtitles.

    A render that builds a correct filtergraph, writes a correct .ass and produces a video with
    no text on it exits 0 and passes every test that stops at the arguments.
    """
    plain = _render(tmp_path, source, ffmpeg, "plain", font=None)
    subtitled = _render(tmp_path, source, ffmpeg, "subbed", font="noto_sans_sc")

    changed = int(np.count_nonzero(cv2.absdiff(plain, subtitled) > 40))
    assert changed > 500, f"only {changed} pixels changed; nothing was drawn"

    # And it was drawn where subtitles go, not smeared over the whole frame.
    top_half = cv2.absdiff(plain, subtitled)[: plain.shape[0] // 2]
    assert np.count_nonzero(top_half > 40) == 0, "something was drawn outside the subtitle area"


def test_each_bundled_font_renders_differently(tmp_path, source, ffmpeg):
    """The three fonts must produce three different pictures.

    This is the test for the failure that actually happened. Asking libass for a family name no
    bundled font declares is not an error: it substitutes a system font and renders. Every one of
    the three then comes out in the same typeface, and the only evidence is the pixels — which is
    exactly what a test asserting `font == "smiley_sans"` would never see.
    """
    rendered = {
        key: _render(tmp_path, source, ffmpeg, f"font-{key}", font=key)
        for key in subtitles.BUNDLED_FONTS
    }
    keys = list(rendered)
    for first in range(len(keys)):
        for second in range(first + 1, len(keys)):
            a, b = keys[first], keys[second]
            differing = int(np.count_nonzero(cv2.absdiff(rendered[a], rendered[b]) > 40))
            assert differing > 200, (
                f"{a} and {b} rendered near-identically ({differing} px differ) — "
                "libass probably substituted one system font for both"
            )


def test_text_stays_inside_the_frame_in_both_orientations(tmp_path, source, ffmpeg):
    """Portrait is not a special case with its own numbers; it is the same fractions.

    A layout in pixels looks right on the frame it was tuned for and runs off the side of the
    other one, which is the thing the fraction-based geometry exists to prevent.
    """
    for width, height, name in ((640, 360, "land"), (360, 640, "port")):
        plain = _render(tmp_path, source, ffmpeg, f"plain-{name}", font=None,
                        width=width, height=height)
        subbed = _render(tmp_path, source, ffmpeg, f"sub-{name}", font="noto_sans_sc",
                         width=width, height=height)
        diff = cv2.absdiff(plain, subbed)
        assert np.count_nonzero(diff > 40) > 300, f"{name}: no text drawn"

        drawn = np.argwhere(diff > 40)
        rows, columns = drawn[:, 0], drawn[:, 1]
        assert rows.min() > 0 and rows.max() < height - 1, f"{name}: text touches top/bottom edge"
        assert columns.min() > 0 and columns.max() < width - 1, f"{name}: text touches a side"
        # And it sits in the lower part of the frame, not floating in the middle.
        assert rows.min() > height * 0.5, f"{name}: subtitles are not near the bottom"


# ── the timing relationship, which is the whole design ───────────────────────────────────────


def _dialogue_times(script: str) -> list[tuple[str, str]]:
    return re.findall(r"^Dialogue: \d+,([\d:.]+),([\d:.]+),", script, flags=re.MULTILINE)


def test_moving_the_voiceover_moves_the_subtitles_by_the_same_amount(tmp_path):
    """One field moves the narration and its text together.

    Cues are stored against the voiceover, never against the timeline, so there is no second copy
    of the timing to forget to update. This asserts the two really are driven by the same number:
    the audio delay and the subtitle timestamps have to shift by the same amount, from the same
    edit.
    """
    cues = [SubtitleCue(start=1.0, end=3.0, text="一"), SubtitleCue(start=3.5, end=5.0, text="二")]

    def build(offset: float):
        timeline = EditTimeline(
            title="t",
            output_path=str(tmp_path / f"o{offset}.mp4"),
            clips=[TimelineClip(media_id="m", source_path=str(tmp_path / "a.mp4"),
                                start=0, duration=6, timeline_start=0)],
            voiceover_path=str(tmp_path / "voice.mp3"),
            voiceover_start_seconds=offset,
            subtitles=SubtitleTrack(cues=cues),
        )
        script = RenderService().write_subtitle_script(timeline)
        args = RenderService().build_ffmpeg_args(timeline, script)
        return script.read_text(encoding="utf-8"), " ".join(args)

    still, still_args = build(0.0)
    moved, moved_args = build(2.5)

    assert _dialogue_times(still) == [("0:00:01.00", "0:00:03.00"), ("0:00:03.50", "0:00:05.00")]
    assert _dialogue_times(moved) == [("0:00:03.50", "0:00:05.50"), ("0:00:06.00", "0:00:07.50")]
    # The audio moved by the same 2.5s, from the same field.
    assert "adelay=2500" in moved_args
    assert "adelay" not in still_args


def test_recut_subtitles_follow_the_soundtrack_they_came_with(tmp_path):
    """手动微调 has no voiceover input, so the cues have to hang off the audio bed instead.

    The bed is a slice of an earlier export's mixed soundtrack: it starts `source_start` inside
    that export and lands at `timeline_start` here, so a line spoken at t in the original is now
    spoken at `t - source_start + timeline_start`. Reading the offset from the same two fields
    that place the audio is what stops the words and the voice drifting apart when someone drags
    the sound.
    """
    from automated_video_editing_backend.core.models import TimelineAudioBed

    # As saved beside the original export: times in that export's own timeline.
    carried = [SubtitleCue(start=2.0, end=4.0, text="第一句"),
               SubtitleCue(start=6.0, end=8.0, text="第二句")]
    timeline = EditTimeline(
        title="recut",
        output_path=str(tmp_path / "recut.mp4"),
        clips=[TimelineClip(media_id="m", source_path=str(tmp_path / "x.mp4"),
                            start=0, duration=6, timeline_start=0)],
        audio_bed=TimelineAudioBed(source_path=str(tmp_path / "orig.mp4"),
                                   source_start=5.0, timeline_start=1.0),
        subtitles=SubtitleTrack(cues=carried),
    )
    service = RenderService()
    assert service.subtitle_offset(timeline) == -4.0

    script = service.write_subtitle_script(timeline).read_text(encoding="utf-8")
    # 第一句 was spoken before the kept stretch begins, so it is gone rather than clamped to 0
    # and shown over a line nobody is saying.
    assert "第一句" not in script
    assert _dialogue_times(script) == [("0:00:02.00", "0:00:04.00")]

    # And the voiceover field, which governs the auto-planned path, is not what did it.
    assert timeline.voiceover_start_seconds == 0


def test_the_cue_file_is_found_from_either_the_video_or_its_own_path(tmp_path):
    """Callers hold one of two things, and both have to resolve to the same file.

    The job records the sidecar's own path on the media item; an older export has nothing
    recorded and is found from the video instead. Deriving the name blindly turns the first into
    `x.subtitles.subtitles.json`, which never exists — so the cues fail to load, 手动微调 renders
    with no subtitles, and nothing anywhere reports a problem.
    """
    service = RenderService()
    video = tmp_path / "巡航成片.mp4"
    sidecar = service.subtitle_sidecar_path(video)
    assert sidecar.name == "巡航成片.subtitles.json"
    assert service.subtitle_sidecar_path(sidecar) == sidecar

    sidecar.write_text(
        '{"cues": [{"start": 1.0, "end": 2.0, "text": "一"}]}', encoding="utf-8"
    )
    from_video = service.read_subtitle_sidecar(video)
    from_sidecar = service.read_subtitle_sidecar(str(sidecar))
    assert from_video == from_sidecar and from_video is not None

    # An export with no subtitles, and an unreadable file, are both "nothing to carry" rather
    # than an exception in the middle of a render.
    assert service.read_subtitle_sidecar(tmp_path / "absent.mp4") is None
    broken = tmp_path / "broken.mp4"
    service.subtitle_sidecar_path(broken).write_text("{not json", encoding="utf-8")
    assert service.read_subtitle_sidecar(broken) is None


def test_the_two_ways_of_placing_narration_do_not_interfere(tmp_path):
    """A voiceover input wins over a bed, because only one of them is the narration.

    An auto-planned export can carry both: its own voiceover, plus original camera audio. If the
    bed were allowed to move the cues there, adding background sound would shift the subtitles.
    """
    from automated_video_editing_backend.core.models import TimelineAudioBed

    timeline = EditTimeline(
        title="both",
        output_path=str(tmp_path / "both.mp4"),
        clips=[TimelineClip(media_id="m", source_path=str(tmp_path / "x.mp4"),
                            start=0, duration=6, timeline_start=0)],
        voiceover_path=str(tmp_path / "voice.mp3"),
        voiceover_start_seconds=1.5,
        audio_bed=TimelineAudioBed(source_path=str(tmp_path / "bed.mp4"),
                                   source_start=9.0, timeline_start=0.0),
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0.0, end=2.0, text="一")]),
    )
    assert RenderService().subtitle_offset(timeline) == 1.5


def test_cues_are_ordered_and_never_overlap():
    """Two subtitles on screen at once is a timing bug that reads as a rendering bug."""
    cues = subtitles.cues_from_words(_words(), subtitles.SubtitleStyle(), 1280, 720)
    assert len(cues) >= 2
    for earlier, later in itertools.pairwise(cues):
        assert earlier.end <= later.start, f"{earlier} overlaps {later}"
        assert earlier.start < earlier.end


def test_every_spoken_character_survives_into_some_cue():
    """No word may be dropped, and none may appear twice.

    `wrap_cue` duplicated the tail of every wrapped line when it was first written, which looked
    entirely reasonable in the cue list and only showed up when read aloud against the audio.
    """
    words = _words()
    style = subtitles.SubtitleStyle()
    for width, height in ((1280, 720), (720, 1280)):
        cues = subtitles.cues_from_words(words, style, width, height)
        capacity = subtitles.line_capacity(width, height, style)
        rebuilt = "".join(
            subtitles.wrap_cue(cue.text, capacity, style.max_lines).replace("\\N", "")
            for cue in cues
        )
        spoken = "".join(word["word"] for word in words)
        # Punctuation is deliberately trimmed where a cue ends, so compare on the characters.
        dropped = [char for char in spoken if char not in "。，" and char not in rebuilt]
        assert not dropped, f"{width}x{height}: lost {dropped}"
        assert len(rebuilt) <= len(spoken), f"{width}x{height}: characters were duplicated"


def test_a_cue_is_on_screen_while_its_own_words_are_spoken():
    """Timings come from the words, not from dividing the running time up evenly."""
    words = _words()
    cues = subtitles.cues_from_words(words, subtitles.SubtitleStyle(), 1280, 720)
    spoken = subtitles.normalise_words(words)
    for cue in cues:
        overlapping = [
            text for text, start, end in spoken if start < cue.end and end > cue.start
        ]
        assert cue.text[0] in "".join(overlapping), (
            f"cue {cue.text!r} is shown at {cue.start:.2f}-{cue.end:.2f}, "
            "when its own words are not being said"
        )


def test_portrait_lines_are_shorter_than_landscape_lines():
    """The same narration has to break differently on a frame half as wide.

    Two different things adapt, and only one of them is the cue list. Where the narration is
    punctuated, cues follow the sentences and are the same at either aspect — it is the wrapping
    into lines that changes. Where a sentence runs on past what the frame can hold, the cue list
    changes too.
    """
    style = subtitles.SubtitleStyle()
    wide = subtitles.line_capacity(1280, 720, style)
    tall = subtitles.line_capacity(720, 1280, style)
    assert tall < wide, "line capacity did not adapt to the frame"

    # Punctuated: same cues, different line breaks.
    words = _words()
    landscape = subtitles.cues_from_words(words, style, 1280, 720)
    portrait = subtitles.cues_from_words(words, style, 720, 1280)
    assert [cue.text for cue in landscape] == [cue.text for cue in portrait]
    long_cue = max(portrait, key=lambda cue: subtitles.display_width(cue.text))
    assert "\\N" not in subtitles.wrap_cue(long_cue.text, wide, style.max_lines)
    assert "\\N" in subtitles.wrap_cue(long_cue.text, tall, style.max_lines)

    # Unpunctuated and spoken quickly, so width rather than time decides: the narrow frame has
    # to cut it into more cues.
    runon = _words("巡航路线经过大厅接待区玻璃连廊会议室以及顶层露台再回到出发点", step_ms=60, hold_ms=55)
    assert len(subtitles.cues_from_words(runon, style, 720, 1280)) > len(
        subtitles.cues_from_words(runon, style, 1280, 720)
    )


# ── the ways this can fail quietly ───────────────────────────────────────────────────────────


def test_the_fonts_directory_holds_only_fonts(fonts_present):
    """libass reads every file in `fontsdir` and rejects what it cannot parse.

    The licence files lived here first, and produced three parse errors per render — noise that
    trains an operator to ignore the log where a real substitution warning would appear.
    """
    strays = [
        path.name
        for path in subtitles.FONT_DIR.iterdir()
        if path.is_file() and path.suffix.lower() not in {".ttf", ".otf", ".ttc"}
    ]
    assert not strays, f"non-font files in fontsdir: {strays}"


def test_every_offered_font_is_really_installed():
    """A font offered in the UI but absent from the build renders as a substitute, silently.

    This is the packaging guard, and the one test here that fails rather than skips when the tree
    has not been prepared. A release that ships offering three fonts it does not carry would put
    every video in whatever the viewer's machine chose, so the build has to stop.
    """
    assert subtitles.missing_fonts() == [], (
        "run scripts/prepare_assets.py — these are offered but not on disk: "
        f"{subtitles.missing_fonts()}"
    )


def test_a_subtitled_export_also_yields_a_clean_master(tmp_path, source, ffmpeg):
    """The master must really have no text in it, not merely a different filename.

    Text burned into a picture cannot be moved afterwards, so re-cutting a delivered file drags
    its subtitles along at the wrong times. The clean copy is what makes 手动微调 able to swap
    the picture underneath — a still, an effect clip — and have the layer drawn again over the
    new arrangement.
    """
    track = SubtitleTrack(cues=[SubtitleCue(start=0.2, end=2.8, text="机器人开始巡航")])
    timeline = EditTimeline(
        title="pair",
        output_path=str(tmp_path / "巡航成片.mp4"),
        output_width=640, output_height=360,
        clips=[TimelineClip(media_id="m", source_path=source, start=0, duration=2.9,
                            timeline_start=0)],
        subtitles=track,
    )
    delivery = asyncio.run(RenderService().render(timeline))
    master = asyncio.run(RenderService().render_master(timeline))

    assert master and master != delivery
    assert Path(master).name == "巡航成片 母版.mp4"
    master_track = RenderService().read_subtitle_sidecar(master)
    assert master_track is not None
    assert master_track["video"] == "巡航成片 母版.mp4"

    def frame_at(path, name):
        out = tmp_path / f"{name}.png"
        subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", "1.5", "-i", path,
                        "-frames:v", "1", "-update", "1", str(out)], check=True)
        return cv2.imread(str(out), cv2.IMREAD_GRAYSCALE)

    delivered, clean = frame_at(delivery, "d"), frame_at(master, "m")
    differing = int(np.count_nonzero(cv2.absdiff(delivered, clean) > 40))
    assert differing > 300, "the two files look identical; the master still has subtitles"

    # And the difference is only where subtitles go, i.e. the master is the same edit.
    top = cv2.absdiff(delivered, clean)[: delivered.shape[0] // 2]
    assert np.count_nonzero(top > 40) == 0, "the master is a different edit, not just untitled"


def test_subtitle_sidecar_records_that_its_soundtrack_contains_voiceover(tmp_path):
    timeline = EditTimeline(
        title="voice metadata",
        output_path=str(tmp_path / "voice.mp4"),
        clips=[TimelineClip(media_id="m", source_path=str(tmp_path / "picture.mp4"),
                            start=0, duration=2, timeline_start=0)],
        voiceover_path=str(tmp_path / "voice.mp3"),
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=1, text="一句")]),
    )

    RenderService().write_subtitle_script(timeline)
    saved = RenderService().read_subtitle_sidecar(timeline.output_path)

    assert saved["has_voiceover"] is True


def test_no_master_is_made_when_there_are_no_subtitles(tmp_path, source, ffmpeg):
    """Nothing to strip means nothing to keep a second copy of."""
    timeline = EditTimeline(
        title="solo",
        output_path=str(tmp_path / "solo.mp4"),
        output_width=640, output_height=360,
        clips=[TimelineClip(media_id="m", source_path=source, start=0, duration=2.0,
                            timeline_start=0)],
    )
    assert asyncio.run(RenderService().render_master(timeline)) is None


def test_the_font_picker_can_actually_draw_each_font(fonts_present):
    """Every offered font must ship a preview subset that covers its own name.

    The picker renders each font's label in that font, because three names in a list say nothing
    about what the typefaces look like. A subset missing a glyph does not show a blank — the
    browser silently falls back to a system font, and the operator picks 得意黑 from a label
    drawn in something else entirely. Same failure as the libass substitution, one layer up.
    """
    for key, font in subtitles.BUNDLED_FONTS.items():
        uri = subtitles.preview_data_uri(key)
        assert uri, f"{key}: no preview subset — run scripts/prepare_assets.py"
        assert uri.startswith("data:font/woff2;base64,")
        # Under 40 KB: the real faces are 10-27 MB and must never be served here by accident.
        assert len(uri) < 40_000, f"{key}: preview is {len(uri)} bytes; the full font got shipped"

        from fontTools.ttLib import TTFont

        subset_font = TTFont(subtitles.PREVIEW_DIR / f"{key}.woff2")
        covered = set()
        for table in subset_font["cmap"].tables:
            covered |= set(table.cmap)
        missing = [char for char in font.label if ord(char) not in covered]
        assert not missing, f"{key}: preview cannot draw its own label; missing {missing}"


def test_narration_cannot_inject_ass_markup():
    """Braces open an override block in ASS; a stray one loses the line or the file."""
    cues = [subtitles.Cue(start=0.0, end=1.0, text="进入{\\an8}展厅 C:\\demo")]
    script = subtitles.to_ass(cues, subtitles.SubtitleStyle(), 1280, 720)
    body = script.rsplit(",,", 1)[-1].strip()
    assert "{" not in body and "}" not in body
    assert "\\an8" not in body
    # The text is still readable, not deleted.
    assert "进入" in body and "展厅" in body


def test_a_windows_path_survives_the_filtergraph():
    r"""`C:\...` in a filter option is read as an option named `C` unless it is escaped."""
    escaped = _filter_argument(r"C:\Program Files\app\subs.ass")
    assert escaped.startswith("'") and escaped.endswith("'")
    assert "\\:" in escaped, "the drive colon was not escaped"
    assert "\\\\" not in escaped, "backslashes should have become forward slashes"


def test_word_timings_that_cannot_be_used_are_told_apart(tmp_path):
    """"No file", "unreadable file" and "no timestamps" call for three different actions."""
    missing, problem = subtitles.load_words(tmp_path / "nope.json")
    assert missing == [] and "找不到" in problem

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    _, problem = subtitles.load_words(broken)
    assert "无法读取" in problem

    empty = tmp_path / "empty.json"
    empty.write_text('{"words": []}', encoding="utf-8")
    _, problem = subtitles.load_words(empty)
    assert "逐字时间戳" in problem


def test_a_word_without_timing_is_dropped_rather_than_defaulted():
    """A record defaulted to 0.0 does not look broken — it looks like a word said at the start,
    and it drags the cue that contains it back to the beginning of the video."""
    records = [
        {"word": "好", "start_time": 1000, "end_time": 1200},
        {"word": "坏", "end_time": 1400},
        {"word": "空", "start_time": "later", "end_time": 1600},
    ]
    assert subtitles.normalise_words(records) == [("好", 1.0, 1.2)]


def test_asking_for_subtitles_without_a_voiceover_says_so(tmp_path):
    """Nothing to subtitle is a thing the operator has to be told, not a silent no-op."""
    from automated_video_editing_backend.core.models import EditJobRequest
    from automated_video_editing_backend.services.timeline import EditPlanner

    warnings: list[str] = []
    request = EditJobRequest(title="t", output_name="t.mp4", subtitles=True)
    assert EditPlanner()._subtitles(request, None, 1280, 720, warnings) is None
    assert warnings and "配音" in warnings[0]

    # Not asked for, so nothing is said.
    quiet: list[str] = []
    off = EditJobRequest(title="t", output_name="t.mp4", subtitles=False)
    assert EditPlanner()._subtitles(off, None, 1280, 720, quiet) is None
    assert quiet == []


def test_the_planner_builds_cues_from_a_real_tts_sidecar(tmp_path, fonts_present):
    """The path the app actually takes: a voiceover media item, its sidecar, and a job that
    asked for subtitles. Tested end to end because the pieces each work in isolation and the
    join between them — where the sidecar lives, and in what shape — is the part that rots."""
    import json

    from automated_video_editing_backend.core.models import EditJobRequest, MediaItem
    from automated_video_editing_backend.services.timeline import EditPlanner

    audio = tmp_path / "旁白-001.mp3"
    audio.write_bytes(b"not really audio")
    sidecar = audio.with_suffix(".json")
    # The shape tts.py writes.
    sidecar.write_text(
        json.dumps({"text": NARRATION, "duration_ms": 8000, "words": _words()},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    voiceover = MediaItem(
        id="v1", name=audio.name, path=str(audio), kind="audio",
        metadata={"role": "tts_voice", "metadata_path": str(sidecar)},
    )

    warnings: list[str] = []
    request = EditJobRequest(title="t", output_name="t.mp4", subtitles=True,
                             subtitle_font="smiley_sans", subtitle_size="large")
    track = EditPlanner()._subtitles(request, voiceover, 1280, 720, warnings)

    assert warnings == []
    assert track is not None and len(track.cues) >= 2
    assert track.font == "smiley_sans"
    assert track.size == subtitles.SIZE_PRESETS["large"]
    assert "机器人" in track.cues[0].text

    # And with the sidecar pointer absent, it is found by the paired stem instead.
    unlinked = MediaItem(id="v2", name=audio.name, path=str(audio), kind="audio",
                         metadata={"role": "tts_voice"})
    assert EditPlanner()._subtitles(request, unlinked, 1280, 720, []) is not None


def test_an_ffmpeg_without_libass_refuses_instead_of_dropping_the_text(tmp_path, monkeypatch):
    """The export must not quietly come out with no subtitles on it.

    An export with the text missing looks the same whether the narration had no timestamps or the
    binary could not draw. Refusing names the cause while the operator can still act on it.
    """
    from automated_video_editing_backend.services import render as render_module

    service = RenderService()
    monkeypatch.setattr(service, "supports_subtitles", lambda: False)
    timeline = EditTimeline(
        title="t",
        output_path=str(tmp_path / "out.mp4"),
        clips=[TimelineClip(media_id="m", source_path=str(tmp_path / "a.mp4"),
                            start=0, duration=2, timeline_start=0)],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0.0, end=1.0, text="字")]),
    )
    with pytest.raises(RuntimeError, match="libass"):
        asyncio.run(service.render(timeline))

    assert render_module._has_libass("/definitely/not/ffmpeg") is False
