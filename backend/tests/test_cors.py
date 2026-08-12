from fastapi.testclient import TestClient

from automated_video_editing_backend.main import create_app


def test_local_vite_dev_ports_are_allowed(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "secret")
    client = TestClient(create_app())

    response = client.options(
        "/api/health",
        headers={
            "origin": "http://localhost:5174",
            "access-control-request-method": "GET",
            "access-control-request-headers": "x-bridge-token,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5174"


def test_packaged_electron_file_origin_is_allowed(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "secret")
    client = TestClient(create_app())

    response = client.options(
        "/api/health",
        headers={
            "origin": "null",
            "access-control-request-method": "GET",
            "access-control-request-headers": "x-bridge-token,content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "null"
