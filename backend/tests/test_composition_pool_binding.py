"""Current composition/voice pairs survive pool updates and scanner-ID changes."""

import json
from pathlib import Path

import pytest

from automated_video_editing_backend.core import paths
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.composition_assets import save_manifest


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APP_ROOT", tmp_path)
    for area in paths.GENERATED_DIRS:
        folder = tmp_path / area
        folder.mkdir()
        monkeypatch.setitem(paths.GENERATED_DIRS, area, folder)
    media = MediaService(path=tmp_path / "data/library.json")
    media.update_media_pool({})
    source = tmp_path / "combined.mp4"
    source.write_bytes(b"video")
    save_manifest(source, {"composition_id": "combo", "narration_binding_id": "active"})
    media.import_path(str(source))
    folder = tmp_path / "data/tts"
    folder.mkdir(exist_ok=True)
    for name in ("active", "historical", "pending"):
        voice = folder / f"{name}.wav"
        voice.write_bytes(b"audio")
        voice.with_suffix(".json").write_text(
            json.dumps(
                {
                    "binding_id": name,
                    "composition_id": "combo",
                    "narration_status": "pending_review" if name == "pending" else "ready",
                }
            )
        )
    media.list_items()
    return media, source


def assets(media):
    items = media.list_items()
    source = next(i for i in items if i.kind == "video")
    voices = {i.metadata["binding_id"]: i for i in items if i.kind == "audio"}
    return source, voices


def test_add_either_remove_either_and_preserve_files(pair):
    media, source_path = pair
    source, voices = assets(media)
    voice = voices["active"]
    for entry in ({"source_media_ids": [source.id]}, {"voiceover_media_ids": [voice.id]}):
        for removed in ("source_media_ids", "voiceover_media_ids"):
            result = media.update_media_pool(entry)
            assert result["source_media_ids"] == [source.id]
            assert result["voiceover_media_ids"] == [voice.id]
            result[removed] = []
            cleared = media.update_media_pool(result)
            assert cleared["source_media_ids"] == cleared["voiceover_media_ids"] == []
            assert source_path.exists() and Path(voice.path).exists()


def test_historical_and_pending_cannot_enter_pool_or_substitute_current_voice(pair):
    media, _ = pair
    source, voices = assets(media)
    for name in ("historical", "pending"):
        with pytest.raises(ValueError, match="尚未绑定或已被替换"):
            media.update_media_pool(
                {"source_media_ids": [source.id], "voiceover_media_ids": [voices[name].id]}
            )
        assert media.media_pool()["source_media_ids"] == []


def test_binding_and_pool_survive_restart_and_media_rename(pair):
    media, source_path = pair
    source, voices = assets(media)
    media.update_media_pool({"voiceover_media_ids": [voices["active"].id]})
    renamed = source_path.with_name("renamed.mp4")
    source_path.rename(renamed)
    Path(str(source_path) + ".composition.json").rename(Path(str(renamed) + ".composition.json"))
    media.repoint(source.id, str(renamed))
    restarted = MediaService(path=media.path)
    current, current_voices = assets(restarted)
    pool = restarted.media_pool()
    assert current.path == str(renamed)
    assert current.metadata["bound_voice_id"] == current_voices["active"].id
    assert pool["source_media_ids"] == [current.id]
    assert pool["voiceover_media_ids"] == [current_voices["active"].id]


def test_regeneration_replaces_pooled_voice_only_after_manifest_commit(pair):
    media, source_path = pair
    source, voices = assets(media)
    media.update_media_pool({"source_media_ids": [source.id]})
    assert media.media_pool()["voiceover_media_ids"] == [voices["active"].id]
    save_manifest(source_path, {"composition_id": "combo", "narration_binding_id": "pending"})
    current, voices = assets(media)
    pool = media.media_pool()
    assert pool["source_media_ids"] == [current.id]
    assert pool["voiceover_media_ids"] == [voices["pending"].id]
    assert not voices["active"].metadata.get("bound_source_id")
