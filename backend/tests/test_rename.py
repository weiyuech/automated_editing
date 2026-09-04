import json
import shutil
from copy import deepcopy
from uuid import uuid4

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    EditJobRequest,
    EditTimeline,
    JobRecord,
    JobStatus,
    SubtitleCue,
    SubtitleTrack,
    TimelineAudioBed,
    TimelineClip,
)
from automated_video_editing_backend.core.store import write_json
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import (
    GeneratedMetadataPersistenceError,
    MediaLibraryPersistenceError,
    MediaPoolPersistenceError,
    MediaService,
)
from automated_video_editing_backend.services.naming import stamped_name, validate_filename
from automated_video_editing_backend.services.rename import MediaRenameService
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.seedance import SeedanceService
from automated_video_editing_backend.services.settings import SettingsService


@pytest.fixture
def bundle(tmp_path, seedance_root):
    media = MediaService(path=tmp_path / "media-library.json")
    jobs = JobService(EventHub(), media, None, None, RenderService())
    seedance = SeedanceService(SettingsService(path=tmp_path / "s.json"), media, RenderService(), root=seedance_root)
    return media, jobs, seedance, MediaRenameService(media, jobs, seedance)


@pytest.fixture
def seedance_root():
    from automated_video_editing_backend.core.paths import generated_path

    root = generated_path("cache", "test-rename", uuid4().hex[:8])
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_generated_names_are_readable_and_unique_per_minute():
    first = stamped_name("图片特效", ".png")
    assert first.startswith("图片特效 ") and first.endswith(".png")
    # Two in the same minute must not collide.
    assert stamped_name("图片特效", ".png", {first}) != first


@pytest.mark.parametrize("bad", ["", "   ", "..", "a/b", "a\\b", "a:b", "a?b", "x" * 200])
def test_unsafe_names_are_refused(bad):
    with pytest.raises(ValueError):
        validate_filename(bad, ".png")


def test_a_missing_extension_is_filled_in():
    assert validate_filename("早班主图", ".png") == "早班主图.png"
    assert validate_filename("早班主图.png", ".png") == "早班主图.png"


def test_rename_refuses_to_change_the_media_type(bundle, tmp_path):
    media, _, _, renamer = bundle
    source = tmp_path / "delivery.mp4"
    source.write_bytes(b"video")
    item = media.import_path(str(source))

    with pytest.raises(ValueError, match="文件类型必须保持为 .mp4"):
        renamer.rename(item.id, "delivery.txt")

    assert source.exists()
    assert not (tmp_path / "delivery.txt").exists()


def test_renaming_moves_the_real_file_and_repoints_the_library(bundle, tmp_path):
    media, _, _, renamer = bundle
    clip = tmp_path / "old.mp4"
    clip.write_bytes(b"video")
    item = media.import_path(str(clip))

    renamed = renamer.rename(item.id, "早班主图")

    assert not clip.exists()
    assert (tmp_path / "早班主图.mp4").exists()
    assert renamed.path == str(tmp_path / "早班主图.mp4")
    assert media.get(item.id).path == str(tmp_path / "早班主图.mp4")


def test_renaming_onto_an_existing_name_is_refused(bundle, tmp_path):
    media, _, _, renamer = bundle
    clip = tmp_path / "a.mp4"
    clip.write_bytes(b"a")
    (tmp_path / "taken.mp4").write_bytes(b"b")
    item = media.import_path(str(clip))

    with pytest.raises(ValueError, match="already exists"):
        renamer.rename(item.id, "taken.mp4")
    # The original must survive a refused rename.
    assert clip.exists()


def test_a_tts_sidecar_moves_with_its_audio(bundle, tmp_path):
    media, _, _, renamer = bundle
    audio = tmp_path / "voiceover-abc.mp3"
    audio.write_bytes(b"audio")
    (tmp_path / "voiceover-abc.json").write_text('{"text": "hi"}', encoding="utf-8")
    item = media.import_path(str(audio))
    item.metadata.update({
        "role": "tts_voice",
        "metadata_path": str(tmp_path / "voiceover-abc.json"),
    })

    renamed = renamer.rename(item.id, "早班旁白")

    # The pair is matched by filename stem, so an orphaned .json would break the asset list.
    assert (tmp_path / "早班旁白.mp3").exists()
    assert (tmp_path / "早班旁白.json").exists()
    assert not (tmp_path / "voiceover-abc.json").exists()
    assert renamed.metadata["metadata_path"] == str(tmp_path / "早班旁白.json")
    reopened = MediaService(path=media.path)
    restored = reopened.get(item.id)
    assert restored is not None
    assert restored.metadata["metadata_path"] == str(tmp_path / "早班旁白.json")


def test_tts_rename_rejects_an_existing_target_sidecar(bundle, tmp_path):
    media, _, _, renamer = bundle
    source = tmp_path / "voice.mp3"
    source_sidecar = tmp_path / "voice.json"
    target_sidecar = tmp_path / "taken.json"
    source.write_bytes(b"audio")
    source_sidecar.write_text('{"text": "source"}', encoding="utf-8")
    target_sidecar.write_text('{"text": "unrelated"}', encoding="utf-8")
    item = media.import_path(str(source))
    item.metadata["role"] = "tts_voice"

    with pytest.raises(ValueError, match="taken.json.*already exists"):
        renamer.rename(item.id, "taken.mp3")

    assert source.exists()
    assert source_sidecar.exists()
    assert not (tmp_path / "taken.mp3").exists()
    assert json.loads(target_sidecar.read_text(encoding="utf-8"))["text"] == "unrelated"


def test_export_subtitle_layers_move_with_the_video(bundle, tmp_path):
    media, _, _, renamer = bundle
    export = tmp_path / "成片.mp4"
    export.write_bytes(b"video")
    track = tmp_path / "成片.subtitles.json"
    track.write_text('{"cues": [{"start": 0, "end": 1, "text": "一"}]}', encoding="utf-8")
    ass = tmp_path / "成片.ass"
    ass.write_text("subtitle", encoding="utf-8")
    item = media.import_path(str(export))
    item.metadata.update({"role": "export", "subtitles_path": str(track)})

    renamed = renamer.rename(item.id, "新成片")

    assert renamed.path == str(tmp_path / "新成片.mp4")
    assert (tmp_path / "新成片.subtitles.json").exists()
    assert (tmp_path / "新成片.ass").exists()
    assert not track.exists()
    assert not ass.exists()
    assert renamed.metadata["subtitles_path"] == str(tmp_path / "新成片.subtitles.json")
    assert json.loads((tmp_path / "新成片.subtitles.json").read_text(encoding="utf-8"))["video"] == "新成片.mp4"


def test_export_rename_never_rebinds_another_videos_subtitles(bundle, tmp_path):
    media, _, _, renamer = bundle
    source = tmp_path / "delivery.mp4"
    source.write_bytes(b"video")
    sidecar = source.with_suffix(".subtitles.json")
    sidecar.write_text(
        '{"video": "another.mp4", "cues": [{"start": 0, "end": 1, "text": "wrong"}]}',
        encoding="utf-8",
    )
    item = media.import_path(str(source))
    item.metadata.update({"role": "export", "subtitles_path": str(sidecar)})

    with pytest.raises(ValueError, match="属于另一个成片"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.exists()
    assert not (tmp_path / "renamed.mp4").exists()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["video"] == "another.mp4"


def test_export_rename_rejects_a_present_malformed_subtitle_binding(bundle, tmp_path):
    media, _, _, renamer = bundle
    source = tmp_path / "delivery.mp4"
    source.write_bytes(b"video")
    sidecar = source.with_suffix(".subtitles.json")
    sidecar.write_text(
        '{"video": 123, "cues": [{"start": 0, "end": 1, "text": "wrong"}]}',
        encoding="utf-8",
    )
    item = media.import_path(str(source))
    item.metadata.update({"role": "export", "subtitles_path": str(sidecar)})

    with pytest.raises(ValueError, match="video 绑定无效"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.exists()
    assert not (tmp_path / "renamed.mp4").exists()
    assert json.loads(sidecar.read_text(encoding="utf-8"))["video"] == 123


@pytest.mark.parametrize("companion_suffix", [".subtitles.json", ".ass"])
def test_export_rename_rejects_an_existing_target_companion(
    bundle, tmp_path, companion_suffix
):
    media, _, _, renamer = bundle
    source = tmp_path / "delivery.mp4"
    source.write_bytes(b"video")
    target_companion = (tmp_path / "taken.mp4").with_suffix(companion_suffix)
    target_companion.write_text("unrelated", encoding="utf-8")
    item = media.import_path(str(source))
    item.metadata["role"] = "export"

    with pytest.raises(ValueError, match="already exists"):
        renamer.rename(item.id, "taken.mp4")

    assert source.exists()
    assert not (tmp_path / "taken.mp4").exists()
    assert target_companion.read_text(encoding="utf-8") == "unrelated"


def test_export_rename_rolls_back_every_file_when_subtitle_rewrite_fails(
    bundle, tmp_path, monkeypatch
):
    media, jobs, _, renamer = bundle
    source = tmp_path / "delivery.mp4"
    source_track = tmp_path / "delivery.subtitles.json"
    source_ass = tmp_path / "delivery.ass"
    source.write_bytes(b"video")
    source_track.write_text(
        '{"video": "delivery.mp4", "cues": [{"text": "source"}]}',
        encoding="utf-8",
    )
    source_ass.write_text("source subtitles", encoding="utf-8")
    item = media.import_path(str(source))
    item.metadata.update({"role": "export", "subtitles_path": str(source_track)})
    job = JobRecord(
        request=EditJobRequest(title="t"),
        status=JobStatus.SUCCEEDED,
        result_path=str(source),
    )
    jobs._jobs[job.id] = job

    monkeypatch.setattr(
        "automated_video_editing_backend.services.rename.write_json",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(ValueError, match="无法更新"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.read_bytes() == b"video"
    assert json.loads(source_track.read_text(encoding="utf-8"))["video"] == "delivery.mp4"
    assert source_ass.read_text(encoding="utf-8") == "source subtitles"
    assert not (tmp_path / "renamed.mp4").exists()
    assert not (tmp_path / "renamed.subtitles.json").exists()
    assert not (tmp_path / "renamed.ass").exists()
    assert media.get(item.id).path == str(source)
    assert media.get(item.id).metadata["subtitles_path"] == str(source_track)
    assert job.result_path == str(source)


def test_export_rename_rolls_back_after_the_files_move_if_final_swap_fails(
    bundle, tmp_path, monkeypatch
):
    media, _, _, renamer = bundle
    source = tmp_path / "delivery.mp4"
    source_track = tmp_path / "delivery.subtitles.json"
    source_ass = tmp_path / "delivery.ass"
    source.write_bytes(b"video")
    source_track.write_text('{"video": "delivery.mp4", "cues": []}', encoding="utf-8")
    source_ass.write_text("source subtitles", encoding="utf-8")
    item = media.import_path(str(source))
    item.metadata.update({"role": "export", "subtitles_path": str(source_track)})

    original_replace = type(source).replace

    def fail_final_swap(path, target):
        if path.name.endswith(".rename-stage"):
            raise OSError("simulated final swap failure")
        return original_replace(path, target)

    monkeypatch.setattr(type(source), "replace", fail_final_swap)

    with pytest.raises(ValueError, match="重命名失败"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.read_bytes() == b"video"
    assert json.loads(source_track.read_text(encoding="utf-8"))["video"] == "delivery.mp4"
    assert source_ass.read_text(encoding="utf-8") == "source subtitles"
    assert not (tmp_path / "renamed.mp4").exists()
    assert not (tmp_path / "renamed.subtitles.json").exists()
    assert not (tmp_path / "renamed.ass").exists()
    assert media.get(item.id).path == str(source)


def test_rename_rolls_back_files_when_media_pool_persistence_fails(
    bundle, tmp_path, monkeypatch
):
    media, _, _, renamer = bundle
    source = tmp_path / "pooled.mp4"
    source.write_bytes(b"video")
    item = media.import_path(str(source))
    media.update_media_pool({"source_media_ids": [item.id]})

    def fail_pool_save(*_args, **_kwargs):
        raise MediaPoolPersistenceError("simulated pool write failure")

    monkeypatch.setattr(media, "_save_pool", fail_pool_save)

    with pytest.raises(MediaPoolPersistenceError, match="simulated"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.exists()
    assert not (tmp_path / "renamed.mp4").exists()
    assert media.get(item.id).path == str(source)
    persisted = json.loads(media.pool_path.read_text(encoding="utf-8"))
    assert persisted["source_media_ids"] == [str(source)]


def test_rename_restores_pool_when_import_manifest_is_the_second_failed_commit(
    bundle, tmp_path, monkeypatch
):
    media, _, _, renamer = bundle
    source = tmp_path / "pooled-local.mp4"
    source.write_bytes(b"video")
    item = media.import_path(str(source))
    media.update_media_pool({"source_media_ids": [item.id]})
    before_library = media.path.read_bytes()

    def fail_import_save(*_args, **_kwargs):
        raise MediaLibraryPersistenceError("simulated import manifest failure")

    monkeypatch.setattr(media, "_save_imports", fail_import_save)

    with pytest.raises(MediaLibraryPersistenceError, match="simulated"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.exists()
    assert not (tmp_path / "renamed.mp4").exists()
    assert item.path == str(source)
    assert media.path.read_bytes() == before_library
    persisted_pool = json.loads(media.pool_path.read_text(encoding="utf-8"))
    assert persisted_pool["source_media_ids"] == [str(source)]
    assert media.media_pool()["source_media_ids"] == [item.id]


def test_rename_is_refused_while_the_media_pool_manifest_is_unreadable(
    bundle, tmp_path
):
    media, _, _, renamer = bundle
    source = tmp_path / "selected.mp4"
    source.write_bytes(b"video")
    item = media.import_path(str(source))
    media.media_pool()
    before_library = media.path.read_bytes()
    before_pool = media.pool_path.read_bytes()
    media._pool_blocked = True
    media.media_pool_problem = "simulated damaged pool"

    with pytest.raises(MediaPoolPersistenceError, match="damaged pool"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.exists()
    assert not (tmp_path / "renamed.mp4").exists()
    assert item.path == str(source)
    assert media.path.read_bytes() == before_library
    assert media.pool_path.read_bytes() == before_pool


def test_export_rename_rolls_back_when_generated_manifest_cannot_commit(
    bundle, monkeypatch
):
    from automated_video_editing_backend.core.paths import generated_path

    media, jobs, _, renamer = bundle
    stem = f"rename-rollback-{uuid4().hex[:8]}"
    source = generated_path("exports", f"{stem}.mp4")
    target = generated_path("exports", f"{stem}-renamed.mp4")
    source_track = source.with_suffix(".subtitles.json")
    target_track = target.with_suffix(".subtitles.json")
    source_ass = source.with_suffix(".ass")
    target_ass = target.with_suffix(".ass")
    source.write_bytes(b"video")
    source_track.write_text(
        json.dumps({
            "video": source.name,
            "cues": [{"start": 0, "end": 1, "text": "kept"}],
        }),
        encoding="utf-8",
    )
    source_ass.write_text("subtitle", encoding="utf-8")
    try:
        item = media.register_generated_path(source, kind="video", metadata={
            "source": "exports",
            "role": "export",
            "export_group": f"export:{uuid4()}",
            "variant": "subtitled",
            "subtitles_path": str(source_track),
        })
        original_metadata = deepcopy(item.metadata)
        job = JobRecord(
            request=EditJobRequest(title="t"),
            status=JobStatus.SUCCEEDED,
            result_path=str(source),
            timeline=EditTimeline(
                title="t",
                clips=[],
                output_path=str(source),
            ),
        )
        jobs._jobs[job.id] = job
        before_manifest = media.generated_metadata_path.read_bytes()

        def fail_generated_save(*_args, **_kwargs):
            raise GeneratedMetadataPersistenceError("simulated manifest write failure")

        monkeypatch.setattr(media, "_save_generated_metadata", fail_generated_save)

        with pytest.raises(GeneratedMetadataPersistenceError, match="simulated"):
            renamer.rename(item.id, target.name)

        assert source.read_bytes() == b"video"
        assert source_ass.read_text(encoding="utf-8") == "subtitle"
        assert json.loads(source_track.read_text(encoding="utf-8"))["video"] == source.name
        assert not target.exists()
        assert not target_track.exists()
        assert not target_ass.exists()
        assert item.path == str(source)
        assert item.metadata == original_metadata
        assert job.result_path == str(source)
        assert job.timeline.output_path == str(source)
        assert media.generated_metadata_path.read_bytes() == before_manifest
    finally:
        for path in (source, target, source_track, target_track, source_ass, target_ass):
            path.unlink(missing_ok=True)


def test_an_effects_metadata_pointer_is_updated(bundle, tmp_path):
    media, _, seedance, renamer = bundle
    png = seedance.effects_dir / "uuid-name.png"
    png.write_bytes(b"img")
    metadata = seedance.effects_dir / "uuid-name.json"
    metadata.write_text(json.dumps({"id": "x", "name": "uuid-name.png", "output_path": str(png),
                                    "metadata_path": str(metadata), "prompt": "p", "kind": "image"}), encoding="utf-8")
    item = media.import_path(str(png))

    renamer.rename(item.id, "早班主图")

    # Effects are paired by a field inside the json, not by filename.
    data = json.loads(metadata.read_text(encoding="utf-8"))
    assert data["output_path"] == str(seedance.effects_dir / "早班主图.png")
    assert data["name"] == "早班主图.png"


def test_effect_rename_rolls_back_when_its_history_cannot_be_saved(
    bundle, monkeypatch
):
    media, _, seedance, renamer = bundle
    source = seedance.effects_dir / "effect-source.png"
    target = seedance.effects_dir / "effect-renamed.png"
    metadata = seedance.effects_dir / "effect-id.json"
    source.write_bytes(b"image")
    original = {
        "id": "effect-id",
        "name": source.name,
        "output_path": str(source),
        "metadata_path": str(metadata),
        "prompt": "kept prompt",
        "kind": "image",
    }
    metadata.write_text(json.dumps(original), encoding="utf-8")
    item = media.import_path(str(source))
    item.metadata.update({
        "role": "seedance_effect",
        "seedance_asset_id": "effect-id",
    })
    real_write_json = write_json

    def fail_effect_metadata(path, payload):
        if path == metadata:
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(
        "automated_video_editing_backend.services.rename.write_json",
        fail_effect_metadata,
    )

    with pytest.raises(ValueError, match="无法更新特效文件名"):
        renamer.rename(item.id, target.name)

    assert source.read_bytes() == b"image"
    assert not target.exists()
    assert item.path == str(source)
    assert json.loads(metadata.read_text(encoding="utf-8")) == original


def test_effect_history_is_restored_when_a_later_pool_commit_rejects_rename(
    bundle, monkeypatch
):
    media, _, seedance, renamer = bundle
    source = seedance.effects_dir / "effect-source.mp4"
    target = seedance.effects_dir / "effect-renamed.mp4"
    metadata = seedance.effects_dir / "effect-video-id.json"
    source.write_bytes(b"video")
    original = {
        "id": "effect-video-id",
        "name": source.name,
        "output_path": str(source),
        "metadata_path": str(metadata),
        "prompt": "kept prompt",
        "kind": "video",
    }
    metadata.write_text(json.dumps(original), encoding="utf-8")
    item = media.register_generated_path(source, kind="video", metadata={
        "source": "test_seedance_effect",
        "role": "seedance_effect",
        "seedance_asset_id": "effect-video-id",
    })
    media.update_media_pool({"effect_media_ids": [item.id]})

    def fail_pool_save(*_args, **_kwargs):
        raise MediaPoolPersistenceError("simulated later pool failure")

    monkeypatch.setattr(media, "_save_pool", fail_pool_save)

    with pytest.raises(MediaPoolPersistenceError, match="later pool failure"):
        renamer.rename(item.id, target.name)

    assert source.read_bytes() == b"video"
    assert not target.exists()
    assert item.path == str(source)
    assert json.loads(metadata.read_text(encoding="utf-8")) == original


def test_renaming_is_blocked_while_a_render_uses_the_file(bundle, tmp_path):
    media, jobs, _, renamer = bundle
    clip = tmp_path / "in-use.mp4"
    clip.write_bytes(b"v")
    item = media.import_path(str(clip))

    timeline = EditTimeline(title="t", clips=[TimelineClip(media_id=item.id, source_path=str(clip),
                                                          start=0, duration=1, timeline_start=0)],
                            output_path=str(tmp_path / "out.mp4"))
    job = JobRecord(request=EditJobRequest(title="t"), timeline=timeline, status=JobStatus.RUNNING)
    jobs._jobs[job.id] = job

    with pytest.raises(ValueError, match="render"):
        renamer.rename(item.id, "新名字")
    assert clip.exists()

    job.status = JobStatus.SUCCEEDED
    renamer.rename(item.id, "新名字")
    assert (tmp_path / "新名字.mp4").exists()


def test_renaming_is_blocked_while_automatic_analysis_has_no_timeline_yet(
    bundle, tmp_path
):
    media, jobs, _, renamer = bundle
    source = tmp_path / "being-analyzed.mp4"
    source.write_bytes(b"video")
    item = media.import_path(str(source))
    job = JobRecord(
        request=EditJobRequest(title="t", media_ids=[item.id]),
        status=JobStatus.RUNNING,
        timeline=None,
    )
    jobs._jobs[job.id] = job

    with pytest.raises(ValueError, match="render"):
        renamer.rename(item.id, "renamed.mp4")

    assert source.exists()
    assert not (tmp_path / "renamed.mp4").exists()


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
@pytest.mark.parametrize("reserved_output", ["delivery", "master"])
def test_rename_cannot_claim_an_active_jobs_future_output_name(
    bundle, tmp_path, status, reserved_output
):
    media, jobs, _, renamer = bundle
    source = tmp_path / "rename-me.mp4"
    source.write_bytes(b"source video")
    item = media.import_path(str(source))

    delivery = tmp_path / "reserved.mp4"
    timeline = EditTimeline(
        title="active export",
        clips=[],
        output_path=str(delivery),
        subtitles=SubtitleTrack(
            cues=[SubtitleCue(start=0.0, end=1.0, text="字幕")]
        ),
    )
    target = delivery if reserved_output == "delivery" else jobs.renderer.master_output_path(timeline)
    job = JobRecord(
        request=EditJobRequest(title="active export"),
        timeline=timeline,
        status=status,
    )
    jobs._jobs[job.id] = job

    with pytest.raises(ValueError, match="render"):
        renamer.rename(item.id, target.name)

    # The reservation is checked before the source or any of its companions can move.
    assert source.read_bytes() == b"source video"
    assert not target.exists()
    assert media.get(item.id).path == str(source)


def test_a_recordings_markers_follow_it(bundle, tmp_path):
    """A recording's notes and markers live in '<full filename>.capture.json'. Renaming the
    video without moving that file orphans every point marker on it."""
    from automated_video_editing_backend.services.capture import read_sidecar, sidecar_path

    media, _, _, renamer = bundle
    video = tmp_path / "robot-cruise.mp4"
    video.write_bytes(b"video")
    sidecar_path(video).write_text('{"markers": [{"timestamp": 12.4, "label": "path1#1"}]}', encoding="utf-8")
    item = media.import_path(str(video))

    renamer.rename(item.id, "早班巡游")

    renamed = tmp_path / "早班巡游.mp4"
    assert renamed.exists()
    assert not sidecar_path(video).exists()
    assert read_sidecar(renamed)["markers"][0]["label"] == "path1#1"


def test_recording_rename_rejects_an_existing_target_capture_sidecar(bundle, tmp_path):
    from automated_video_editing_backend.services.capture import sidecar_path

    media, _, _, renamer = bundle
    source = tmp_path / "robot-cruise.mp4"
    target = tmp_path / "taken.mp4"
    source.write_bytes(b"video")
    sidecar_path(source).write_text('{"markers": [{"label": "source"}]}', encoding="utf-8")
    sidecar_path(target).write_text('{"markers": [{"label": "target"}]}', encoding="utf-8")
    item = media.import_path(str(source))

    with pytest.raises(ValueError, match="taken.mp4.capture.json.*already exists"):
        renamer.rename(item.id, target.name)

    assert source.exists()
    assert json.loads(sidecar_path(source).read_text(encoding="utf-8"))["markers"][0]["label"] == "source"
    assert json.loads(sidecar_path(target).read_text(encoding="utf-8"))["markers"][0]["label"] == "target"
    assert not target.exists()


def test_video_rename_cannot_adopt_a_target_capture_sidecar_when_source_has_none(
    bundle, tmp_path
):
    from automated_video_editing_backend.services.capture import sidecar_path

    media, _, _, renamer = bundle
    source = tmp_path / "plain.mp4"
    target = tmp_path / "taken.mp4"
    source.write_bytes(b"video")
    sidecar_path(target).write_text(
        '{"markers": [{"label": "belongs elsewhere"}]}', encoding="utf-8"
    )
    item = media.import_path(str(source))

    with pytest.raises(ValueError, match="taken.mp4.capture.json.*already exists"):
        renamer.rename(item.id, target.name)

    assert source.exists()
    assert not target.exists()
    assert json.loads(sidecar_path(target).read_text(encoding="utf-8"))["markers"][0][
        "label"
    ] == "belongs elsewhere"


def test_renaming_is_blocked_while_a_render_uses_the_file_as_its_audio_bed(
    bundle, tmp_path
):
    media, jobs, _, renamer = bundle
    audio = tmp_path / "master-with-mix.mp4"
    audio.write_bytes(b"video-and-audio")
    item = media.import_path(str(audio))
    timeline = EditTimeline(
        title="fine tune",
        clips=[],
        audio_bed=TimelineAudioBed(source_path=str(audio)),
        output_path=str(tmp_path / "out.mp4"),
    )
    job = JobRecord(
        request=EditJobRequest(title="fine tune"),
        timeline=timeline,
        status=JobStatus.RUNNING,
    )
    jobs._jobs[job.id] = job

    with pytest.raises(ValueError, match="render"):
        renamer.rename(item.id, "renamed.mp4")

    assert audio.exists()
    assert not (tmp_path / "renamed.mp4").exists()


def test_a_renamed_export_stays_linked_in_the_render_queue(bundle, tmp_path):
    from automated_video_editing_backend.core.models import EditJobRequest, JobRecord, JobStatus

    media, jobs, _, renamer = bundle
    export = tmp_path / "导出 08-06 10-00.mp4"
    export.write_bytes(b"v")
    item = media.import_path(str(export))

    job = JobRecord(request=EditJobRequest(title="t"), status=JobStatus.SUCCEEDED, result_path=str(export))
    jobs._jobs[job.id] = job

    renamer.rename(item.id, "早班成片")

    # Otherwise 渲染队列 would keep a dead link to the old filename.
    assert job.result_path == str(tmp_path / "早班成片.mp4")


def test_a_renamed_generated_export_keeps_its_group_after_restart(bundle):
    """Renaming changes the manifest key as well as the file visible in the library."""
    from automated_video_editing_backend.core.paths import generated_path

    media, _, _, renamer = bundle
    stem = f"rename-group-{uuid4().hex[:8]}"
    source = generated_path("exports", f"{stem}.mp4")
    target = generated_path("exports", f"{stem}-renamed.mp4")
    source_subtitles = source.with_suffix(".subtitles.json")
    target_subtitles = target.with_suffix(".subtitles.json")
    source.write_bytes(b"video")
    source_subtitles.write_text('{"cues": []}', encoding="utf-8")
    group = f"export:{uuid4()}"
    try:
        item = media.register_generated_path(source, kind="video", metadata={
            "source": "exports",
            "role": "export",
            "export_group": group,
            "variant": "subtitled",
            "variant_label": "成片（带字幕）",
            "subtitles_path": str(source_subtitles),
            "has_burned_subtitles": True,
            "has_voiceover": True,
        })

        renamed = renamer.rename(item.id, target.name)
        reopened = MediaService(path=media.path)
        restored = next(entry for entry in reopened.list_items() if entry.path == str(target))

        assert renamed.path == str(target)
        assert restored.metadata["export_group"] == group
        assert restored.metadata["variant_label"] == "成片（带字幕）"
        assert restored.metadata["subtitles_path"] == str(target_subtitles)
        assert restored.metadata["has_burned_subtitles"] is True
        assert restored.metadata["has_voiceover"] is True
        assert json.loads(target_subtitles.read_text(encoding="utf-8"))["video"] == target.name
        assert str(source) not in json.loads(
            media.generated_metadata_path.read_text(encoding="utf-8")
        )["items"]
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        source_subtitles.unlink(missing_ok=True)
        target_subtitles.unlink(missing_ok=True)


def test_export_rename_rejects_a_missing_recorded_subtitle_sidecar(bundle):
    from automated_video_editing_backend.core.paths import generated_path

    media, _, _, renamer = bundle
    stem = f"rename-missing-sidecar-{uuid4().hex[:8]}"
    source = generated_path("exports", f"{stem}.mp4")
    target = generated_path("exports", f"{stem}-renamed.mp4")
    sidecar = source.with_suffix(".subtitles.json")
    source.write_bytes(b"video")
    sidecar.write_text('{"cues": []}', encoding="utf-8")
    try:
        item = media.register_generated_path(source, kind="video", metadata={
            "source": "exports",
            "role": "export",
            "export_group": f"export:{uuid4()}",
            "variant": "master",
            "subtitles_path": str(sidecar),
        })
        manifest_before = media.generated_metadata_path.read_bytes()
        sidecar.unlink()

        with pytest.raises(ValueError, match="字幕文件缺失"):
            renamer.rename(item.id, target.name)

        assert source.is_file()
        assert not target.exists()
        assert item.path == str(source)
        assert media.generated_metadata_path.read_bytes() == manifest_before
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
