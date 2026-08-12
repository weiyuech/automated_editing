from automated_video_editing_backend.core.models import (
    EditTimeline,
    SubtitleCue,
    SubtitleTrack,
    TimelineClip,
)
from automated_video_editing_backend.services.render import RenderService


def test_ffmpeg_maps_music_audio_and_not_clip_audio():
    timeline = EditTimeline(
        title="Beat render",
        clips=[
            TimelineClip(
                media_id="clip-1",
                source_path="/tmp/source-with-noise.mp4",
                start=0,
                duration=3,
                timeline_start=0,
            )
        ],
        music_path="/tmp/music.wav",
        output_path="/tmp/export.mp4",
        mute_original_audio=True,
        beat_sync=True,
    )

    args = RenderService().build_ffmpeg_args(timeline)

    filter_graph = args[args.index("-filter_complex") + 1]

    assert "-filter_complex" in args
    assert "concat=n=1:v=1:a=0[vout]" in filter_graph
    assert args[args.index("-map") + 1] == "[vout]"
    # Music with no voice under it plays at full volume, not at the bed level.
    assert "[1:a:0]volume=1.0[bgm]" in filter_graph
    assert "-shortest" in args
    assert "0:a:0" not in args
    assert "[0:a]" not in filter_graph


def test_ffmpeg_disables_audio_without_music():
    timeline = EditTimeline(
        title="Silent render",
        clips=[TimelineClip(media_id="clip-1", source_path="/tmp/source.mp4", start=0, duration=3, timeline_start=0)],
        output_path="/tmp/export.mp4",
    )

    args = RenderService().build_ffmpeg_args(timeline)

    assert "-an" in args


def test_portrait_frame_is_cropped_before_subtitles_are_drawn():
    timeline = EditTimeline(
        title="Portrait render",
        clips=[TimelineClip(
            media_id="clip-1", source_path="/tmp/source.mp4", start=0, duration=3,
            timeline_start=0,
        )],
        output_path="/tmp/export.mp4",
        output_width=720,
        output_height=1280,
        output_crop_x=0.25,
        output_crop_y=0.75,
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=2, text="安全字幕")]),
    )

    args = RenderService().build_ffmpeg_args(timeline, "/tmp/export.ass")
    graph = args[args.index("-filter_complex") + 1]

    assert "scale=720:1280:force_original_aspect_ratio=increase" in graph
    assert "crop=720:1280:(iw-ow)*0.250000:(ih-oh)*0.750000" in graph
    assert "pad=" not in graph
    assert graph.index("crop=720:1280") < graph.index("ass=filename=")


def test_original_frame_fits_without_cropping():
    timeline = EditTimeline(
        title="Original frame",
        clips=[TimelineClip(
            media_id="clip-1", source_path="/tmp/source.mp4", start=0, duration=3,
            timeline_start=0,
        )],
        output_path="/tmp/export.mp4",
        output_width=1080,
        output_height=1920,
        output_fit="contain",
    )

    args = RenderService().build_ffmpeg_args(timeline)
    graph = args[args.index("-filter_complex") + 1]

    assert "scale=1080:1920:force_original_aspect_ratio=decrease" in graph
    assert "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black" in graph
    assert "crop=1080:1920" not in graph


def test_ffmpeg_mixes_music_and_voiceover():
    timeline = EditTimeline(
        title="Voice render",
        clips=[TimelineClip(media_id="clip-1", source_path="/tmp/source.mp4", start=0, duration=3, timeline_start=0)],
        music_path="/tmp/music.wav",
        voiceover_path="/tmp/voice.mp3",
        output_path="/tmp/export.mp4",
    )

    args = RenderService().build_ffmpeg_args(timeline)
    filter_graph = args[args.index("-filter_complex") + 1]

    assert "[1:a:0]volume=0.25[bgm]" in filter_graph
    assert "[2:a:0]volume=1.0[vox]" in filter_graph
    assert "[bgm][vox]amix=inputs=2" in filter_graph
    # amix halves every input unless told otherwise, which put the narration at 0.5 and the
    # bed at 0.14 — a mixed export was audibly quieter than a narration-only one.
    assert "normalize=0" in filter_graph
    assert "[aout]" in args


def test_audio_length_never_shortens_the_video():
    """A 5-second narration used to end a 30-second export at 5 seconds.

    Padding the mix with silence makes -shortest land on the video instead.
    """
    timeline = EditTimeline(
        title="Short voice",
        clips=[TimelineClip(media_id="clip-1", source_path="/tmp/source.mp4", start=0, duration=30, timeline_start=0)],
        voiceover_path="/tmp/voice.mp3",
        output_path="/tmp/export.mp4",
    )

    filter_graph = RenderService().build_ffmpeg_args(timeline)[
        RenderService().build_ffmpeg_args(timeline).index("-filter_complex") + 1
    ]

    assert "apad" in filter_graph


def test_original_audio_is_only_wired_when_the_timeline_says_so():
    clips = [TimelineClip(media_id="clip-1", source_path="/tmp/source.mp4", start=1, duration=3, timeline_start=0)]
    muted = EditTimeline(title="Muted", clips=clips, music_path="/tmp/music.wav", output_path="/tmp/a.mp4")
    kept = EditTimeline(
        title="Kept", clips=clips, music_path="/tmp/music.wav", output_path="/tmp/b.mp4",
        mute_original_audio=False, include_original_audio=True,
    )

    muted_graph = RenderService().build_ffmpeg_args(muted)[
        RenderService().build_ffmpeg_args(muted).index("-filter_complex") + 1
    ]
    kept_graph = RenderService().build_ffmpeg_args(kept)[
        RenderService().build_ffmpeg_args(kept).index("-filter_complex") + 1
    ]

    assert "[0:a]" not in muted_graph
    assert "[0:a]atrim=start=1.000:duration=3.000" in kept_graph
    assert "concat=n=1:v=0:a=1[origraw]" in kept_graph


def test_ffmpeg_binary_resolves_common_macos_path(monkeypatch):
    monkeypatch.delenv("FFMPEG_BIN", raising=False)
    binary = RenderService().ffmpeg_binary()
    assert binary.endswith("ffmpeg")


def test_still_clip_is_looped_to_its_duration():
    """A still has no length to trim to. Without looping, a 3-second image renders as one
    frame — a flash rather than a held picture."""
    timeline = EditTimeline(
        title="Still",
        clips=[TimelineClip(media_id="i1", source_path="/tmp/a.png", start=0, duration=3,
                            timeline_start=0, kind="image")],
        output_path="/tmp/export.mp4",
    )

    args = RenderService().build_ffmpeg_args(timeline)
    at = args.index("-i")

    assert args[at - 6:at] == ["-loop", "1", "-framerate", "30", "-t", "3.000"]


def test_video_clip_is_not_looped():
    timeline = EditTimeline(
        title="Video",
        clips=[TimelineClip(media_id="v1", source_path="/tmp/a.mp4", start=0, duration=3, timeline_start=0)],
        output_path="/tmp/export.mp4",
    )

    args = RenderService().build_ffmpeg_args(timeline)

    assert "-loop" not in args


def test_audio_bed_plays_unbroken_and_shifts_when_asked():
    """手动微调 lays one soundtrack under the cuts rather than taking audio per clip, which
    would chop the sound at every edit."""
    from automated_video_editing_backend.core.models import TimelineAudioBed

    clips = [
        TimelineClip(media_id="v1", source_path="/tmp/a.mp4", start=0, duration=4, timeline_start=0),
        TimelineClip(media_id="i1", source_path="/tmp/b.png", start=0, duration=3, timeline_start=4, kind="image"),
    ]
    timeline = EditTimeline(
        title="Bed", clips=clips, output_path="/tmp/export.mp4",
        audio_bed=TimelineAudioBed(source_path="/tmp/a.mp4", source_start=1.5, timeline_start=3),
    )

    args = RenderService().build_ffmpeg_args(timeline)
    graph = args[args.index("-filter_complex") + 1]

    # The bed is its own input, after the two clips.
    assert args.count("-i") == 3
    assert "[2:a:0]atrim=start=1.500" in graph
    assert "adelay=3000:all=1" in graph
    assert "[bed]" in graph
    # Per-clip audio must not appear — that is what would cut the sound at each edit.
    assert "[0:a]" not in graph
    assert "[1:a]" not in graph


def test_audio_bed_is_not_delayed_when_nothing_precedes_the_picture():
    from automated_video_editing_backend.core.models import TimelineAudioBed

    timeline = EditTimeline(
        title="Bed", output_path="/tmp/export.mp4",
        clips=[TimelineClip(media_id="v1", source_path="/tmp/a.mp4", start=0, duration=4, timeline_start=0)],
        audio_bed=TimelineAudioBed(source_path="/tmp/a.mp4"),
    )

    graph = RenderService().build_ffmpeg_args(timeline)[
        RenderService().build_ffmpeg_args(timeline).index("-filter_complex") + 1
    ]

    assert "adelay" not in graph


def test_effect_audio_is_ducked_and_aligned_without_changing_the_voice_bed():
    """Effect sound is additive; narration remains one untouched continuous input."""
    from automated_video_editing_backend.core.models import TimelineAudioBed

    timeline = EditTimeline(
        title="Effect sound under voice",
        output_path="/tmp/export.mp4",
        clips=[
            TimelineClip(
                media_id="base", source_path="/tmp/base.mp4", start=0, duration=3,
                timeline_start=0,
            ),
            TimelineClip(
                media_id="effect", source_path="/tmp/effect.mp4", start=0.5, duration=2,
                timeline_start=3, include_audio=True, audio_volume=0.3,
            ),
        ],
        audio_bed=TimelineAudioBed(
            source_path="/tmp/master.mp4", source_start=1.25,
            timeline_start=0, has_voiceover=True,
        ),
    )

    args = RenderService().build_ffmpeg_args(timeline)
    graph = args[args.index("-filter_complex") + 1]

    assert "[2:a:0]atrim=start=1.250,asetpts=PTS-STARTPTS[bed]" in graph
    assert "[1:a:0]atrim=start=0.500:duration=2.000" in graph
    assert "volume=0.300,adelay=3000:all=1[fxa1]" in graph
    assert "[bed][fxa1]amix=inputs=2:duration=longest" in graph
    assert "normalize=0" in graph


def test_effect_audio_plays_full_volume_without_a_voice_bed():
    timeline = EditTimeline(
        title="Effect only",
        output_path="/tmp/export.mp4",
        clips=[TimelineClip(
            media_id="effect", source_path="/tmp/effect.mp4", start=0, duration=4,
            timeline_start=0, include_audio=True, audio_volume=1.0,
        )],
    )

    args = RenderService().build_ffmpeg_args(timeline)
    graph = args[args.index("-filter_complex") + 1]

    assert "volume=1.000[fxa0]" in graph
    assert "[fxa0]anull[mixed]" in graph
