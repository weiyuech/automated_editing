import tempfile
from pathlib import Path
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.media import MediaService
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

    export = GENERATED_DIRS["exports"] / "test-tuned-output.mp4"
    export.write_bytes(b"rendered")
    try:
        media = MediaService(path=tmp_path / "media-library.json")
        item = next(i for i in media.list_items() if i.path == str(export.resolve()))

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
    import os
    from datetime import datetime, timedelta, timezone as tz
    from automated_video_editing_backend.services.media import MediaService
    from automated_video_editing_backend.services.media_vault import MediaVaultService

    clip = tmp_path / "IMG_2026.MOV"
    clip.write_bytes(b"quicktime")
    # filmed two months ago
    old = (datetime.now(tz.utc) - timedelta(days=60)).timestamp()
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
    import os
    from datetime import datetime, timezone as tz
    from automated_video_editing_backend.core.paths import GENERATED_DIRS
    from automated_video_editing_backend.services.media import MediaService
    from automated_video_editing_backend.services.media_vault import MediaVaultService

    export = GENERATED_DIRS["exports"] / "test-local-day.mp4"
    export.write_bytes(b"x")
    try:
        # 02:00 local today — a different UTC date in any timezone east of Greenwich
        local_2am = datetime.now().astimezone().replace(hour=2, minute=0, second=0, microsecond=0)
        os.utime(export, (local_2am.timestamp(), local_2am.timestamp()))

        vault = MediaVaultService(MediaService(path=tmp_path / "media-library.json"))
        asset = next(a for a in vault.list_assets() if a.path == str(export.resolve()))

        assert asset.day == local_2am.date().isoformat()
        utc_day = datetime.fromtimestamp(local_2am.timestamp(), tz.utc).date().isoformat()
        if utc_day != local_2am.date().isoformat():
            assert asset.day != utc_day, "still computing the calendar day in UTC"
    finally:
        export.unlink(missing_ok=True)


def test_an_export_and_its_master_are_one_entry_not_two():
    """A subtitled export is saved twice, and the library must not double in length.

    The delivered cut and its subtitle-free master are two files but one finished video. They
    are paired by a group recorded on the media item rather than by their filenames, because a
    name is something an operator can change and grouping that breaks on rename is grouping
    that will break.
    """
    from automated_video_editing_backend.services.media import MediaService

    delivery = generated_path("exports", "vault-group-test.mp4")
    master = generated_path("exports", "vault-group-test 母版.mp4")
    layer = generated_path("exports", "vault-group-test.ass")
    for path in (delivery, master):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"placeholder")
    layer.write_text("[Script Info]\n", encoding="utf-8")

    # Given its own library file: constructed without `path=`, it writes into the real data/
    # directory, which is what the guard in test_tests_do_not_write_project_data.py forbids.
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    try:
        media.register_generated_path(delivery, kind="video", metadata={
            "source": "exports", "role": "export",
            "export_group": "export:test-1", "variant": "subtitled",
            "variant_label": "成片（带字幕）"})
        media.register_generated_path(master, kind="video", metadata={
            "source": "exports", "role": "export",
            "export_group": "export:test-1", "variant": "master",
            "variant_label": "母版（无字幕）"})

        assets = MediaVaultService(media).list_assets()
        by_path = {asset.path: asset for asset in assets}

        # Both carry the same group, so the library can fold them into one row.
        assert by_path[str(delivery)].export_group == "export:test-1"
        assert by_path[str(master)].export_group == "export:test-1"
        assert by_path[str(delivery)].variant_label == "成片（带字幕）"
        assert by_path[str(master)].variant == "master"

        # The cue file is bookkeeping, not an asset — one per video would bury the videos.
        assert str(layer) not in by_path
    finally:
        for path in (delivery, master, layer):
            path.unlink(missing_ok=True)


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
