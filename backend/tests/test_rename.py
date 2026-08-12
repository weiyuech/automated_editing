import json

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import JobRecord, JobStatus, EditJobRequest, EditTimeline, TimelineClip
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService
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
    import shutil
    from uuid import uuid4

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
    item.metadata["role"] = "tts_voice"

    renamer.rename(item.id, "早班旁白")

    # The pair is matched by filename stem, so an orphaned .json would break the asset list.
    assert (tmp_path / "早班旁白.mp3").exists()
    assert (tmp_path / "早班旁白.json").exists()
    assert not (tmp_path / "voiceover-abc.json").exists()


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
