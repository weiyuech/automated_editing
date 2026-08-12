import tempfile
from pathlib import Path
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.media import MediaService, _is_supported_download, _safe_download_name


def test_direct_media_download_detection():
    assert _is_supported_download("https://example.com/clip.mp4", "text/html") is True
    assert _is_supported_download("https://example.com/file", "video/mp4") is True
    assert _is_supported_download("https://example.com/page", "text/html") is False


def test_download_name_uses_media_content_type():
    name = _safe_download_name("https://example.com/no-extension", "audio/mpeg")
    assert name.startswith("no-extension-")
    assert name.endswith(".mp3")


def test_download_name_uses_image_content_type():
    name = _safe_download_name("https://robot.local/capture", "image/png")
    assert name.startswith("capture-")
    assert name.endswith(".png")


def test_media_service_loads_existing_downloads():
    path = generated_path("data", "downloads", "existing-test-clip.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"placeholder")
    try:
        items = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json").list_items()
        assert any(item.path == str(path) and item.kind == "video" for item in items)
    finally:
        path.unlink(missing_ok=True)


def test_importing_the_same_file_twice_does_not_duplicate_it(tmp_path):
    """The vault keys assets by path and showed one row; Edit Studio lists by id and showed
    two, so the same clip appeared selectable twice."""
    from automated_video_editing_backend.services.media import MediaService

    clip = tmp_path / "IMG_2026.MOV"
    clip.write_bytes(b"quicktime")

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    before = len(media.list_items())

    first = media.import_path(str(clip))
    second = media.import_path(str(clip))

    assert first.id == second.id
    assert len([item for item in media.list_items() if item.path == str(clip)]) == 1
    assert len(media.list_items()) == before + 1


def test_uppercase_extensions_are_recognised(tmp_path):
    from automated_video_editing_backend.services.media import MediaService

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    for name, expected in [("IMG_2026.MOV", "video"), ("clip.MP4", "video"), ("song.MP3", "audio")]:
        path = tmp_path / name
        path.write_bytes(b"x")
        item = media.import_path(str(path))
        assert item.kind == expected, name
        assert item.metadata["role"] in {"raw_video", "music"}


def test_imported_clips_survive_a_restart(tmp_path):
    """Imported footage lives outside the app folder, so nothing rediscovers it on startup.
    Without persistence the library forgot every imported clip on every launch."""
    from automated_video_editing_backend.services.media import MediaService

    library = tmp_path / "media-library.json"
    clip = tmp_path / "IMG_2026.MOV"
    clip.write_bytes(b"quicktime")

    first = MediaService(path=library)
    imported = first.import_path(str(clip))

    reopened = MediaService(path=library)
    restored = [item for item in reopened.list_items() if item.path == str(clip)]

    assert len(restored) == 1
    assert restored[0].id == imported.id
    assert restored[0].kind == "video"


def test_a_clip_deleted_from_disk_is_dropped_on_restart(tmp_path):
    from automated_video_editing_backend.services.media import MediaService

    library = tmp_path / "media-library.json"
    clip = tmp_path / "gone.mp4"
    clip.write_bytes(b"x")

    MediaService(path=library).import_path(str(clip))
    clip.unlink()

    reopened = MediaService(path=library)
    # Better an empty library than a row that cannot be played.
    assert [item for item in reopened.list_items() if item.path == str(clip)] == []


def test_forgetting_an_import_removes_it_from_the_library_but_keeps_the_file(tmp_path):
    from automated_video_editing_backend.services.media import MediaService

    library = tmp_path / "media-library.json"
    clip = tmp_path / "IMG_2026.MOV"
    clip.write_bytes(b"quicktime")

    media = MediaService(path=library)
    item = media.import_path(str(clip))

    forgotten = media.forget(item.id)

    assert forgotten is not None
    assert [i for i in media.list_items() if i.path == str(clip)] == []
    # The operator's own file must survive: the app may forget it, never delete it.
    assert clip.exists()
    assert clip.read_bytes() == b"quicktime"
    # And it must stay forgotten across a restart.
    assert [i for i in MediaService(path=library).list_items() if i.path == str(clip)] == []


def test_generated_media_cannot_be_forgotten(tmp_path):
    """Forgetting a generated file would be pointless: the folder scan brings it back."""
    from automated_video_editing_backend.services.media import MediaService

    media = MediaService(path=tmp_path / "media-library.json")
    generated = [i for i in media.list_items() if i.metadata.get("source") != "local_import"]
    if not generated:
        return
    assert media.forget(generated[0].id) is None


def test_a_recording_says_how_many_cruise_points_it_carries(tmp_path):
    """Most editing choices divide up the points a cruise recorded, and the panel offering
    them has no other way to know whether the chosen footage has any. Computed on listing
    rather than at import, because a cruise writes its spans after the file already exists.
    """
    import json

    plain = tmp_path / "plain.mp4"
    plain.write_bytes(b"x")
    cruise = tmp_path / "cruise.mp4"
    cruise.write_bytes(b"x")
    cruise.with_name("cruise.mp4.capture.json").write_text(
        json.dumps({"segments": [
            {"index": index, "path_name": "path1", "goal_id": index + 1, "status": "arrived"}
            for index in range(6)
        ]}),
        encoding="utf-8",
    )

    media = MediaService(path=tmp_path / "lib.json")
    media.import_path(str(plain))
    media.import_path(str(cruise))
    points = {Path(item.path).name: item.metadata.get("cruise_points") for item in media.list_items()}

    assert points["cruise.mp4"] == 6
    assert points["plain.mp4"] == 0
