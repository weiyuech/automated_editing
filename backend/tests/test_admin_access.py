import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import automated_video_editing_backend.main as main_module
from automated_video_editing_backend.services.admin_access import AdminAccessService
from automated_video_editing_backend.services.settings import SettingsService


def test_admin_access_issues_and_revokes_a_process_local_token():
    access = AdminAccessService()

    with pytest.raises(HTTPException) as failure:
        access.unlock("user777", "wrong")
    assert failure.value.status_code == 401

    result = access.unlock("user777", "miaofei")
    token = str(result["token"])
    access.require(token)
    assert access.status(token) == {"unlocked": True}

    access.lock(token)
    assert access.status(token) == {"unlocked": False}
    with pytest.raises(HTTPException) as locked:
        access.require(token)
    assert locked.value.status_code == 403


def test_settings_mutation_requires_admin_but_masked_summary_remains_available(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "bridge-secret")
    settings_path = tmp_path / "settings.local.json"
    monkeypatch.setattr(
        main_module,
        "SettingsService",
        lambda: SettingsService(path=settings_path),
    )
    headers = {"x-bridge-token": "bridge-secret"}

    with TestClient(main_module.create_app()) as client:
        blocked = client.put(
            "/api/settings",
            headers=headers,
            json={"llm": {"api_key": "never-written"}},
        )
        assert blocked.status_code == 403
        assert not settings_path.exists()

        wrong = client.post(
            "/api/settings/admin/unlock",
            headers=headers,
            json={"username": "user777", "password": "wrong"},
        )
        assert wrong.status_code == 401

        unlocked = client.post(
            "/api/settings/admin/unlock",
            headers=headers,
            json={"username": "user777", "password": "miaofei"},
        )
        assert unlocked.status_code == 200
        admin_headers = {
            **headers,
            "x-admin-token": unlocked.json()["token"],
        }

        saved = client.put(
            "/api/settings",
            headers=admin_headers,
            json={"llm": {"api_key": "server-side-secret"}},
        )
        assert saved.status_code == 200

        summary = client.get("/api/settings", headers=headers)
        assert summary.status_code == 200
        assert summary.json()["llm"]["api_key"]["configured"] is True
        assert "server-side-secret" not in summary.text

        client.post("/api/settings/admin/lock", headers=admin_headers)
        relocked = client.put("/api/settings", headers=admin_headers, json={})
        assert relocked.status_code == 403
