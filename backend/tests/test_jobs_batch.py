import json
import tempfile
from pathlib import Path

import pytest

from automated_video_editing_backend.core.models import (
    EditJobRequest,
    EditTimeline,
    JobRecord,
    JobStatus,
    MediaItem,
    SubtitleCue,
    SubtitleTrack,
    TimelineAudioBed,
    TimelineClip,
)
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import (
    GeneratedMetadataPersistenceError,
    MediaService,
)
from automated_video_editing_backend.services.render import RenderService


class DummyEvents:
    async def publish(self, *_args, **_kwargs):
        return None


def test_voiceover_duration_prefers_actual_probe_without_mutating_sidecars(tmp_path):
    class DurationRenderer:
        def __init__(self):
            self.calls = 0

        def probe_duration(self, _path):
            self.calls += 1
            return 4.25

    renderer = DurationRenderer()
    service = JobService(DummyEvents(), None, None, None, renderer)
    estimated = MediaItem(
        path=str(tmp_path / "estimated.mp3"),
        kind="audio",
        metadata={"duration_ms": 1200, "timing_quality": "estimated"},
    )
    exact = MediaItem(
        path=str(tmp_path / "exact.mp3"),
        kind="audio",
        metadata={"duration_ms": 1200, "timing_quality": "exact"},
    )

    assert service._audio_duration(estimated) == 4.25
    assert service._audio_duration(exact) == 4.25
    assert renderer.calls == 2

    without_probe = JobService(DummyEvents(), None, None, None, None)
    assert without_probe._audio_duration(estimated) == 1.2

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json", encoding="utf-8")
    damaged = MediaItem(
        path=str(tmp_path / "damaged.mp3"),
        kind="audio",
        metadata={"metadata_path": str(malformed), "duration_ms": 1200},
    )
    assert service._audio_duration(damaged) == 4.25
    assert malformed.read_text(encoding="utf-8") == "{not json"
    assert not list(tmp_path.glob("malformed.json.corrupt-*"))

    cached_audio = tmp_path / "cached.mp3"
    cached_audio.write_bytes(b"stable-audio")
    cached = MediaItem(path=str(cached_audio), kind="audio")
    calls_before_cache = renderer.calls
    assert service._audio_duration(cached) == 4.25
    assert service._audio_duration(cached) == 4.25
    assert renderer.calls == calls_before_cache + 1


def test_voiceover_duration_rejects_boolean_and_non_finite_metadata(tmp_path):
    audio = tmp_path / "voice.mp3"
    audio.write_bytes(b"audio")
    sidecar = audio.with_suffix(".json")
    sidecar.write_text(json.dumps({"duration_ms": 2400}), encoding="utf-8")
    service = JobService(DummyEvents(), None, None, None, None)

    for invalid in (True, False, float("nan"), float("inf"), -1, 0):
        item = MediaItem(
            path=str(audio),
            kind="audio",
            metadata={"metadata_path": str(sidecar), "duration_ms": invalid},
        )
        assert service._audio_duration(item) == 2.4


class CompatibilitySemantic:
    def compatibility(self, source, voice):
        return 0.9 if source.metadata.get("topic") == voice.metadata.get("topic") else 0.2


@pytest.mark.asyncio
async def test_manual_timeline_rejects_downloads_output_before_render_or_restart(tmp_path):
    from automated_video_editing_backend.core.paths import generated_path

    media_path = tmp_path / "media-library.json"
    media = MediaService(path=media_path)
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    escaped = generated_path("data", "downloads", "escaped-manual-output.mp4")
    escaped.unlink(missing_ok=True)
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unsafe manual output",
        output_path=str(escaped),
        clips=[TimelineClip(
            media_id=video.id,
            source_path=video.path,
            start=0,
            duration=1,
            timeline_start=0,
        )],
    )

    with pytest.raises(ValueError, match="managed exports"):
        await service.create_from_timeline(timeline)

    assert not escaped.exists()
    reopened = MediaService(path=media_path)
    assert all(item.path != str(escaped) for item in reopened.list_items())


@pytest.mark.asyncio
async def test_manual_timeline_rejects_a_clip_path_unknown_to_the_media_library(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    unknown = _write(tmp_path / "unknown.mp4")
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unknown clip",
        output_path="",
        clips=[TimelineClip(
            media_id="forged-or-stale-id",
            source_path=str(unknown),
            start=0,
            duration=1,
            timeline_start=0,
        )],
    )

    with pytest.raises(ValueError, match="不在媒体库"):
        await service.create_from_timeline(timeline)

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_manual_timeline_rejects_an_unknown_retained_audio_bed(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    clip = media.import_path(str(_write(tmp_path / "known.mp4")))
    unknown_bed = _write(tmp_path / "unknown-bed.mp4")
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unknown bed",
        output_path="",
        clips=[TimelineClip(
            media_id=clip.id,
            source_path=clip.path,
            start=0,
            duration=1,
            timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(source_path=str(unknown_bed)),
    )

    with pytest.raises(ValueError, match="原声不在媒体库"):
        await service.create_from_timeline(timeline)

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_manual_timeline_rejects_client_subtitles_without_a_bed_sidecar(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    clip = media.import_path(str(_write(tmp_path / "known-no-sidecar.mp4")))
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unverified client track",
        output_path="",
        clips=[TimelineClip(
            media_id=clip.id,
            source_path=clip.path,
            start=0,
            duration=1,
            timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(source_path=clip.path),
        subtitles=SubtitleTrack(cues=[
            SubtitleCue(start=0, end=0.8, text="客户端提交的未知字幕"),
        ]),
    )

    with pytest.raises(ValueError, match="没有可验证的字幕数据"):
        await service.create_from_timeline(timeline)

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_failed_delivery_registration_removes_the_new_flat_file_family(
    tmp_path, monkeypatch
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"failed-delivery-{uuid4().hex[:8]}.mp4")
    renderer = RenderService()
    media = MediaService(path=tmp_path / "media-library.json")
    source = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="failure",
        output_path=str(output),
        clips=[TimelineClip(
            media_id=source.id, source_path=source.path,
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=None,
    )
    job = JobRecord(
        request=EditJobRequest(title="failure", media_ids=[source.id]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_render(_timeline):
        output.write_bytes(b"delivery")
        output.with_suffix(".ass").write_text("subtitle", encoding="utf-8")
        output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(output)

    monkeypatch.setattr(renderer, "render", fake_render)
    monkeypatch.setattr(
        media,
        "register_generated_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GeneratedMetadataPersistenceError("simulated registration failure")
        ),
    )

    await service._run_job(job.id)

    assert job.status == JobStatus.FAILED
    assert "simulated registration failure" in job.error
    assert not output.exists()
    assert not output.with_suffix(".ass").exists()
    assert not output.with_suffix(".subtitles.json").exists()


@pytest.mark.asyncio
async def test_failed_group_registration_removes_delivery_and_master_without_publishing(
    tmp_path, monkeypatch
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"failed-master-{uuid4().hex[:8]}.mp4")
    master = output.with_name(f"{output.stem} 母版{output.suffix}")
    renderer = RenderService()
    media = MediaService(path=tmp_path / "media-library.json")
    source = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="master failure",
        output_path=str(output),
        clips=[TimelineClip(
            media_id=source.id, source_path=source.path,
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    job = JobRecord(
        request=EditJobRequest(title="master failure", media_ids=[source.id]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_render(_timeline):
        output.write_bytes(b"delivery")
        output.with_suffix(".ass").write_text("subtitle", encoding="utf-8")
        output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(output)

    async def fake_master(_timeline):
        master.write_bytes(b"master")
        master.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(master)

    monkeypatch.setattr(renderer, "render", fake_render)
    monkeypatch.setattr(renderer, "render_master", fake_master)
    monkeypatch.setattr(
        media,
        "register_generated_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GeneratedMetadataPersistenceError("simulated group registration failure")
        ),
    )
    try:
        await service._run_job(job.id)

        assert job.status == JobStatus.FAILED
        assert "simulated group registration failure" in job.error
        assert not output.exists()
        assert not output.with_suffix(".ass").exists()
        assert not output.with_suffix(".subtitles.json").exists()
        assert not master.exists()
        assert not master.with_suffix(".subtitles.json").exists()
        assert all(
            item.path not in {str(output), str(master)} for item in media.list_items()
        )
        if media.generated_metadata_path.exists():
            manifest = json.loads(
                media.generated_metadata_path.read_text(encoding="utf-8")
            )
            assert str(output) not in manifest["items"]
            assert str(master) not in manifest["items"]
    finally:
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".subtitles.json"),
        ):
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_master_render_failure_removes_the_entire_new_export_group(
    tmp_path, monkeypatch
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"master-render-failure-{uuid4().hex[:8]}.mp4")
    master = output.with_name(f"{output.stem} 母版{output.suffix}")
    renderer = RenderService()
    media = MediaService(path=tmp_path / "media-library.json")
    source = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="master render failure",
        output_path=str(output),
        clips=[TimelineClip(
            media_id=source.id, source_path=source.path,
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    job = JobRecord(
        request=EditJobRequest(title="master render failure", media_ids=[source.id]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_render(_timeline):
        output.write_bytes(b"delivery")
        output.with_suffix(".ass").write_text("subtitle", encoding="utf-8")
        output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(output)

    async def fail_master(_timeline):
        master.write_bytes(b"partial master")
        master.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        raise RuntimeError("simulated master render failure")

    monkeypatch.setattr(renderer, "render", fake_render)
    monkeypatch.setattr(renderer, "render_master", fail_master)
    try:
        await service._run_job(job.id)

        assert job.status == JobStatus.FAILED
        assert "simulated master render failure" in job.error
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".ass"),
            master.with_suffix(".subtitles.json"),
        ):
            assert not path.exists()
        assert all(
            item.path not in {str(output), str(master)} for item in media.list_items()
        )
    finally:
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".ass"),
            master.with_suffix(".subtitles.json"),
        ):
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_out_of_range_carried_cues_publish_one_unsubtitled_output(
    tmp_path, monkeypatch
):
    """Rendering, registration and master creation share the same output-clock boundary."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"outside-cues-{uuid4().hex[:8]}.mp4")
    master = output.with_name(f"{output.stem} 母版{output.suffix}")
    source_path = _write(tmp_path / "source.mp4")
    media = MediaService(path=tmp_path / "media-library.json")
    source = media.import_path(str(source_path))
    renderer = RenderService()
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="outside cues",
        output_path=str(output),
        clips=[TimelineClip(
            media_id=source.id, source_path=source.path,
            start=0, duration=2, timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(
            source_path=source.path,
            source_start=10,
            timeline_start=0,
            has_voiceover=True,
        ),
        subtitles=SubtitleTrack(cues=[
            SubtitleCue(start=1, end=2, text="no longer in this cut"),
        ]),
    )
    job = JobRecord(
        request=EditJobRequest(title="outside cues", media_ids=[source.id]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_ffmpeg(args):
        Path(args[-1]).write_bytes(b"single output")

    monkeypatch.setattr(renderer, "_run", fake_ffmpeg)
    try:
        await service._run_job(job.id)

        assert job.status == JobStatus.SUCCEEDED
        assert job.result_path == str(output)
        published = next(item for item in media.list_items() if item.path == str(output))
        assert published.metadata["variant"] == "single"
        assert published.metadata["variant_label"] == "成片"
        assert published.metadata["subtitles_path"] == ""
        assert published.metadata["has_burned_subtitles"] is False
        assert output.exists()
        assert not output.with_suffix(".ass").exists()
        assert not output.with_suffix(".subtitles.json").exists()
        assert not master.exists()
        assert not master.with_suffix(".subtitles.json").exists()
    finally:
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".subtitles.json"),
        ):
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_manual_tune_rejects_a_burned_export_even_when_it_is_not_the_first_clip(
    tmp_path,
):
    """Exports remain selectable for inspection, but cannot be submitted as re-cut picture."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    token = uuid4().hex[:8]
    original = generated_path("exports", f"burned-original-{token}.mp4")
    try:
        original.write_bytes(b"subtitle pixels")
        media = MediaService(path=tmp_path / "media-library.json")
        original_item = media.register_generated_path(
            original,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "variant": "subtitled",
                "has_burned_subtitles": True,
            },
        )
        safe_path = _write(tmp_path / "safe-source.mp4")
        safe = media.import_path(str(safe_path))
        timeline = EditTimeline(
            title="unsafe recut",
            output_path="",
            clips=[
                TimelineClip(
                    media_id=safe.id,
                    source_path=safe.path,
                    start=0,
                    duration=1,
                    timeline_start=0,
                ),
                TimelineClip(
                    # Simulate a stale/direct client trying to make a burned path look unrelated.
                    # The backend must bind provenance to the path FFmpeg will actually open.
                    media_id="stale-or-forged-export-id",
                    source_path=original_item.path,
                    start=0,
                    duration=1,
                    timeline_start=1,
                ),
            ],
        )
        service = JobService(DummyEvents(), media, None, None, RenderService())

        with pytest.raises(ValueError, match="字幕已烧录"):
            await service.create_from_timeline(timeline)

        assert service.list_jobs() == []
        assert not Path(timeline.output_path).exists()
    finally:
        original.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_manual_tune_restores_its_authoritative_track_when_an_old_client_omits_it(
    tmp_path,
):
    """The backend closes the old-client/loading-race path without silently dropping words."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    master = generated_path("exports", f"clean-master-{uuid4().hex[:8]}.mp4")
    sidecar = master.with_suffix(".subtitles.json")
    try:
        master.write_bytes(b"clean picture and mixed sound")
        sidecar.write_text(
            '{"cues":[{"start":0,"end":1,"text":"六和桥"}]}',
            encoding="utf-8",
        )
        media = MediaService(path=tmp_path / "media-library.json")
        master_item = media.register_generated_path(
            master,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "variant": "master",
                "subtitles_path": str(sidecar),
                "has_burned_subtitles": False,
                "has_voiceover": True,
            },
        )
        timeline = EditTimeline(
            title="missing carried track",
            output_path="",
            clips=[TimelineClip(
                media_id=master_item.id,
                source_path=master_item.path,
                start=0,
                duration=1,
                timeline_start=0,
            )],
            audio_bed=TimelineAudioBed(
                source_path=master_item.path,
                source_start=0,
                timeline_start=0,
                has_voiceover=True,
            ),
            subtitles=None,
        )
        service = JobService(DummyEvents(), media, None, None, RenderService())

        async def noop(_job_id):
            return None

        service._run = noop
        job = await service.create_from_timeline(timeline)

        assert job.timeline.subtitles is not None
        assert [cue.text for cue in job.timeline.subtitles.cues] == ["六和桥"]
        assert job.request.subtitles is True
        assert not Path(timeline.output_path).exists()
    finally:
        master.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_manual_tune_replaces_another_exports_subtitles_with_the_beds_own_track(
    tmp_path,
):
    """A direct client cannot pair export A's soundtrack with export B's submitted words."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    master = generated_path("exports", f"bound-master-{uuid4().hex[:8]}.mp4")
    sidecar = master.with_suffix(".subtitles.json")
    try:
        master.write_bytes(b"clean picture and mixed sound")
        sidecar.write_text(
            json.dumps({
                "video": master.name,
                "cues": [{"start": 0, "end": 1, "text": "六和桥"}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        media = MediaService(path=tmp_path / "media-library.json")
        master_item = media.register_generated_path(
            master,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "variant": "master",
                "subtitles_path": str(sidecar),
                "has_burned_subtitles": False,
                "has_voiceover": True,
            },
        )
        timeline = EditTimeline(
            title="mismatched client track",
            output_path="",
            clips=[TimelineClip(
                media_id=master_item.id,
                source_path=master_item.path,
                start=0,
                duration=1,
                timeline_start=0,
            )],
            audio_bed=TimelineAudioBed(
                source_path=master_item.path,
                source_start=0,
                timeline_start=0,
                has_voiceover=True,
            ),
            subtitles=SubtitleTrack(
                cues=[SubtitleCue(start=0, end=1, text="错误的六合桥")]
            ),
        )
        service = JobService(DummyEvents(), media, None, None, RenderService())

        async def noop(_job_id):
            return None

        service._run = noop
        job = await service.create_from_timeline(timeline)

        assert job.timeline.subtitles is not None
        assert [cue.text for cue in job.timeline.subtitles.cues] == ["六和桥"]
        assert all(cue.text != "错误的六合桥" for cue in job.timeline.subtitles.cues)
        assert not Path(timeline.output_path).exists()
    finally:
        master.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)


def _write(path):
    path.write_bytes(b"placeholder")
    return path


def _write_cruise(path, points=6):
    """A recording with cruise spans beside it.

    Most editing dimensions act on the points a cruise recorded, so plain footage cannot
    exercise them — and, since the batch stopped dealing levels that cannot do anything, no
    longer even receives them.
    """
    import json

    path.write_bytes(b"placeholder")
    cursor, segments = 0.0, []
    for index in range(points):
        segments.append({
            "index": index, "path_name": "path1", "goal_id": index + 1, "status": "arrived",
            "transit_start_seconds": cursor, "arrived_at_seconds": cursor + 12.0,
            "departed_at_seconds": cursor + 20.0,
        })
        cursor += 20.0
    path.with_name(path.name + ".capture.json").write_text(
        json.dumps({"markers": [], "segments": segments}), encoding="utf-8",
    )
    return path


def test_export_is_named_after_the_edit_title(tmp_path):
    """The 剪辑标题 box was ignored: every export came out as 导出 regardless of what you
    typed. A Chinese title also had to survive, since the old sanitiser stripped non-ASCII."""
    import re

    from automated_video_editing_backend.core.events import EventHub
    from automated_video_editing_backend.services.jobs import JobService
    from automated_video_editing_backend.services.render import RenderService

    jobs = JobService(EventHub(), MediaService(path=tmp_path / "m.json"), None, None, RenderService())

    named = jobs._next_output_name("节拍剪辑")
    assert re.fullmatch(r"节拍剪辑 \d{2}-\d{2} \d{2}-\d{2}\.mp4", named), named

    # A blank title still falls back to something sensible.
    assert jobs._next_output_name("").startswith("导出 ")
    # A title that is only illegal characters must not produce an empty filename.
    assert jobs._next_output_name("///").startswith("导出 ")


def test_two_exports_in_the_same_minute_do_not_collide(tmp_path):
    from automated_video_editing_backend.core.events import EventHub
    from automated_video_editing_backend.services.jobs import JobService
    from automated_video_editing_backend.services.render import RenderService

    jobs = JobService(EventHub(), MediaService(path=tmp_path / "m.json"), None, None, RenderService())
    names = {jobs._next_output_name("节拍剪辑") for _ in range(3)}
    assert len(names) == 3, names


@pytest.mark.parametrize(
    "blocked_member",
    [
        "delivery",
        "master",
        "delivery_ass",
        "delivery_subtitles",
        "master_ass",
        "master_subtitles",
    ],
)
def test_output_name_reserves_the_whole_export_file_family(
    tmp_path, monkeypatch, blocked_member
):
    """An orphaned hidden companion must not be overwritten by a later render."""
    from datetime import datetime as real_datetime

    import automated_video_editing_backend.services.jobs as jobs_module

    media = MediaService(path=tmp_path / "media-library.json")
    jobs = JobService(DummyEvents(), media, None, None, None)

    class FrozenDatetime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 9, 4, 14, 23).astimezone()

    monkeypatch.setattr(jobs_module, "datetime", FrozenDatetime)
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "exports", tmp_path)

    delivery = tmp_path / "客户成片 09-04 14-23.mp4"
    master = tmp_path / "客户成片 09-04 14-23 母版.mp4"
    family = {
        "delivery": delivery,
        "master": master,
        "delivery_ass": delivery.with_suffix(".ass"),
        "delivery_subtitles": delivery.with_suffix(".subtitles.json"),
        "master_ass": master.with_suffix(".ass"),
        "master_subtitles": master.with_suffix(".subtitles.json"),
    }
    family[blocked_member].write_bytes(b"existing")

    assert jobs._next_output_name("客户成片") == "客户成片 09-04 14-23_01.mp4"


@pytest.mark.asyncio
async def test_manual_tune_render_also_honours_the_original_audio_toggle(tmp_path):
    """手动微调 renders a client-built timeline, which skipped the audio resolution entirely.

    The toggle was therefore dead on that path even after it was wired up for automation.
    """
    import subprocess

    from automated_video_editing_backend.core.models import EditTimeline, TimelineClip
    from automated_video_editing_backend.services.render import RenderService

    clip_path = tmp_path / "withsound.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip_path)],
        check=True,
    )
    silent_path = tmp_path / "nosound.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=2", str(silent_path)],
        check=True,
    )

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    clip_item = media.import_path(str(clip_path))
    silent_item = media.import_path(str(silent_path))
    service = JobService(DummyEvents(), media, None, None, RenderService())

    async def noop(_job_id):
        return None

    service._run = noop

    def timeline_for(source, mute):
        return EditTimeline(
            title="tune", output_path="", mute_original_audio=mute,
            clips=[TimelineClip(
                media_id=source.id,
                source_path=source.path,
                start=0,
                duration=1,
                timeline_start=0,
            )],
        )

    kept = await service.create_from_timeline(timeline_for(clip_item, mute=False))
    assert kept.timeline.include_original_audio is True

    muted = await service.create_from_timeline(timeline_for(clip_item, mute=True))
    assert muted.timeline.include_original_audio is False

    # A source with no audio track downgrades instead of failing the whole render.
    downgraded = await service.create_from_timeline(timeline_for(silent_item, mute=False))
    assert downgraded.timeline.include_original_audio is False
    assert any("没有声音轨" in warning for warning in downgraded.timeline.warnings)

    effect_timeline = timeline_for(clip_item, mute=True)
    effect_timeline.clips[0].include_audio = True
    effect_timeline.clips[0].audio_volume = 0.3
    effect = await service.create_from_timeline(effect_timeline)
    assert effect.timeline.clips[0].include_audio is True
    assert effect.timeline.clips[0].audio_volume == 0.3

    silent_effect_timeline = timeline_for(silent_item, mute=True)
    silent_effect_timeline.clips[0].include_audio = True
    silent_effect = await service.create_from_timeline(silent_effect_timeline)
    assert silent_effect.timeline.clips[0].include_audio is False
    assert any("静音特效" in warning for warning in silent_effect.timeline.warnings)
