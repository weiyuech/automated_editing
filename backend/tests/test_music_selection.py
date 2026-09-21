import asyncio
import json
import sys
from pathlib import Path

import numpy as np
import pytest

from automated_video_editing_backend.core.models import EditTimeline, TimelineClip
from automated_video_editing_backend.services import music_selection as music
from automated_video_editing_backend.services.render import RenderService


def features():
    n = int(60 * music.SAMPLE_RATE / music.HOP)
    rms = np.ones(n) * 0.1
    rms[: int(20 * music.SAMPLE_RATE / music.HOP)] = 0
    return {"duration": 60.0, "rms": rms, "onset": np.ones(n), "beats": np.arange(20, 60, 0.5)}


def test_whole_music_search_avoids_silence_and_lines_up_cuts():
    result = music.choose_offset(features(), 20, [4, 8, 12, 16])
    assert 20 <= result["start"] <= 40
    assert abs(result["start"] % 0.5) < 0.01
    assert not result["loop"]
    assert music.choose_offset(features(), 20, [4, 8, 12, 16]) == result


def test_short_track_loops_and_equal_duration_does_not():
    assert music.choose_offset(features(), 80, [])["loop"]
    assert not music.choose_offset(features(), 60, [])["loop"]


def test_saved_composition_shot_cuts_survive_flat_input_and_trim(tmp_path):
    source = tmp_path / "combined.mp4"
    Path(str(source) + ".composition.json").write_text(
        json.dumps(
            {
                "composition_tree": [
                    {"start": 0, "children": [{"start": 0}, {"start": 6}, {"start": 10}]},
                    {"start": 16},
                ]
            }
        )
    )
    timeline = EditTimeline(
        title="test",
        output_path=str(tmp_path / "out.mp4"),
        music_delay_seconds=2,
        clips=[
            TimelineClip(
                media_id="a", source_path=str(source), start=4, duration=12, timeline_start=2
            )
        ],
    )
    assert music.picture_cuts(timeline) == [0, 2, 6]


@pytest.mark.asyncio
async def test_analysis_updates_only_music_and_reuses_selection(monkeypatch, tmp_path):
    source = tmp_path / "music.mp3"
    source.write_bytes(b"audio")
    timeline = EditTimeline(
        title="test",
        output_path=str(tmp_path / "out.mp4"),
        music_path=str(source),
        clips=[
            TimelineClip(
                media_id="a", source_path="video.mp4", start=0, duration=20, timeline_start=0
            )
        ],
    )
    original = timeline.model_dump()
    calls = []

    async def analyze(*_):
        calls.append(1)
        return features()

    monkeypatch.setattr(music, "features_for", analyze)
    renderer = RenderService()
    monkeypatch.setattr(renderer, "probe_duration", lambda _: 60)
    await music.prepare_music(timeline, renderer)
    assert timeline.music_start_seconds >= 20
    assert timeline.music_duration_seconds == 20
    assert timeline.clips == EditTimeline.model_validate(original).clips
    assert timeline.subtitles == EditTimeline.model_validate(original).subtitles
    await music.prepare_music(timeline, renderer)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_analysis_timeout_falls_back_but_cancel_propagates(monkeypatch, tmp_path):
    source = tmp_path / "music.mp3"
    source.write_bytes(b"audio")
    timeline = EditTimeline(
        title="test",
        output_path=str(tmp_path / "out.mp4"),
        music_path=str(source),
        clips=[
            TimelineClip(
                media_id="a", source_path="video.mp4", start=0, duration=20, timeline_start=0
            )
        ],
    )
    renderer = RenderService()
    monkeypatch.setattr(renderer, "probe_duration", lambda _: 60)

    async def fail(*_):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(music, "features_for", fail)
    await music.prepare_music(timeline, renderer)
    assert timeline.music_start_seconds == 0 and timeline.warnings
    timeline.planning_diagnostics.clear()

    async def cancel(*_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(music, "features_for", cancel)
    with pytest.raises(asyncio.CancelledError):
        await music.prepare_music(timeline, renderer)


@pytest.mark.asyncio
async def test_worker_timeout_kills_child(tmp_path):
    marker = tmp_path / "should-not-exist"
    script = f"import time; from pathlib import Path; time.sleep(1); Path({str(marker)!r}).touch()"
    with pytest.raises(asyncio.TimeoutError):
        await music._run([sys.executable, "-c", script], 0.05)
    await asyncio.sleep(1.1)
    assert not marker.exists()
