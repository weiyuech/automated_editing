import pytest
import tempfile
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

from automated_video_editing_backend.api.routes import build_media_file_router
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.media import MediaService


@pytest.fixture
def media():
    return MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")


@pytest.fixture
def client(monkeypatch, media):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "test-token")
    app = FastAPI()
    app.include_router(build_media_file_router(media), prefix="/api")
    return TestClient(app)


@pytest.fixture
def clip():
    """A real file inside APP_ROOT, since the endpoint refuses anything outside it."""
    path = generated_path("cache", "test-media-file", "clip.mp4")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"0123456789")
    yield path
    path.unlink(missing_ok=True)
    path.parent.rmdir()


def test_serves_a_media_file_with_a_valid_token(client, clip):
    response = client.get(f"/api/media/file?path={clip}&token=test-token")

    assert response.status_code == 200
    assert response.content == b"0123456789"


def test_seeking_is_supported(client, clip):
    response = client.get(
        f"/api/media/file?path={clip}&token=test-token",
        headers={"Range": "bytes=2-5"},
    )

    # Without 206 + Content-Range a <video> element cannot scrub.
    assert response.status_code == 206
    assert response.headers["content-range"] == "bytes 2-5/10"
    assert response.content == b"2345"


@pytest.mark.parametrize("query", ["", "&token=", "&token=wrong"])
def test_a_bad_token_is_rejected(client, clip, query):
    assert client.get(f"/api/media/file?path={clip}{query}").status_code == 401


def test_files_outside_the_app_root_are_refused(client):
    # Without this guard the endpoint would read any file on the machine.
    for path in ["/etc/passwd", "/etc/../etc/hosts", "../../../../etc/passwd"]:
        response = client.get(f"/api/media/file?path={path}&token=test-token")
        assert response.status_code == 403, path


def test_an_imported_clip_outside_the_app_root_can_be_previewed(client, media, tmp_path):
    # Imported footage lives wherever the operator keeps it, so app-root-only would make
    # the preview useless for exactly the files people most want to preview.
    outside = tmp_path / "IMG_2026.MOV"
    outside.write_bytes(b"quicktime")

    assert client.get(f"/api/media/file?path={outside}&token=test-token").status_code == 403

    media.import_path(str(outside))
    response = client.get(f"/api/media/file?path={outside}&token=test-token")
    assert response.status_code == 200
    assert response.content == b"quicktime"


def test_non_media_files_inside_the_root_are_refused(client):
    secret = generated_path("cache", "test-media-file-secret.json")
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"api_key": "shh"}', encoding="utf-8")
    try:
        response = client.get(f"/api/media/file?path={secret}&token=test-token")
        # Being inside the root is not enough; settings.local.json lives there too.
        assert response.status_code == 415
    finally:
        secret.unlink(missing_ok=True)


def test_a_missing_file_is_not_found(client):
    path = generated_path("cache", "test-media-file", "absent.mp4")
    assert client.get(f"/api/media/file?path={path}&token=test-token").status_code == 404
