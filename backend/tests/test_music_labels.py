from pathlib import Path

import pytest

from automated_video_editing_backend.services import music_labels as labels_module
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.music_labels import MusicLabels
from test_media_import_route import _client, _real_media, HEADERS


def test_labels_atomic_and_durable(tmp_path, monkeypatch):
    path = tmp_path / "labels.json"
    labels = MusicLabels(path)
    assert labels.labels() == ["轻快", "舒缓"]
    labels.create(" 国风 ")
    labels.assign("/music.mp3", ["国风", "舒缓", "国风"])
    assert MusicLabels(path).for_path("/music.mp3") == ["国风", "舒缓"]
    monkeypatch.setattr(labels_module, "write_json", lambda *_: False)
    with pytest.raises(ValueError, match="保存失败"):
        labels.assign("/music.mp3", [])
    assert labels.for_path("/music.mp3") == ["国风", "舒缓"]
    assert MusicLabels(path).for_path("/music.mp3") == ["国风", "舒缓"]


@pytest.mark.parametrize("name", ["", "未分类", "全部", "x" * 21, "a\nb"])
def test_invalid_label(name, tmp_path):
    with pytest.raises(ValueError):
        MusicLabels(tmp_path / "labels.json").create(name)


@pytest.mark.parametrize("mode", ["copy", "reference"])
def test_import_tag_rescan_restart_and_rename(monkeypatch, tmp_path, mode):
    media, _ = _real_media(monkeypatch, tmp_path)
    file = tmp_path / "music.mp3"
    file.write_bytes(b"audio")
    client = _client(monkeypatch, media)
    item = client.post(
        "/api/media/import", headers=HEADERS, json={"path": str(file), "storage_mode": mode}
    ).json()
    assert client.get("/api/music/labels", headers=HEADERS).json() == ["轻快", "舒缓"]
    assert (
        client.post("/api/music/labels", headers=HEADERS, json={"name": "国风"}).status_code == 200
    )
    response = client.put(
        f"/api/media/{item['id']}/music-labels", headers=HEADERS, json={"labels": ["国风"]}
    )
    assert response.status_code == 200
    reloaded = MediaService(path=media.path)
    restored = next(entry for entry in reloaded.list_items() if entry.path == item["path"])
    assert restored.metadata["music_labels"] == ["国风"]
    new_path = Path(restored.path).with_name("renamed.mp3")
    Path(restored.path).rename(new_path)
    reloaded.repoint(restored.id, str(new_path))
    final = MediaService(path=media.path)
    assert next(entry for entry in final.list_items() if entry.path == str(new_path)).metadata[
        "music_labels"
    ] == ["国风"]
    final.set_music_labels(
        next(entry.id for entry in final.list_items() if entry.path == str(new_path)), []
    )
    assert final.music_labels.for_path(str(new_path)) == []


def test_video_and_unknown_label_rejected(monkeypatch, tmp_path):
    media, _ = _real_media(monkeypatch, tmp_path)
    file = tmp_path / "clip.mp4"
    file.write_bytes(b"video")
    item = media.import_path(str(file))
    with pytest.raises(ValueError, match="只有音乐"):
        media.set_music_labels(item.id, ["舒缓"])
    with pytest.raises(ValueError, match="已存在"):
        media.music_labels.assign("x.mp3", ["missing"])


def test_corrupt_labels_cannot_be_overwritten_after_restart(tmp_path):
    path = tmp_path / "labels.json"
    path.write_text("{broken")
    for _ in range(2):
        with pytest.raises(ValueError):
            MusicLabels(path).create("国风")
    assert list(tmp_path.glob("labels.json.corrupt-*"))


def test_rename_failure_restores_labels(monkeypatch, tmp_path):
    media, _ = _real_media(monkeypatch, tmp_path)
    file = tmp_path / "music.mp3"
    file.write_bytes(b"audio")
    item = media.import_path(str(file))
    media.set_music_labels(item.id, ["舒缓"])

    def reject(*_):
        raise ValueError("catalog blocked")

    monkeypatch.setattr(media, "_repoint", reject)
    with pytest.raises(ValueError, match="catalog blocked"):
        media.repoint(item.id, str(tmp_path / "new.mp3"))
    assert MusicLabels(media.music_labels.path).for_path(str(file)) == ["舒缓"]
