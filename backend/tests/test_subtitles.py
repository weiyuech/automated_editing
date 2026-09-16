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
import json
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automated_video_editing_backend.api.routes import build_router
from automated_video_editing_backend.core.models import (
    EditTimeline,
    MediaItem,
    SubtitleCue,
    SubtitleTrack,
    TimelineAudioBed,
    TimelineClip,
)
from automated_video_editing_backend.services import subtitles
from automated_video_editing_backend.services.render import RenderService, _filter_argument

NARRATION = "机器人从大厅出发，缓缓驶过展区。前方是新品体验台，这里陈列着今年的旗舰产品。"


@pytest.mark.parametrize(
    "contents",
    [
        "{not valid json",
        json.dumps({
            "video": "另一个成片.mp4",
            "cues": [{"start": 0, "end": 1, "text": "不应静默丢失"}],
        }, ensure_ascii=False),
    ],
)
def test_legacy_export_with_present_invalid_subtitles_is_not_treated_as_subtitle_free(
    monkeypatch, tmp_path, contents,
):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "subtitle-test-token")
    video = tmp_path / "旧成片.mp4"
    video.write_bytes(b"video")
    renderer = RenderService()
    renderer.subtitle_sidecar_path(video).write_text(contents, encoding="utf-8")
    item = MediaItem(id="legacy-export", path=str(video), kind="video", metadata={})

    class MediaStub:
        def get(self, media_id):
            return item if media_id == item.id else None

    unused = object()
    app = FastAPI()
    app.include_router(
        build_router(
            robot=unused,
            capture=unused,
            cruise=unused,
            cruise_routes=unused,
            media=MediaStub(),
            jobs=type("JobsStub", (), {"renderer": renderer})(),
            vault=unused,
            settings=unused,
            llm=unused,
            tts=unused,
            seedance=unused,
            renamer=unused,
            framing_test=unused,
            admin_access=unused,
        ),
        prefix="/api",
    )

    response = TestClient(app).get(
        "/api/subtitles/track",
        params={"media_id": item.id},
        headers={"x-bridge-token": "subtitle-test-token"},
    )

    assert response.status_code == 200
    assert response.json()["track"] is None
    assert "已停止按无字幕处理" in response.json()["problem"]


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
                                start=0, duration=9, timeline_start=0)],
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


def test_carried_track_entirely_outside_the_output_creates_no_subtitle_family(tmp_path):
    """A non-empty source track is not proof that this shorter re-cut contains any text."""
    from automated_video_editing_backend.core.models import TimelineAudioBed

    service = RenderService()
    output = tmp_path / "later-recut.mp4"
    timeline = EditTimeline(
        title="later recut",
        output_path=str(output),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "picture.mp4"),
            start=0, duration=2, timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(
            source_path=str(tmp_path / "earlier-export.mp4"),
            source_start=10,
            timeline_start=0,
            has_voiceover=True,
        ),
        subtitles=SubtitleTrack(cues=[
            SubtitleCue(start=1, end=2, text="already finished"),
            SubtitleCue(start=4, end=5, text="also before the retained sound"),
        ]),
    )

    assert service.output_subtitle_cues(timeline) == []
    assert service.write_subtitle_script(timeline) is None
    assert asyncio.run(service.render_master(timeline)) is None
    assert not output.with_suffix(".ass").exists()
    assert not output.with_suffix(".subtitles.json").exists()
    assert not (tmp_path / "later-recut 母版.mp4").exists()
    assert not (tmp_path / "later-recut 母版.subtitles.json").exists()


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


def test_recut_sidecars_stay_inside_each_finished_video_timeline(tmp_path):
    """A retained soundtrack may begin after some of its old cues, twice in succession.

    Each sidecar is the input to the next manual fine-tune.  It therefore must contain the
    finished video's clock — never negative source-relative times, nor cues beyond the picture
    that will disappear when FFmpeg stops on the video stream.
    """
    service = RenderService()
    first_output = tmp_path / "第一次微调.mp4"
    first = EditTimeline(
        title="first recut",
        output_path=str(first_output),
        clips=[TimelineClip(
            media_id="picture", source_path=str(tmp_path / "picture.mp4"),
            start=0, duration=5, timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(
            source_path=str(tmp_path / "earlier-master.mp4"),
            source_start=3, timeline_start=1,
            has_voiceover=True,
        ),
        subtitles=SubtitleTrack(cues=[
            # Ends before the retained soundtrack begins: drop it.
            SubtitleCue(start=0.25, end=1.75, text="已经说完"),
            # Crosses the beginning of this output: retain it, clamped to zero.
            SubtitleCue(start=1.5, end=2.5, text="跨越开头"),
            SubtitleCue(start=3, end=4, text="完整保留"),
            # Crosses the finished video's end: retain only its visible interval.
            SubtitleCue(start=6.25, end=8.5, text="跨越结尾"),
            # Begins after the picture has ended: drop it.
            SubtitleCue(start=7.1, end=8, text="画面外"),
        ]),
    )

    service.write_subtitle_script(first)
    first_payload = service.read_subtitle_sidecar(first_output)
    assert first_payload is not None
    assert first_payload["cues"] == [
        {"start": 0.0, "end": 0.5, "text": "跨越开头"},
        {"start": 1.0, "end": 2.0, "text": "完整保留"},
        {"start": 4.25, "end": 5.0, "text": "跨越结尾"},
    ]
    first_track = SubtitleTrack.model_validate({
        field: first_payload[field]
        for field in (
            "cues", "font", "size", "side_margin", "bottom_margin", "outline", "shadow",
            "primary_colour", "outline_colour", "max_lines",
        )
    })

    # Re-cut that output again from 0.25s into its mixed soundtrack.  The source begins after
    # the destination, so this exercises the same negative-offset boundary on generation two.
    second_output = tmp_path / "第二次微调.mp4"
    second = EditTimeline(
        title="second recut",
        output_path=str(second_output),
        clips=[TimelineClip(
            media_id="new-picture", source_path=str(tmp_path / "new-picture.mp4"),
            start=0, duration=3, timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(
            source_path=str(first_output),
            source_start=0.25, timeline_start=0,
            has_voiceover=True,
        ),
        subtitles=first_track,
    )

    service.write_subtitle_script(second)
    second_payload = service.read_subtitle_sidecar(second_output)
    assert second_payload is not None
    second_track = SubtitleTrack.model_validate({
        field: second_payload[field]
        for field in (
            "cues", "font", "size", "side_margin", "bottom_margin", "outline", "shadow",
            "primary_colour", "outline_colour", "max_lines",
        )
    })
    assert [cue.model_dump() for cue in second_track.cues] == [
        {"start": 0.0, "end": 0.25, "text": "跨越开头"},
        {"start": 0.75, "end": 1.75, "text": "完整保留"},
    ]


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


def test_a_sidecar_bound_to_another_video_is_never_loaded(tmp_path):
    service = RenderService()
    video = tmp_path / "当前母版.mp4"
    sidecar = service.subtitle_sidecar_path(video)
    sidecar.write_text(json.dumps({
        "video": "别的成片.mp4",
        "cues": [{"start": 0, "end": 1, "text": "不应出现"}],
    }, ensure_ascii=False), encoding="utf-8")

    assert service.read_subtitle_sidecar(video) is None
    assert service.read_subtitle_sidecar(sidecar, expected_video_path=video) is None


@pytest.mark.parametrize("invalid_binding", ["", 123, ["成片.mp4"]])
def test_a_present_malformed_video_binding_is_not_treated_as_legacy(
    tmp_path, invalid_binding
):
    service = RenderService()
    video = tmp_path / "成片.mp4"
    service.subtitle_sidecar_path(video).write_text(
        json.dumps({
            "video": invalid_binding,
            "cues": [{"start": 0, "end": 1, "text": "不能认领"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    assert service.read_subtitle_sidecar(video) is None


@pytest.mark.parametrize(
    "invalid_cues",
    [
        {"start": 0, "end": 1, "text": "not a list"},
        [{"start": 0, "end": 1}],
        [{"start": "now", "end": 1, "text": "bad time"}],
        [{"start": 1, "end": 1, "text": "zero duration"}],
        [{"start": 2, "end": 1, "text": "backwards"}],
        [{"start": 0, "end": 1, "text": "   "}],
    ],
)
def test_malformed_or_non_visible_cues_fail_closed(invalid_cues, tmp_path):
    service = RenderService()
    video = tmp_path / "strict-sidecar.mp4"
    service.subtitle_sidecar_path(video).write_text(
        json.dumps({"video": video.name, "cues": invalid_cues}),
        encoding="utf-8",
    )

    assert service.read_subtitle_sidecar(video) is None


def test_blank_cues_do_not_create_subtitle_artifacts_or_a_master(tmp_path):
    service = RenderService()
    timeline = EditTimeline(
        title="blank subtitle",
        output_path=str(tmp_path / "成片.mp4"),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text=" \t ")]),
    )

    assert service.output_subtitle_cues(timeline) == []
    assert service.write_subtitle_script(timeline) is None
    assert asyncio.run(service.render_master(timeline)) is None
    assert not (tmp_path / "成片.ass").exists()
    assert not (tmp_path / "成片.subtitles.json").exists()
    assert not (tmp_path / "成片 母版.mp4").exists()


def test_subtitle_data_write_failure_aborts_before_creating_ass(monkeypatch, tmp_path):
    timeline = EditTimeline(
        title="sidecar failure",
        output_path=str(tmp_path / "成片.mp4"),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    monkeypatch.setattr(
        "automated_video_editing_backend.services.render.write_json",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="字幕数据无法保存"):
        RenderService().write_subtitle_script(timeline)

    assert not (tmp_path / "成片.ass").exists()


def test_failed_delivery_render_removes_partial_video_and_subtitle_family(
    monkeypatch, tmp_path
):
    service = RenderService()
    output = tmp_path / "失败成片.mp4"
    timeline = EditTimeline(
        title="failed delivery",
        output_path=str(output),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )

    async def fail_after_opening_target(args):
        Path(args[-1]).write_bytes(b"partial mp4")
        raise RuntimeError("simulated decoder failure")

    monkeypatch.setattr(service, "supports_subtitles", lambda: True)
    monkeypatch.setattr(service, "_run", fail_after_opening_target)

    with pytest.raises(RuntimeError, match="decoder failure"):
        asyncio.run(service.render(timeline))

    assert not output.exists()
    assert not output.with_suffix(".ass").exists()
    assert not output.with_suffix(".subtitles.json").exists()


def test_delivery_render_never_overwrites_an_existing_family_member(
    monkeypatch, tmp_path
):
    service = RenderService()
    output = tmp_path / "已有成片.mp4"
    existing_sidecar = output.with_suffix(".subtitles.json")
    existing_sidecar.write_text('{"video": "older.mp4"}', encoding="utf-8")
    timeline = EditTimeline(
        title="collision",
        output_path=str(output),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
    )
    called = False

    async def should_not_run(_args):
        nonlocal called
        called = True

    monkeypatch.setattr(service, "_run", should_not_run)

    with pytest.raises(RuntimeError, match="目标文件已存在"):
        asyncio.run(service.render(timeline))

    assert called is False
    assert existing_sidecar.read_text(encoding="utf-8") == '{"video": "older.mp4"}'
    assert not output.exists()


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


def test_master_is_not_reported_when_its_canonical_subtitles_cannot_be_saved(
    monkeypatch, tmp_path,
):
    service = RenderService()
    timeline = EditTimeline(
        title="master sidecar failure",
        output_path=str(tmp_path / "成片.mp4"),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    service.write_subtitle_script(timeline)

    async def fake_run(args):
        Path(args[-1]).write_bytes(b"rendered master")

    monkeypatch.setattr(service, "_run", fake_run)
    monkeypatch.setattr(
        "automated_video_editing_backend.services.render.write_json",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="母版字幕数据无法保存"):
        asyncio.run(service.render_master(timeline))

    assert not (tmp_path / "成片 母版.mp4").exists()
    assert not (tmp_path / "成片 母版.subtitles.json").exists()


def test_cancelled_master_render_removes_partial_output(monkeypatch, tmp_path):
    service = RenderService()
    timeline = EditTimeline(
        title="cancelled master",
        output_path=str(tmp_path / "成片.mp4"),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )

    async def cancelled_run(args):
        Path(args[-1]).write_bytes(b"partial master")
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "_run", cancelled_run)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.render_master(timeline))

    assert not (tmp_path / "成片 母版.mp4").exists()
    assert not (tmp_path / "成片 母版.subtitles.json").exists()


def test_master_never_rebinds_another_videos_subtitle_layer(monkeypatch, tmp_path):
    service = RenderService()
    timeline = EditTimeline(
        title="mismatched master sidecar",
        output_path=str(tmp_path / "成片.mp4"),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    service.subtitle_sidecar_path(timeline.output_path).write_text(
        json.dumps({
            "video": "别的成片.mp4",
            "cues": [{"start": 0, "end": 0.8, "text": "不应继承"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    async def fake_run(args):
        Path(args[-1]).write_bytes(b"rendered master")

    monkeypatch.setattr(service, "_run", fake_run)

    with pytest.raises(RuntimeError, match="属于另一个成片"):
        asyncio.run(service.render_master(timeline))

    assert not (tmp_path / "成片 母版.mp4").exists()
    assert not (tmp_path / "成片 母版.subtitles.json").exists()


def test_master_rejects_a_present_malformed_video_binding(monkeypatch, tmp_path):
    service = RenderService()
    timeline = EditTimeline(
        title="malformed master sidecar",
        output_path=str(tmp_path / "成片.mp4"),
        clips=[TimelineClip(
            media_id="m", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    service.subtitle_sidecar_path(timeline.output_path).write_text(
        json.dumps({
            "video": 123,
            "cues": [{"start": 0, "end": 0.8, "text": "不应继承"}],
        }, ensure_ascii=False),
        encoding="utf-8",
    )

    async def fake_run(args):
        Path(args[-1]).write_bytes(b"rendered master")

    monkeypatch.setattr(service, "_run", fake_run)

    with pytest.raises(RuntimeError, match="video 绑定无效"):
        asyncio.run(service.render_master(timeline))

    assert not (tmp_path / "成片 母版.mp4").exists()
    assert not (tmp_path / "成片 母版.subtitles.json").exists()


def test_a_future_export_survives_restart_and_can_be_recut_from_its_master(
    tmp_path, source, ffmpeg,
):
    """Exercise the complete boundary the UI relies on, not only each file in isolation.

    A future render writes a delivery, a clean master and their cue layers; the backend then
    restarts and has to rediscover which file is safe to re-cut.  手动微调 takes the master's
    mixed soundtrack as one bed and draws the retained cues over the newly assembled picture.
    """
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path
    from automated_video_editing_backend.services.media import MediaService

    renderer = RenderService()
    stem = f"subtitle-restart-{uuid4().hex[:8]}"
    delivery = generated_path("exports", f"{stem}.mp4")
    master = generated_path("exports", f"{stem} 母版.mp4")
    voiceover = tmp_path / "reviewed-voice.wav"
    subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-f", "lavfi", "-i",
         "sine=frequency=440:sample_rate=48000:duration=2.9", str(voiceover)],
        check=True,
    )
    expected = SubtitleTrack(
        cues=[
            SubtitleCue(start=0.25, end=1.15, text="六和桥"),
            SubtitleCue(start=1.35, end=2.55, text="OPC 展台"),
        ],
        font="noto_sans_sc",
        size=0.052,
        side_margin=0.09,
        bottom_margin=0.11,
        outline=0.08,
        shadow=0.03,
        primary_colour="FFF4CC",
        outline_colour="101010",
        max_lines=2,
        timing_quality="estimated",
    )
    timeline = EditTimeline(
        title="restart source",
        output_path=str(delivery),
        output_width=640,
        output_height=360,
        clips=[TimelineClip(
            media_id="source", source_path=source, start=0, duration=2.9, timeline_start=0,
        )],
        voiceover_path=str(voiceover),
        subtitles=expected,
    )
    companions = [
        delivery,
        master,
        delivery.with_suffix(".ass"),
        delivery.with_suffix(".subtitles.json"),
        master.with_suffix(".subtitles.json"),
    ]

    try:
        assert asyncio.run(renderer.render(timeline)) == str(delivery)
        assert asyncio.run(renderer.render_master(timeline)) == str(master)

        library = tmp_path / "media-library.json"
        media = MediaService(path=library)
        group = f"export:{uuid4()}"
        media.register_generated_path(delivery, kind="video", metadata={
            "source": "exports",
            "role": "export",
            "export_group": group,
            "variant": "subtitled",
            "variant_label": "成片（带字幕）",
            "subtitles_path": str(delivery.with_suffix(".subtitles.json")),
            "has_burned_subtitles": True,
            "has_voiceover": True,
        })
        media.register_generated_path(master, kind="video", metadata={
            "source": "exports",
            "role": "export",
            "export_group": group,
            "variant": "master",
            "variant_label": "母版（无字幕）",
            "subtitles_path": str(master.with_suffix(".subtitles.json")),
            "has_burned_subtitles": False,
            "has_voiceover": True,
        })

        # Reconstructing MediaService is the backend-restart boundary. These are precisely the
        # metadata lookup and sidecar read performed by GET /subtitles/track.
        reopened = MediaService(path=library)
        restored = next(item for item in reopened.list_items() if item.path == str(master))
        assert restored.metadata["variant"] == "master"
        assert restored.metadata["has_burned_subtitles"] is False
        assert restored.metadata["has_voiceover"] is True
        payload = renderer.read_subtitle_sidecar(restored.metadata["subtitles_path"])
        assert payload is not None
        assert payload["cues"] == [cue.model_dump() for cue in expected.cues]
        for field in (
            "font", "size", "side_margin", "bottom_margin", "outline", "shadow",
            "primary_colour", "outline_colour", "max_lines", "timing_quality",
        ):
            assert payload[field] == getattr(expected, field)

        carried = SubtitleTrack.model_validate({
            field: payload[field]
            for field in (
                "cues", "font", "size", "side_margin", "bottom_margin", "outline", "shadow",
                "primary_colour", "outline_colour", "max_lines", "timing_quality",
            )
        })
        recut_delivery = tmp_path / "微调成片.mp4"
        recut = EditTimeline(
            title="微调成片",
            output_path=str(recut_delivery),
            output_width=640,
            output_height=360,
            clips=[TimelineClip(
                media_id=restored.id, source_path=restored.path,
                start=0, duration=2.8, timeline_start=0,
            )],
            audio_bed=TimelineAudioBed(
                source_path=restored.path, source_start=0, timeline_start=0,
                has_voiceover=True,
            ),
            subtitles=carried,
        )
        assert asyncio.run(renderer.render(recut)) == str(recut_delivery)
        recut_master = asyncio.run(renderer.render_master(recut))
        assert recut_master == str(tmp_path / "微调成片 母版.mp4")
        assert renderer.read_subtitle_sidecar(recut_delivery)["cues"] == payload["cues"]
        assert renderer.read_subtitle_sidecar(recut_master)["cues"] == payload["cues"]
        assert renderer.read_subtitle_sidecar(recut_master)["timing_quality"] == "estimated"

        def frame_at(path: str | Path, name: str):
            frame = tmp_path / f"{name}.png"
            subprocess.run(
                [ffmpeg, "-y", "-v", "error", "-ss", "0.7", "-i", str(path),
                 "-frames:v", "1", "-update", "1", str(frame)],
                check=True,
            )
            return cv2.imread(str(frame), cv2.IMREAD_GRAYSCALE)

        rendered = frame_at(recut_delivery, "recut-delivery")
        clean = frame_at(recut_master, "recut-master")
        assert rendered is not None and clean is not None
        assert np.count_nonzero(cv2.absdiff(rendered, clean) > 40) > 300
    finally:
        for path in companions:
            path.unlink(missing_ok=True)


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


def test_missing_word_clock_is_estimated_from_reviewed_text_and_audio_duration(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_text(json.dumps({
        "text": "欢迎来到六和桥。请看新品体验台！",
        "duration_ms": 4800,
        "words": [],
    }, ensure_ascii=False), encoding="utf-8")

    words, quality, problem = subtitles.load_words_or_estimate(sidecar)

    assert quality == "estimated"
    assert "逐字时间戳" in problem
    assert "".join(word["word"] for word in words) == "欢迎来到六和桥。请看新品体验台！"
    assert words[0]["start_time"] == 0
    assert words[-1]["end_time"] == 4800
    assert all(
        first["start_time"] <= first["end_time"] <= second["end_time"]
        and first["start_time"] <= second["start_time"]
        for first, second in itertools.pairwise(words)
    )
    cues = subtitles.cues_from_words(words, subtitles.SubtitleStyle(), 1280, 720)
    assert [cue.text for cue in cues] == ["欢迎来到六和桥", "请看新品体验台"]
    assert cues[-1].end == pytest.approx(4.8)


def test_probed_audio_duration_enables_estimation_when_sidecar_has_no_duration(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_text(json.dumps({
        "text": "这是已经确认的旁白",
        "words": [],
    }, ensure_ascii=False), encoding="utf-8")

    words, quality, problem = subtitles.load_words_or_estimate(
        sidecar,
        audio_duration_seconds=2.75,
    )

    assert quality == "estimated"
    assert problem is not None
    assert "".join(word["word"] for word in words) == "这是已经确认的旁白"
    assert words[-1]["end_time"] == 2750


def test_invalid_utf8_sidecar_fails_safely_without_becoming_subtitle_text(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_bytes(b"\xff\xfe\x00")

    words, quality, problem = subtitles.load_words_or_estimate(
        sidecar,
        audio_duration_seconds=2.0,
    )

    assert words == []
    assert quality is None
    assert "无法读取" in problem


def test_boolean_duration_cannot_create_an_estimated_subtitle_clock(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_text(json.dumps({
        "text": "已经确认的旁白",
        "duration_ms": True,
        "words": [],
    }, ensure_ascii=False), encoding="utf-8")

    words, quality, problem = subtitles.load_words_or_estimate(
        sidecar,
        audio_duration_seconds=True,
    )

    assert words == []
    assert quality is None
    assert "无法取得旁白时长" in problem


def test_persisted_estimate_is_rebuilt_over_the_actual_audio_duration(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_text(json.dumps({
        "text": "完整旁白不能提前消失",
        "duration_ms": 1200,
        "timing_quality": "estimated",
        "words": subtitles.estimate_source_timing("完整旁白不能提前消失", 1200),
    }, ensure_ascii=False), encoding="utf-8")

    words, quality, problem = subtitles.load_words_or_estimate(
        sidecar,
        audio_duration_seconds=3.6,
    )

    assert quality == "estimated"
    assert problem is None
    assert "".join(word["word"] for word in words) == "完整旁白不能提前消失"
    assert words[0]["start_time"] == 0
    assert words[-1]["end_time"] == 3600


def test_estimation_never_uses_provider_labels_and_requires_reviewed_text(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_text(json.dumps({
        "duration_ms": 900,
        "words": [{"word": "六合桥", "end_time": 700}],
    }, ensure_ascii=False), encoding="utf-8")

    words, quality, problem = subtitles.load_words_or_estimate(sidecar)

    assert words == []
    assert quality is None
    assert "已确认的旁白文字" in problem


def test_a_word_without_timing_is_dropped_rather_than_defaulted():
    """A record defaulted to 0.0 does not look broken — it looks like a word said at the start,
    and it drags the cue that contains it back to the beginning of the video."""
    records = [
        {"word": "好", "start_time": 1000, "end_time": 1200},
        {"word": "坏", "end_time": 1400},
        {"word": "空", "start_time": "later", "end_time": 1600},
    ]
    assert subtitles.normalise_words(records) == [("好", 1.0, 1.2)]


def test_sidecar_restores_reviewed_characters_without_changing_provider_timing(tmp_path):
    sidecar = tmp_path / "voice.json"
    sidecar.write_text(json.dumps({
        "text": "六和桥OPC",
        "words": [
            {"word": "六合桥", "start_time": 100, "end_time": 700},
            {"word": "opc", "start_time": 720, "end_time": 1300},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    words, problem = subtitles.load_words(sidecar)

    assert problem is None
    assert [word["word"] for word in words] == ["六和桥", "OPC"]
    assert words[0]["start_time"] == 100
    assert words[0]["end_time"] == 700
    assert words[1]["start_time"] == 720
    assert words[1]["end_time"] == 1300


def test_reordered_equal_length_labels_use_safe_proportional_timing():
    source = "今天经过六和桥"
    words, quality = subtitles.restore_source_spelling_with_quality([
        {"word": "今经过天", "start_time": 100, "end_time": 700},
        {"word": "六合桥", "start_time": 720, "end_time": 1300},
    ], source)

    # Equal character count is not sufficient evidence: the moved 天 produces both an insert
    # and a delete in sequence alignment, so the source is spread safely over the spoken range.
    assert "".join(word["word"] for word in words) == source
    assert [word["word"] for word in words] == list(source)
    assert words[0]["start_time"] == 100
    assert words[-1]["end_time"] == 1300
    assert quality == "estimated"
    assert all(
        first["start_time"] <= first["end_time"] <= second["end_time"]
        for first, second in itertools.pairwise(words)
    )


@pytest.mark.parametrize(
    "raw_words",
    [
        [
            {"word": "甲", "start_time": True, "end_time": 300},
            {"word": "乙", "start_time": 500, "end_time": 900},
        ],
        [
            {"word": "甲", "start_time": -100, "end_time": 300},
            {"word": "乙", "start_time": 500, "end_time": 900},
        ],
        [
            {"word": "甲", "start_time": 100, "end_time": 100},
            {"word": "乙", "start_time": 500, "end_time": 900},
        ],
        [
            {"word": "甲", "start_time": 0, "end_time": 600},
            {"word": "乙", "start_time": 500, "end_time": 900},
        ],
    ],
    ids=["boolean", "negative", "zero_length", "overlap"],
)
def test_damaged_provider_clocks_are_never_labeled_exact(raw_words):
    words, quality = subtitles.restore_source_spelling_with_quality(
        raw_words,
        "甲乙",
        duration_ms=1000,
    )

    assert quality != "exact"
    if words:
        assert "".join(word["word"] for word in words) == "甲乙"


def test_missing_provider_character_is_aligned_to_neighbouring_token():
    source = "欢迎来到六和桥"
    words = subtitles.restore_source_spelling([
        {"word": "欢迎来到", "start_time": 0, "end_time": 500},
        {"word": "六合", "start_time": 520, "end_time": 900},
    ], source)

    assert [word["word"] for word in words] == ["欢迎来到", "六和桥"]
    assert words[1]["start_time"] == 520
    assert words[1]["end_time"] == 900


def test_unrelated_equal_length_labels_never_replace_reviewed_source_by_position():
    words = subtitles.restore_source_spelling([
        {"word": "完全错", "start_time": 0, "end_time": 900},
    ], "六和桥")

    assert [word["word"] for word in words] == ["六", "和", "桥"]
    assert words[0]["start_time"] == 0
    assert words[-1]["end_time"] == 900


@pytest.mark.parametrize(
    "bad_token",
    [
        "not an object",
        {"word": "欢迎", "end_time": 1100},
        {"word": "", "start_time": 720, "end_time": 1100},
        {"word": "欢迎", "start_time": "later", "end_time": 1100},
        {"word": "欢迎", "start_time": float("nan"), "end_time": 1100},
        {"word": "欢迎", "start_time": 1200, "end_time": 1100},
    ],
)
def test_one_bad_provider_token_never_restores_provider_spelling(bad_token):
    source = "六和桥欢迎您"
    words = subtitles.restore_source_spelling([
        {"word": "六合桥", "start_time": 100, "end_time": 700},
        bad_token,
        {"word": "您", "start_time": 1300, "end_time": 1500},
    ], source)

    assert "".join(word["word"] for word in words) == source
    assert "六合桥" not in "".join(word["word"] for word in words)
    assert words[0]["start_time"] == 100
    assert words[-1]["end_time"] == 1500
    assert all(
        first["start_time"] <= first["end_time"] <= second["end_time"]
        and first["start_time"] <= second["start_time"]
        for first, second in itertools.pairwise(words)
    )


def test_rebuilt_reviewed_text_preserves_meaningful_spaces():
    source = "OPC 六和桥 欢迎您"
    words = subtitles.restore_source_spelling([
        {"word": "opc", "start_time": 0, "end_time": 450},
        {"word": "损坏", "end_time": 900},
        {"word": "您", "start_time": 1100, "end_time": 1500},
    ], source, duration_ms=1500)

    assert "".join(word["word"] for word in words) == source
    spoken = subtitles.normalise_words(words)
    assert "".join(word for word, _start, _end in spoken) == source
    cues = subtitles.build_cues(spoken, capacity=100)
    assert [cue.text for cue in cues] == [source]


def test_truncated_provider_fragment_cannot_compress_a_full_reviewed_sentence(tmp_path):
    sidecar = tmp_path / "truncated-voice.json"
    sidecar.write_text(json.dumps({
        "text": "六和桥欢迎您参观今天的展览",
        "duration_ms": 5000,
        "words": [
            {"word": "六合桥", "start_time": 0, "end_time": 120},
            {"word": "损坏", "end_time": 4000},
        ],
    }, ensure_ascii=False), encoding="utf-8")

    words, problem = subtitles.load_words(sidecar)

    assert words == []
    assert "时间戳损坏" in problem


def test_out_of_order_provider_times_rebuild_reviewed_text_in_spoken_order():
    words = subtitles.restore_source_spelling([
        {"word": "六", "start_time": 700, "end_time": 900},
        {"word": "合", "start_time": 100, "end_time": 300},
        {"word": "桥", "start_time": 1000, "end_time": 1200},
    ], "六和桥")

    assert "".join(word["word"] for word in words) == "六和桥"
    assert [word["start_time"] for word in words] == sorted(
        word["start_time"] for word in words
    )
    assert words[0]["start_time"] == 100
    assert words[-1]["end_time"] == 1200


def test_all_invalid_provider_tokens_fail_closed_without_provider_text(tmp_path):
    sidecar = tmp_path / "unsafe-voice.json"
    sidecar.write_text(json.dumps({
        "text": "六和桥",
        "words": [{"word": "六合桥", "end_time": 700}],
    }, ensure_ascii=False), encoding="utf-8")

    words, problem = subtitles.load_words(sidecar)

    assert words == []
    assert "时间戳损坏" in problem


def test_provider_labels_fail_closed_when_reviewed_source_text_is_missing(tmp_path):
    sidecar = tmp_path / "unsafe-provider-only-voice.json"
    sidecar.write_text(json.dumps({
        "words": [{"word": "六合桥", "start_time": 100, "end_time": 700}],
    }, ensure_ascii=False), encoding="utf-8")

    words, problem = subtitles.load_words(sidecar)

    assert words == []
    assert "时间戳损坏" in problem


def test_final_short_cue_does_not_outlive_the_last_spoken_word():
    cues = subtitles.cues_from_words(
        [{"word": "好", "start_time": 100, "end_time": 280}],
        subtitles.SubtitleStyle(),
        1280,
        720,
    )

    assert len(cues) == 1
    assert cues[0].start == pytest.approx(0.1)
    assert cues[0].end == pytest.approx(0.28)


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

    from automated_video_editing_backend.core.models import (
        AnalysisResult,
        EditJobRequest,
        MediaItem,
    )
    from automated_video_editing_backend.services.timeline import EditPlanner

    audio = tmp_path / "旁白-001.mp3"
    audio.write_bytes(b"not really audio")
    sidecar = audio.with_suffix(".json")
    # The shape tts.py writes.
    sidecar.write_text(
        json.dumps({
            "text": NARRATION,
            "duration_ms": 8000,
            "timing_quality": "exact",
            "words": _words(),
        },
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
    assert track.timing_quality == "exact"
    assert track.font == "smiley_sans"
    assert track.size == subtitles.SIZE_PRESETS["large"]
    assert "机器人" in track.cues[0].text

    # And with the sidecar pointer absent, it is found by the paired stem instead.
    unlinked = MediaItem(id="v2", name=audio.name, path=str(audio), kind="audio",
                         metadata={"role": "tts_voice"})
    assert EditPlanner()._subtitles(request, unlinked, 1280, 720, []) is not None

    video = MediaItem(id="video", path=str(tmp_path / "video.mp4"), kind="video")
    analysis = AnalysisResult(media_id=video.id, scenes=[{"start": 0, "end": 10}])
    exact_timeline = EditPlanner().plan(
        request.model_copy(update={"media_ids": [video.id]}),
        [video],
        [analysis],
        None,
        voiceover,
        voiceover_duration=8.0,
    )
    assert exact_timeline.planning_diagnostics["subtitles"] == {
        "requested": True,
        "mode": "exact",
        "label": "精确字幕",
    }


def test_planner_marks_estimated_subtitles_and_keeps_narration_when_estimation_is_impossible(
    tmp_path, fonts_present,
):
    from automated_video_editing_backend.core.models import (
        AnalysisResult,
        EditJobRequest,
        MediaItem,
    )
    from automated_video_editing_backend.services.timeline import EditPlanner

    audio = tmp_path / "旁白.mp3"
    audio.write_bytes(b"not really audio")
    sidecar = audio.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "text": "欢迎来到六和桥。这里是新品体验台。",
        "words": [],
    }, ensure_ascii=False), encoding="utf-8")
    voiceover = MediaItem(
        id="estimated",
        path=str(audio),
        kind="audio",
        metadata={"role": "tts_voice", "metadata_path": str(sidecar)},
    )
    request = EditJobRequest(title="t", output_name="t.mp4", subtitles=True)
    warnings: list[str] = []

    track = EditPlanner()._subtitles(
        request, voiceover, 1280, 720, warnings, voiceover_duration=4.2,
    )

    assert track is not None
    assert track.timing_quality == "estimated"
    assert "".join(cue.text for cue in track.cues) == "欢迎来到六和桥这里是新品体验台"
    assert track.cues[-1].end == pytest.approx(4.2)
    assert any("估算字幕" in warning for warning in warnings)

    video = MediaItem(id="video", path=str(tmp_path / "video.mp4"), kind="video")
    analysis = AnalysisResult(media_id=video.id, scenes=[{"start": 0, "end": 6}])
    estimated_timeline = EditPlanner().plan(
        request,
        [video],
        [analysis],
        None,
        voiceover,
        voiceover_duration=4.2,
    )
    assert estimated_timeline.planning_diagnostics["subtitles"] == {
        "requested": True,
        "mode": "estimated",
        "label": "估算字幕",
    }

    sidecar.write_text(json.dumps({"words": []}), encoding="utf-8")
    narration_only_warnings: list[str] = []
    unavailable = EditPlanner()._subtitles(
        request,
        voiceover,
        1280,
        720,
        narration_only_warnings,
        voiceover_duration=4.2,
    )
    assert unavailable is None
    assert any("成片仍保留旁白" in warning for warning in narration_only_warnings)

    narration_only_timeline = EditPlanner().plan(
        request,
        [video],
        [analysis],
        None,
        voiceover,
        voiceover_duration=4.2,
    )
    assert narration_only_timeline.subtitles is None
    assert narration_only_timeline.planning_diagnostics["subtitles"] == {
        "requested": True,
        "mode": "narration_only",
        "label": "仅旁白",
    }


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
