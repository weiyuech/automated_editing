import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest

from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services import media as media_module
from automated_video_editing_backend.services.media import (
    MEDIA_POOL_FIELDS,
    GeneratedMetadataPersistenceError,
    MediaLibraryPersistenceError,
    MediaPoolPersistenceError,
    MediaService,
    _is_supported_download,
    _safe_download_name,
)


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


def test_media_pool_is_separate_from_library_and_survives_restart(tmp_path):
    library = tmp_path / "media-library.json"
    first_clip = tmp_path / "first.mp4"
    second_clip = tmp_path / "second.mp4"
    song = tmp_path / "bed.mp3"
    for path in (first_clip, second_clip, song):
        path.write_bytes(b"x")

    media = MediaService(path=library)
    first = media.import_path(str(first_clip))
    second = media.import_path(str(second_clip))
    music = media.import_path(str(song))

    # The compatibility read seeds the old all-library working set once.
    assert set(media.media_pool()["source_media_ids"]) >= {first.id, second.id}
    assert media.pool_path.is_file()
    assert not media.pool_path.with_name(f"{media.pool_path.name}.tmp").exists()
    state = media.update_media_pool(
        {
            "source_media_ids": [second.id],
            "music_media_ids": [music.id],
            "voiceover_media_ids": [],
            "effect_media_ids": [],
        }
    )
    assert state["source_media_ids"] == [second.id]
    assert first_clip.exists()  # Removing from the pool never touches the library or file.

    reopened = MediaService(path=library)
    reopened_by_path = {item.path: item.id for item in reopened.list_items()}
    restored = reopened.media_pool()
    assert restored["source_media_ids"] == [reopened_by_path[str(second_clip)]]
    assert restored["music_media_ids"] == [reopened_by_path[str(song)]]

    renamed_clip = tmp_path / "renamed.mp4"
    second_clip.rename(renamed_clip)
    restored_id = reopened_by_path[str(second_clip)]
    reopened.repoint(restored_id, str(renamed_clip))
    assert reopened.media_pool()["source_media_ids"] == [restored_id]


def test_damaged_media_pool_is_reported_and_never_auto_seeded(tmp_path):
    library = tmp_path / "media-library.json"
    clip = tmp_path / "kept.mp4"
    clip.write_bytes(b"x")
    original = MediaService(path=library)
    original.import_path(str(clip))
    original.pool_path.write_text('{"source_media_ids": [', encoding="utf-8")

    damaged = MediaService(path=library)

    assert "损坏" in damaged.media_pool_problem
    with pytest.raises(MediaPoolPersistenceError, match="损坏"):
        damaged.media_pool()
    kept = next(item for item in damaged.list_items() if item.path == str(clip.resolve()))
    with pytest.raises(MediaPoolPersistenceError, match="损坏"):
        damaged.update_media_pool({field: [] for field in MEDIA_POOL_FIELDS})
    with pytest.raises(MediaPoolPersistenceError, match="损坏"):
        damaged.repoint(kept.id, str(tmp_path / "renamed.mp4"))
    with pytest.raises(MediaPoolPersistenceError, match="损坏"):
        damaged.forget(kept.id)
    assert damaged.get(kept.id) is not None
    assert damaged.path.exists()
    # read_json preserves the bad bytes under a quarantine name. Most importantly, the
    # compatibility read must not replace them with an all-library pool.
    assert not damaged.pool_path.exists()
    quarantined = list(tmp_path.glob(f"{damaged.pool_path.name}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == '{"source_media_ids": ['

    # The quarantine still distinguishes this from a fresh install on the next process start.
    restarted = MediaService(path=library)
    with pytest.raises(MediaPoolPersistenceError, match="之前损坏"):
        restarted.media_pool()
    assert not restarted.pool_path.exists()


@pytest.mark.parametrize("invalid", ['{"source_media_ids": "not-a-list"}', "null"])
def test_invalid_media_pool_shape_is_reported_without_replacing_file(tmp_path, invalid):
    library = tmp_path / "media-library.json"
    pool_path = library.with_name(f"{library.stem}-pool.json")
    pool_path.write_text(invalid, encoding="utf-8")

    media = MediaService(path=library)

    assert "格式无效" in media.media_pool_problem
    with pytest.raises(MediaPoolPersistenceError, match="格式无效"):
        media.media_pool()
    assert pool_path.read_text(encoding="utf-8") == invalid


def test_first_upgrade_seed_reports_atomic_save_failure(tmp_path, monkeypatch):
    media = MediaService(path=tmp_path / "media-library.json")
    clip = tmp_path / "first.mp4"
    clip.write_bytes(b"x")
    media.import_path(str(clip))

    real_write_json = media_module.write_json

    def fail_pool_write(path, payload):
        if path == media.pool_path:
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(media_module, "write_json", fail_pool_write)

    with pytest.raises(MediaPoolPersistenceError, match="无法保存"):
        media.media_pool()
    assert not media.pool_path.exists()
    assert media._pool_initialized is False


def test_explicit_pool_update_fails_transactionally_when_save_fails(tmp_path, monkeypatch):
    media = MediaService(path=tmp_path / "media-library.json")
    first_path = tmp_path / "first.mp4"
    second_path = tmp_path / "second.mp4"
    first_path.write_bytes(b"x")
    second_path.write_bytes(b"x")
    first = media.import_path(str(first_path))
    second = media.import_path(str(second_path))
    before = media.media_pool()
    before_bytes = media.pool_path.read_bytes()
    assert set(before["source_media_ids"]) >= {first.id, second.id}

    real_write_json = media_module.write_json

    def fail_pool_write(path, payload):
        if path == media.pool_path:
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(media_module, "write_json", fail_pool_write)
    requested = {field: [] for field in MEDIA_POOL_FIELDS}
    requested["source_media_ids"] = [first.id]

    with pytest.raises(MediaPoolPersistenceError, match="没有更新"):
        media.update_media_pool(requested)

    assert media.pool_path.read_bytes() == before_bytes
    # A failed PUT must retain the last durable set in memory as well as on disk.
    assert set(media.media_pool()["source_media_ids"]) >= {first.id, second.id}
    assert "无法保存" in media.media_pool_problem


def test_export_registration_fails_when_group_metadata_cannot_be_saved(
    tmp_path,
    monkeypatch,
):
    media = MediaService(path=tmp_path / "media-library.json")
    output = generated_path("exports", f"metadata-save-failure-{uuid4().hex}.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"rendered")
    output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
    real_write_json = media_module.write_json

    def fail_generated_metadata_write(path, payload):
        if path == media.generated_metadata_path:
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(media_module, "write_json", fail_generated_metadata_write)
    try:
        with pytest.raises(GeneratedMetadataPersistenceError, match="无法保存"):
            media.register_generated_path(
                output,
                kind="video",
                metadata={
                    "source": "exports",
                    "role": "export",
                    "export_group": "export:must-be-durable",
                    "variant": "subtitled",
                    "subtitles_path": str(output.with_suffix(".subtitles.json")),
                },
            )

        assert not media.generated_metadata_path.exists()
        assert "无法保存" in media.generated_metadata_problem
        # A video without durable metadata is not a completed export. It remains visible to
        # storage cleanup, but never becomes selectable media in this process or after restart.
        assert all(item.path != str(output) for item in media.list_items())
    finally:
        output.unlink(missing_ok=True)
        output.with_suffix(".subtitles.json").unlink(missing_ok=True)


def test_damaged_generated_metadata_blocks_flattening_and_later_registration(tmp_path):
    library = tmp_path / "media-library.json"
    manifest = library.with_name(f"{library.stem}-generated-metadata.json")
    manifest.write_text('{"version": 1, "items": {', encoding="utf-8")
    output = generated_path("exports", f"blocked-metadata-{uuid4().hex}.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"rendered")

    try:
        media = MediaService(path=library)

        assert "损坏" in media.generated_metadata_problem
        # Showing this as a plain export would silently flatten its 成片/母版 relationship.
        assert all(item.path != str(output) for item in media.list_items())
        with pytest.raises(GeneratedMetadataPersistenceError, match="损坏"):
            media.register_generated_path(
                output,
                kind="video",
                metadata={
                    "source": "exports",
                    "role": "export",
                    "export_group": "export:new-job",
                },
            )

        assert not manifest.exists()
        quarantined = list(tmp_path.glob(f"{manifest.name}.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == '{"version": 1, "items": {'

        restarted = MediaService(path=library)
        assert "之前损坏" in restarted.generated_metadata_problem
        assert all(item.path != str(output) for item in restarted.list_items())
        assert not manifest.exists()
    finally:
        output.unlink(missing_ok=True)


def test_invalid_generated_metadata_is_preserved_and_blocks_registration(tmp_path):
    library = tmp_path / "media-library.json"
    manifest = library.with_name(f"{library.stem}-generated-metadata.json")
    invalid = '{"version": 1, "items": []}'
    manifest.write_text(invalid, encoding="utf-8")
    output = generated_path("exports", f"invalid-metadata-{uuid4().hex}.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"rendered")

    try:
        media = MediaService(path=library)

        assert "格式无效" in media.generated_metadata_problem
        with pytest.raises(GeneratedMetadataPersistenceError, match="格式无效"):
            media.register_generated_path(
                output,
                kind="video",
                metadata={"source": "exports", "role": "export"},
            )
        assert manifest.read_text(encoding="utf-8") == invalid
        assert all(item.path != str(output) for item in media.list_items())
    finally:
        output.unlink(missing_ok=True)


def test_manifest_with_another_exports_sidecar_is_rejected_fail_closed(tmp_path):
    library = tmp_path / "media-library.json"
    manifest = library.with_name(f"{library.stem}-generated-metadata.json")
    output = generated_path("exports", f"manifest-owner-{uuid4().hex}.mp4")
    other = generated_path("exports", f"manifest-other-{uuid4().hex}.mp4")
    output.write_bytes(b"rendered")
    payload = {
        "version": 1,
        "items": {
            str(output): {
                "source": "exports",
                "role": "export",
                "subtitles_path": str(other.with_suffix(".subtitles.json")),
            },
        },
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    try:
        media = MediaService(path=library)

        assert "subtitles_path" in media.generated_metadata_problem
        assert all(item.path != str(output) for item in media.list_items())
        assert json.loads(manifest.read_text(encoding="utf-8")) == payload
    finally:
        output.unlink(missing_ok=True)


def test_export_repoint_is_transactional_when_manifest_save_fails(tmp_path, monkeypatch):
    media = MediaService(path=tmp_path / "media-library.json")
    source = generated_path("exports", f"repoint-source-{uuid4().hex}.mp4")
    target = source.with_name(f"repoint-target-{uuid4().hex}.mp4")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"rendered")

    try:
        item = media.register_generated_path(
            source,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "export_group": "export:repoint",
                "variant": "subtitled",
            },
        )
        before_item = item.model_dump(mode="json")
        before_manifest = json.loads(media.generated_metadata_path.read_text(encoding="utf-8"))
        before_bytes = media.generated_metadata_path.read_bytes()
        real_write_json = media_module.write_json

        def fail_generated_metadata_write(path, payload):
            if path == media.generated_metadata_path:
                return False
            return real_write_json(path, payload)

        monkeypatch.setattr(media_module, "write_json", fail_generated_metadata_write)

        with pytest.raises(GeneratedMetadataPersistenceError, match="没有更新"):
            media.repoint(item.id, str(target))

        assert item.model_dump(mode="json") == before_item
        assert media._generated_metadata == before_manifest["items"]
        assert media.generated_metadata_path.read_bytes() == before_bytes
    finally:
        source.unlink(missing_ok=True)
        target.unlink(missing_ok=True)


def test_unregistered_file_inside_exports_is_hidden_and_never_pooled(tmp_path):
    library = tmp_path / "media-library.json"
    output = generated_path("exports", f"poisoned-import-{uuid4().hex}.mp4")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"rendered")
    library.write_text(
        json.dumps(
            [
                {
                    "id": "poisoned-raw-id",
                    "path": str(output),
                    "kind": "video",
                    "metadata": {"source": "local_import", "role": "raw_video"},
                }
            ]
        ),
        encoding="utf-8",
    )

    try:
        media = MediaService(path=library)
        assert all(item.path != str(output) for item in media.list_items())
        assert "poisoned-raw-id" not in media.media_pool()["source_media_ids"]
        with pytest.raises(ValueError, match="只能用于手动微调"):
            media.import_path(str(output))

        restarted = MediaService(path=library)
        assert all(item.path != str(output) for item in restarted.list_items())
        assert all(
            restarted.get(media_id).path != str(output)
            for media_id in restarted.media_pool()["source_media_ids"]
        )
    finally:
        output.unlink(missing_ok=True)


def test_export_group_appears_only_after_atomic_registration_and_survives_restart(tmp_path):
    library = tmp_path / "media-library.json"
    media = MediaService(path=library)
    delivery = generated_path("exports", f"publishing-{uuid4().hex}.mp4")
    master = delivery.with_name(f"{delivery.stem} 母版.mp4")
    delivery_sidecar = delivery.with_suffix(".subtitles.json")
    master_sidecar = master.with_suffix(".subtitles.json")
    family = (delivery, master, delivery_sidecar, master_sidecar)
    try:
        delivery.write_bytes(b"ffmpeg is still writing")
        delivery_sidecar.write_text("{}", encoding="utf-8")

        assert all(item.path != str(delivery) for item in media.list_items())
        assert all(
            item.path != str(delivery)
            for item in MediaService(path=library).list_items()
        )

        master.write_bytes(b"clean master complete")
        master_sidecar.write_text("{}", encoding="utf-8")
        group = f"export:{uuid4()}"
        media.register_generated_paths([
            (delivery, "video", {
                "source": "exports",
                "role": "export",
                "export_group": group,
                "variant": "subtitled",
                "subtitles_path": str(delivery_sidecar),
            }),
            (master, "video", {
                "source": "exports",
                "role": "export",
                "export_group": group,
                "variant": "master",
                "subtitles_path": str(master_sidecar),
            }),
        ])

        assert {item.path for item in media.list_items()} >= {str(delivery), str(master)}
        restarted = MediaService(path=library)
        restored = {
            item.path: item for item in restarted.list_items()
            if item.path in {str(delivery), str(master)}
        }
        assert set(restored) == {str(delivery), str(master)}
        assert {item.metadata["export_group"] for item in restored.values()} == {group}
    finally:
        for path in family:
            path.unlink(missing_ok=True)


def test_export_registration_rejects_an_escape_and_another_videos_sidecar(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    escaped = generated_path("data", "downloads", f"escaped-export-{uuid4().hex}.mp4")
    first = generated_path("exports", f"first-export-{uuid4().hex}.mp4")
    second = generated_path("exports", f"second-export-{uuid4().hex}.mp4")
    wrong_sidecar = second.with_suffix(".subtitles.json")
    first.write_bytes(b"rendered")

    with pytest.raises(ValueError, match="managed exports"):
        media.register_generated_path(
            escaped,
            kind="video",
            metadata={"source": "exports", "role": "export"},
        )
    try:
        with pytest.raises(ValueError, match="canonical sidecar"):
            media.register_generated_path(
                first,
                kind="video",
                metadata={
                    "source": "exports",
                    "role": "export",
                    "subtitles_path": str(wrong_sidecar),
                },
            )

        assert not media.generated_metadata_path.exists()
    finally:
        first.unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("area", "parts"),
    [
        ("data", ("seedance", "effects", "old-effect.mp4")),
        ("data", ("tts", "old-generated-video.mp4")),
        ("previews", ("old-preview.mp4",)),
        ("cache", ("old-cache-video.mp4",)),
    ],
)
def test_poisoned_raw_role_in_managed_non_source_area_is_never_pooled_after_restart(
    tmp_path, area, parts
):
    """Old metadata cannot promote generated app state into automatic input footage."""
    library = tmp_path / "media-library.json"
    output = generated_path(area, *parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"generated")
    library.write_text(
        json.dumps(
            [
                {
                    "id": "poisoned-generated-id",
                    "path": str(output),
                    "kind": "video",
                    "metadata": {"source": "local_import", "role": "raw_video"},
                }
            ]
        ),
        encoding="utf-8",
    )

    try:
        media = MediaService(path=library)
        matches = [item for item in media.list_items() if item.path == str(output)]

        assert all(media.is_automatic_source(item) is False for item in matches)
        assert "poisoned-generated-id" not in {item.id for item in matches}
        assert all(
            item.id not in media.media_pool()["source_media_ids"] for item in matches
        )

        reopened = MediaService(path=library)
        reopened_matches = [
            item for item in reopened.list_items() if item.path == str(output)
        ]
        assert all(
            reopened.is_automatic_source(item) is False for item in reopened_matches
        )
        assert "poisoned-generated-id" not in {
            item.id for item in reopened_matches
        }
        assert all(
            item.id not in reopened.media_pool()["source_media_ids"]
            for item in reopened_matches
        )
    finally:
        output.unlink(missing_ok=True)


def test_data_download_video_remains_an_automatic_source_after_restart(tmp_path):
    library = tmp_path / "media-library.json"
    capture = generated_path("data", "downloads", f"capture-{uuid4().hex}.mp4")
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_bytes(b"capture")

    try:
        media = MediaService(path=library)
        source = next(item for item in media.list_items() if item.path == str(capture))
        assert source.metadata["source"] == "data/downloads"
        assert media.is_automatic_source(source) is True
        assert source.id in media.media_pool()["source_media_ids"]

        reopened = MediaService(path=library)
        reopened_source = next(
            item for item in reopened.list_items() if item.path == str(capture)
        )
        assert reopened.is_automatic_source(reopened_source) is True
        assert reopened_source.id in reopened.media_pool()["source_media_ids"]
    finally:
        capture.unlink(missing_ok=True)


def test_local_import_inside_app_root_but_outside_managed_roots_remains_source(
    tmp_path, monkeypatch
):
    app_root = tmp_path / "app-root"
    managed = {
        "data": app_root / "data",
        "cache": app_root / ".cache",
        "logs": app_root / "logs",
        "exports": app_root / "exports",
        "previews": app_root / "previews",
    }
    monkeypatch.setattr(media_module, "GENERATED_DIRS", managed)
    footage = app_root / "project-footage" / "camera-original.mp4"
    footage.parent.mkdir(parents=True, exist_ok=True)
    footage.write_bytes(b"source")
    library = tmp_path / "media-library.json"

    media = MediaService(path=library)
    source = media.import_path(str(footage))

    assert source.metadata == {"source": "local_import", "role": "raw_video"}
    assert media.is_automatic_source(source) is True
    assert source.id in media.media_pool()["source_media_ids"]

    reopened = MediaService(path=library)
    reopened_source = next(
        item for item in reopened.list_items() if item.path == str(footage)
    )
    assert reopened.is_automatic_source(reopened_source) is True
    assert reopened_source.id in reopened.media_pool()["source_media_ids"]


def test_damaged_import_library_cannot_prune_a_valid_curated_pool(tmp_path):
    library = tmp_path / "media-library.json"
    pool = library.with_name(f"{library.stem}-pool.json")
    clip = tmp_path / "external.mp4"
    clip.write_bytes(b"source")
    broken_bytes = b'[{"id": "unfinished"'
    library.write_bytes(broken_bytes)
    pool_payload = {field: [] for field in MEDIA_POOL_FIELDS}
    pool_payload["source_media_ids"] = [str(clip)]
    pool.write_text(json.dumps(pool_payload), encoding="utf-8")
    pool_bytes = pool.read_bytes()

    media = MediaService(path=library)

    assert "损坏" in media.media_library_problem
    with pytest.raises(MediaPoolPersistenceError, match="保护已选素材"):
        media.media_pool()
    assert pool.read_bytes() == pool_bytes
    assert not library.exists()
    quarantined = list(tmp_path.glob(f"{library.name}.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == broken_bytes

    # Neither a later request nor a restart may overwrite the preserved library or use its
    # apparent emptiness as permission to delete the external selection from the pool.
    with pytest.raises(MediaLibraryPersistenceError, match="损坏"):
        media.import_path(str(clip))
    restarted = MediaService(path=library)
    with pytest.raises(MediaPoolPersistenceError, match="保护已选素材"):
        restarted.media_pool()
    assert pool.read_bytes() == pool_bytes


def test_import_write_failure_does_not_create_an_in_memory_only_asset(tmp_path, monkeypatch):
    media = MediaService(path=tmp_path / "media-library.json")
    clip = tmp_path / "external.mp4"
    clip.write_bytes(b"source")
    real_write_json = media_module.write_json

    def fail_import_library_write(path, payload):
        if path == media.path:
            return False
        return real_write_json(path, payload)

    monkeypatch.setattr(media_module, "write_json", fail_import_library_write)

    with pytest.raises(MediaLibraryPersistenceError, match="没有更新"):
        media.import_path(str(clip))
    assert all(item.path != str(clip) for item in media.list_items())
    assert not media.path.exists()


def test_media_pool_rejects_an_item_in_the_wrong_category(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    song = tmp_path / "bed.mp3"
    song.write_bytes(b"x")
    music = media.import_path(str(song))

    try:
        media.update_media_pool({"source_media_ids": [music.id]})
    except ValueError as exc:
        assert "Invalid media type" in str(exc)
    else:
        raise AssertionError("music must not be accepted into the source-video pool")


def test_a_recording_says_how_many_cruise_points_it_carries(tmp_path):
    """Most editing choices divide up the points a cruise recorded, and the panel offering
    them has no other way to know whether the chosen footage has any. Computed on listing
    rather than at import, because a cruise writes its spans after the file already exists.
    """
    plain = tmp_path / "plain.mp4"
    plain.write_bytes(b"x")
    cruise = tmp_path / "cruise.mp4"
    cruise.write_bytes(b"x")
    cruise.with_name("cruise.mp4.capture.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "index": index,
                        "path_name": "path1",
                        "goal_id": index + 1,
                        "status": "arrived",
                    }
                    for index in range(6)
                ]
            }
        ),
        encoding="utf-8",
    )

    media = MediaService(path=tmp_path / "lib.json")
    media.import_path(str(plain))
    media.import_path(str(cruise))
    points = {
        Path(item.path).name: item.metadata.get("cruise_points") for item in media.list_items()
    }

    assert points["cruise.mp4"] == 6
    assert points["plain.mp4"] == 0
