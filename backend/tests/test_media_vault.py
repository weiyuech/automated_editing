import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.media import (
    GeneratedMetadataPersistenceError,
    MediaService,
)
from automated_video_editing_backend.services.media_vault import MediaVaultService


def test_vault_classifies_downloads_and_exports_separately():
    raw = generated_path("data", "downloads", "vault-raw-test.mp4")
    music = generated_path("data", "downloads", "vault-music-test.mp3")
    export = generated_path("exports", "vault-export-test.mp4")
    for path in [raw, music, export]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")

    try:
        assets = MediaVaultService().list_assets()
        by_path = {asset.path: asset for asset in assets}

        assert by_path[str(raw)].role == "raw_video"
        assert by_path[str(raw)].can_use_as_source is True
        assert by_path[str(music)].role == "music"
        assert by_path[str(export)].role == "export"
        assert by_path[str(export)].can_use_as_source is False
    finally:
        for path in [raw, music, export]:
            path.unlink(missing_ok=True)


def test_vault_hides_capture_sidecar_because_it_belongs_to_the_video():
    video = generated_path("data", "downloads", f"capture-{uuid4().hex}.mp4")
    sidecar = video.with_name(f"{video.name}.capture.json")
    video.parent.mkdir(parents=True, exist_ok=True)
    video.write_bytes(b"capture")
    sidecar.write_text("{}", encoding="utf-8")

    try:
        paths = {asset.path for asset in MediaVaultService().list_assets()}
        assert str(video) in paths
        assert str(sidecar) not in paths
    finally:
        video.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)


def test_vault_includes_registered_external_import(tmp_path):
    path = tmp_path / "local-import.mp4"
    path.write_bytes(b"placeholder")
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    item = media.import_path(str(path))

    assets = MediaVaultService(media).list_assets()
    by_path = {asset.path: asset for asset in assets}

    assert by_path[item.path].role == "raw_video"
    assert by_path[item.path].area == "external"
    assert by_path[item.path].can_use_as_source is True


def test_corrupt_export_manifest_is_reported_instead_of_flattening_the_vault(tmp_path):
    export = generated_path("exports", f"vault-blocked-{uuid4().hex[:8]}.mp4")
    export.write_bytes(b"delivery")
    library = tmp_path / "media-library.json"
    generated_manifest = library.with_name(f"{library.stem}-generated-metadata.json")
    generated_manifest.write_text("{broken", encoding="utf-8")
    try:
        media = MediaService(path=library)
        assert media.generated_metadata_problem

        with pytest.raises(GeneratedMetadataPersistenceError, match="损坏"):
            MediaVaultService(media).list_assets()
    finally:
        export.unlink(missing_ok=True)


def test_vault_storage_report_marks_threshold():
    report = MediaVaultService().storage_report()

    assert report.threshold_bytes == 20 * 1024 * 1024 * 1024
    assert {bucket.key for bucket in report.buckets} >= {"downloads", "exports", "previews", "cache", "seedance"}


def test_safe_cleanup_keeps_exports_and_seedance_effects():
    preview = generated_path("previews", "safe-clean-preview.mp4")
    cache = generated_path("cache", "safe-clean-cache.bin")
    effect = generated_path("data", "seedance", "effects", "safe-clean-effect.mp4")
    export = generated_path("exports", "safe-clean-export.mp4")
    for path in [preview, cache, effect, export]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")

    try:
        result = MediaVaultService().safe_cleanup()

        assert result.deleted_count >= 2
        assert preview.exists() is False
        assert cache.exists() is False
        assert effect.exists() is True
        assert export.exists() is True
    finally:
        for path in [preview, cache, effect, export]:
            path.unlink(missing_ok=True)


def test_vault_calendar_groups_by_day():
    path = generated_path("exports", "vault-calendar-test.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")
    try:
        days = MediaVaultService().calendar()
        assert any(any(asset.name == path.name for asset in day.assets) for day in days)
    finally:
        path.unlink(missing_ok=True)


def test_vault_calendar_hides_cache_noise():
    path = generated_path("cache", "vault-cache-noise.bin")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")
    try:
        days = MediaVaultService().calendar()
        assert all(all(asset.role != "cache" for asset in day.assets) for day in days)
    finally:
        path.unlink(missing_ok=True)


def test_exports_are_never_offered_as_import_sources(tmp_path):
    """A rendered file must stay an 导出. If it were ever classed as raw_video it would be
    silently re-rendered by the automatic mode, losing quality each pass."""
    from automated_video_editing_backend.core.paths import GENERATED_DIRS
    from automated_video_editing_backend.services.media import MediaService

    export = GENERATED_DIRS["exports"] / f"test-tuned-output-{uuid4().hex[:8]}.mp4"
    export.write_bytes(b"rendered")
    try:
        media = MediaService(path=tmp_path / "media-library.json")
        item = media.register_generated_path(
            export,
            kind="video",
            metadata={"source": "exports", "role": "export"},
        )

        assert item.metadata["role"] == "export"
        assert item.metadata["role"] != "raw_video"
        # Edit Studio's source list is exactly the raw_video ones.
        raw = [i for i in media.list_items() if i.metadata.get("role") == "raw_video"]
        assert item.id not in {i.id for i in raw}
    finally:
        export.unlink(missing_ok=True)


def test_an_imported_clip_files_under_the_day_it_was_imported(tmp_path):
    """An IMG_*.MOV keeps the mtime of the day it was filmed. Filing by that would hide it
    from today's calendar, which is where the operator just put it."""
    clip = tmp_path / "IMG_2026.MOV"
    clip.write_bytes(b"quicktime")
    # filmed two months ago
    old = (datetime.now(UTC) - timedelta(days=60)).timestamp()
    os.utime(clip, (old, old))

    media = MediaService(path=tmp_path / "media-library.json")
    media.import_path(str(clip))
    vault = MediaVaultService(media)

    asset = next(a for a in vault.list_assets() if a.path == str(clip.resolve()))
    today = datetime.now().astimezone().date().isoformat()

    assert asset.day == today, f"filed under {asset.day}, expected {today}"
    assert today in {day.day for day in vault.calendar()}


def test_the_calendar_day_is_local_not_utc(tmp_path):
    """In +0800 anything before 08:00 local would otherwise land on the previous day."""
    from automated_video_editing_backend.core.paths import GENERATED_DIRS

    export = GENERATED_DIRS["exports"] / "test-local-day.mp4"
    export.write_bytes(b"x")
    try:
        # 02:00 local today — a different UTC date in any timezone east of Greenwich
        local_2am = datetime.now().astimezone().replace(hour=2, minute=0, second=0, microsecond=0)
        os.utime(export, (local_2am.timestamp(), local_2am.timestamp()))

        vault = MediaVaultService(MediaService(path=tmp_path / "media-library.json"))
        asset = next(a for a in vault.list_assets() if a.path == str(export.resolve()))

        assert asset.day == local_2am.date().isoformat()
        utc_day = datetime.fromtimestamp(local_2am.timestamp(), UTC).date().isoformat()
        if utc_day != local_2am.date().isoformat():
            assert asset.day != utc_day, "still computing the calendar day in UTC"
    finally:
        export.unlink(missing_ok=True)


def test_an_export_and_its_master_are_one_entry_not_two_after_restart(tmp_path):
    """A subtitled export is saved twice, and the library must not double in length.

    The delivered cut and its subtitle-free master are two files but one finished video. They
    are paired by a group recorded on the media item rather than by their filenames, because a
    name is something an operator can change and grouping that breaks on rename is grouping
    that will break.
    """
    from automated_video_editing_backend.services.media import MediaService

    stem = f"vault-group-{uuid4().hex[:8]}"
    delivery = generated_path("exports", f"{stem}.mp4")
    master = generated_path("exports", f"{stem} 母版.mp4")
    layer = generated_path("exports", f"{stem}.ass")
    delivery_subtitles = delivery.with_suffix(".subtitles.json")
    master_subtitles = master.with_suffix(".subtitles.json")
    for path in (delivery, master):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
    layer.write_text("[Script Info]\n", encoding="utf-8")
    for path in (delivery_subtitles, master_subtitles):
        path.write_text('{"cues": [{"start": 0, "end": 1, "text": "一"}]}', encoding="utf-8")

    # Given its own library file: constructed without `path=`, it writes into the real data/
    # directory, which is what the guard in test_tests_do_not_write_project_data.py forbids.
    library = tmp_path / "media-library.json"
    media = MediaService(path=library)
    try:
        media.register_generated_path(delivery, kind="video", metadata={
            "source": "exports", "role": "export",
            "export_group": "export:test-1", "variant": "subtitled",
            "variant_label": "成片（带字幕）",
            "subtitles_path": str(delivery_subtitles),
            "has_burned_subtitles": True,
            "has_voiceover": True,
        })
        media.register_generated_path(master, kind="video", metadata={
            "source": "exports", "role": "export",
            "export_group": "export:test-1", "variant": "master",
            "variant_label": "母版（无字幕）",
            "subtitles_path": str(master_subtitles),
            "has_burned_subtitles": False,
            "has_voiceover": True,
        })

        # Reconstructing the service is the same boundary as restarting the backend. Generated
        # ids may change, but the path-keyed render metadata must still join the two files.
        reopened = MediaService(path=library)
        assets = MediaVaultService(reopened).list_assets()
        by_path = {asset.path: asset for asset in assets}

        # Both carry the same group, so the library can fold them into one row.
        assert by_path[str(delivery)].export_group == "export:test-1"
        assert by_path[str(master)].export_group == "export:test-1"
        assert by_path[str(delivery)].variant_label == "成片（带字幕）"
        assert by_path[str(master)].variant == "master"

        reopened_items = {item.path: item for item in reopened.list_items()}
        assert reopened_items[str(delivery)].metadata["subtitles_path"] == str(
            delivery_subtitles
        )
        assert reopened_items[str(master)].metadata["subtitles_path"] == str(master_subtitles)
        assert reopened_items[str(delivery)].metadata["has_burned_subtitles"] is True
        assert reopened_items[str(master)].metadata["has_burned_subtitles"] is False
        assert reopened_items[str(delivery)].metadata["has_voiceover"] is True
        assert reopened_items[str(master)].metadata["has_voiceover"] is True

        # Exports stay available to 手动微调 through role=export, but must never leak into
        # the raw-video working pool and be automatically re-encoded as source footage.
        pool = reopened.media_pool()
        media_ids_by_path = {path: item.id for path, item in reopened_items.items()}
        assert media_ids_by_path[str(delivery)] not in pool["source_media_ids"]
        assert media_ids_by_path[str(master)] not in pool["source_media_ids"]

        # The cue file is bookkeeping, not an asset — one per video would bury the videos.
        assert str(layer) not in by_path
    finally:
        for path in (delivery, master, layer, delivery_subtitles, master_subtitles):
            path.unlink(missing_ok=True)


def test_unannotated_export_names_are_never_guessed_into_a_group(tmp_path):
    """Even an exact-looking legacy pair stays flat unless a render explicitly grouped it."""
    stem = f"vault-plain-{uuid4().hex[:8]}"
    delivery = generated_path("exports", f"{stem}.mp4")
    master = generated_path("exports", f"{stem} 母版.mp4")
    similar = generated_path("exports", f"{stem} 母版 副本.mp4")
    for path in (delivery, master, similar):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")

    try:
        media = MediaService(path=tmp_path / "media-library.json")
        assets = MediaVaultService(media).list_assets()
        by_path = {asset.path: asset for asset in assets}

        assert by_path[str(delivery)].export_group == ""
        assert by_path[str(master)].export_group == ""
        assert by_path[str(similar)].export_group == ""
    finally:
        for path in (delivery, master, similar):
            path.unlink(missing_ok=True)


def test_deleted_export_metadata_is_pruned_on_refresh(tmp_path):
    stem = f"vault-delete-{uuid4().hex[:8]}"
    delivery = generated_path("exports", f"{stem}.mp4")
    master = generated_path("exports", f"{stem} 母版.mp4")
    for path in (delivery, master):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")

    library = tmp_path / "media-library.json"
    media = MediaService(path=library)
    group = f"export:{uuid4()}"
    try:
        media.register_generated_path(delivery, kind="video", metadata={
            "source": "exports", "role": "export", "export_group": group,
            "variant": "subtitled", "variant_label": "成片（带字幕）",
        })
        media.register_generated_path(master, kind="video", metadata={
            "source": "exports", "role": "export", "export_group": group,
            "variant": "master", "variant_label": "母版（无字幕）",
        })

        master.unlink()
        media.list_items()

        saved = json.loads(
            media.generated_metadata_path.read_text(encoding="utf-8")
        )["items"]
        assert str(delivery) in saved
        assert str(master) not in saved
    finally:
        delivery.unlink(missing_ok=True)
        master.unlink(missing_ok=True)


def test_an_ordinary_export_carries_no_group():
    """Grouping only exists where there is really a pair; everything else stays a plain row."""
    export = generated_path("exports", "vault-ungrouped-test.mp4")
    export.parent.mkdir(parents=True, exist_ok=True)
    export.write_bytes(b"placeholder")
    try:
        assets = MediaVaultService().list_assets()
        asset = next(item for item in assets if item.path == str(export))
        assert asset.export_group == ""
    finally:
        export.unlink(missing_ok=True)
