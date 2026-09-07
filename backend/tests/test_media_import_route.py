from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automated_video_editing_backend.api.routes import build_router
from automated_video_editing_backend.services import media as media_module
from automated_video_editing_backend.services.media import (
    GeneratedMetadataPersistenceError,
    MediaLibraryPersistenceError,
    MediaService,
)

TOKEN = "media-import-route-test-token"
HEADERS = {"x-bridge-token": TOKEN}


class MediaStub:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str]] = []

    async def import_path_async(self, path: str, storage_mode: str):
        self.calls.append((path, storage_mode))
        if self.error is not None:
            raise self.error
        return {
            "id": "imported-media",
            "path": path,
            "kind": "video",
            "metadata": {"source": "local_import", "role": "raw_video"},
        }


def _client(monkeypatch: pytest.MonkeyPatch, media: object) -> TestClient:
    monkeypatch.setenv("APP_BRIDGE_TOKEN", TOKEN)
    unused = object()
    app = FastAPI()
    app.include_router(
        build_router(
            robot=unused,
            capture=unused,
            cruise=unused,
            cruise_routes=unused,
            media=media,
            jobs=unused,
            vault=unused,
            settings=unused,
            llm=unused,
            tts=unused,
            seedance=unused,
            renamer=unused,
            framing_test=unused,
            admin_access=unused,
        ),
        prefix="/api",
    )
    return TestClient(app, raise_server_exceptions=False)


def _real_media(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[MediaService, dict[str, Path]]:
    app_root = tmp_path / "app-root"
    managed = {
        "data": app_root / "data",
        "cache": app_root / ".cache",
        "logs": app_root / "logs",
        "exports": app_root / "exports",
        "previews": app_root / "previews",
    }
    monkeypatch.setattr(media_module, "GENERATED_DIRS", managed)
    monkeypatch.setattr(media_module, "ensure_inside_root", lambda path: Path(path).resolve())
    return MediaService(path=app_root / "data" / "media-library.json"), managed


def test_media_import_defaults_to_reference(monkeypatch):
    media = MediaStub()
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/clip.mp4"},
    )

    assert response.status_code == 200
    assert response.json()["id"] == "imported-media"
    assert media.calls == [("/external/clip.mp4", "reference")]


def test_media_import_forwards_copy_mode(monkeypatch):
    media = MediaStub()
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/clip.mp4", "storage_mode": "copy"},
    )

    assert response.status_code == 200
    assert media.calls == [("/external/clip.mp4", "copy")]


def test_media_import_reference_uses_real_service_without_copying(monkeypatch, tmp_path):
    media, managed = _real_media(monkeypatch, tmp_path)
    source = tmp_path / "external" / "reference.mp4"
    source.parent.mkdir()
    source.write_bytes(b"reference-video")

    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": str(source)},
    )

    assert response.status_code == 200
    imported = response.json()
    assert imported["path"] == str(source.resolve())
    assert imported["metadata"] == {"source": "local_import", "role": "raw_video"}
    assert source.read_bytes() == b"reference-video"
    assert list((managed["data"] / "downloads").iterdir()) == []
    assert media.get(imported["id"]).path == str(source.resolve())


def test_media_import_copy_uses_real_service_and_managed_identity(monkeypatch, tmp_path):
    media, managed = _real_media(monkeypatch, tmp_path)
    source = tmp_path / "external" / "客户素材.MP4"
    source.parent.mkdir()
    source.write_bytes(b"copied-video")

    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": str(source), "storage_mode": "copy"},
    )

    assert response.status_code == 200
    imported = response.json()
    copied_path = Path(imported["path"])
    assert copied_path.parent == (managed["data"] / "downloads").resolve()
    assert copied_path.name == "客户素材.mp4"
    assert copied_path.read_bytes() == b"copied-video"
    assert source.read_bytes() == b"copied-video"
    assert imported["metadata"] == {"source": "data/downloads", "role": "raw_video"}
    assert media.get(imported["id"]).path == str(copied_path)

    reopened = MediaService(path=media.path)
    restored = next(item for item in reopened.list_items() if item.path == str(copied_path))
    assert restored.metadata["source"] == "data/downloads"
    assert restored.metadata["role"] == "raw_video"


def test_media_pool_http_contract_preserves_offline_reference_until_explicit_clear(
    monkeypatch,
    tmp_path,
):
    media, _managed = _real_media(monkeypatch, tmp_path)
    source = tmp_path / "removable-drive" / "selected.mp4"
    source.parent.mkdir()
    source.write_bytes(b"referenced-video")
    client = _client(monkeypatch, media)

    imported = client.post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": str(source)},
    )
    assert imported.status_code == 200
    media_id = imported.json()["id"]
    selected_pool = {
        "source_media_ids": [media_id],
        "music_media_ids": [],
        "voiceover_media_ids": [],
        "effect_media_ids": [],
    }
    selected = client.put(
        "/api/media/pool",
        headers=HEADERS,
        json=selected_pool,
    )
    assert selected.status_code == 200
    assert selected.json() == selected_pool

    source.unlink()
    offline = client.get("/api/media/pool", headers=HEADERS)
    assert offline.status_code == 200
    assert offline.json() == selected_pool

    preserved = client.put(
        "/api/media/pool",
        headers=HEADERS,
        json=offline.json(),
    )
    assert preserved.status_code == 200
    assert preserved.json() == selected_pool

    cleared_pool = {**selected_pool, "source_media_ids": []}
    cleared = client.put(
        "/api/media/pool",
        headers=HEADERS,
        json=cleared_pool,
    )
    assert cleared.status_code == 200
    assert cleared.json() == cleared_pool

    source.write_bytes(b"reconnected-video")
    reopened = MediaService(path=media.path)
    reconnected = _client(monkeypatch, reopened).get(
        "/api/media/pool",
        headers=HEADERS,
    )
    assert reconnected.status_code == 200
    assert reconnected.json() == cleared_pool


def test_media_import_maps_missing_file_to_404(monkeypatch):
    media = MediaStub(FileNotFoundError("gone"))
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/gone.mp4"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "选择的媒体文件不存在或不是文件"


@pytest.mark.parametrize(
    "message",
    [
        "不支持此文件格式：.txt",
        "该文件位于应用托管目录，但不属于可导入的媒体区域",
    ],
)
def test_media_import_maps_invalid_media_to_400(monkeypatch, message):
    media = MediaStub(ValueError(message))
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/invalid.mp4"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == message


@pytest.mark.parametrize(
    "error",
    [
        MediaLibraryPersistenceError("媒体库无法保存"),
        GeneratedMetadataPersistenceError("成片记录无法保存"),
    ],
)
def test_media_import_maps_persistence_failures_to_409(monkeypatch, error):
    media = MediaStub(error)
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/clip.mp4"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == str(error)


def test_media_import_maps_copy_io_failure_to_500(monkeypatch):
    private_path = "/Users/customer/私密项目/原片.mp4"
    media = MediaStub(OSError(f"Permission denied: {private_path}"))
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/clip.mp4", "storage_mode": "copy"},
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "媒体文件导入失败；请检查磁盘空间和文件权限"
    assert private_path not in response.text


def test_media_import_rejects_unknown_storage_mode_before_service_call(monkeypatch):
    media = MediaStub()
    response = _client(monkeypatch, media).post(
        "/api/media/import",
        headers=HEADERS,
        json={"path": "/external/clip.mp4", "storage_mode": "move"},
    )

    assert response.status_code == 422
    assert media.calls == []
