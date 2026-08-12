from pathlib import Path
import pytest
import tempfile
from pydantic import ValidationError

from automated_video_editing_backend.core.models import (
    AutomationSettingsUpdate,
    SeedanceSettingsUpdate,
    SettingsUpdateRequest,
    TTSGenerateRequest,
)
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.tts import TTSService


def test_settings_masks_and_preserves_blank_secrets(tmp_path):
    service = SettingsService(path=tmp_path / "settings.json")
    service.replace_for_development({
        "llm": {"enabled": True, "api_key": "abcd1234secret5678", "model": "ep-test"},
        "tts": {"enabled": True, "app_id": "1234567890", "access_token": "token-secret-1234"},
    })

    summary = service.summary()
    assert summary.llm.api_key.configured is True
    assert summary.llm.api_key.masked == "abcd...5678"
    assert summary.tts.access_token.masked == "toke...1234"

    service.update(SettingsUpdateRequest.model_validate({"llm": {"api_key": "", "model": "ep-next"}}))
    cfg = service.llm_config()
    assert cfg["api_key"] == "abcd1234secret5678"
    assert cfg["model"] == "ep-next"


def test_settings_accepts_only_robot_websocket_url(tmp_path):
    service = SettingsService(path=tmp_path / "settings.json")
    summary = service.update(
        SettingsUpdateRequest.model_validate({"robot": {"websocket_url": "ws://robot.local:8765"}})
    )

    assert summary.robot.enabled is True
    assert summary.robot.websocket_url == "ws://robot.local:8765"

    with pytest.raises(ValidationError):
        SettingsUpdateRequest.model_validate({"robot": {"websocket_url": "https://example.com"}})


def test_output_framing_starts_unset_and_persists_one_preference(tmp_path):
    service = SettingsService(path=tmp_path / "settings.json")
    assert service.summary().automation.output_aspect_ratio is None
    assert service.summary().automation.framing_configured is False

    service.update(SettingsUpdateRequest(
        automation=AutomationSettingsUpdate(
            output_aspect_ratio="9:16", framing_configured=True,
            framing_mode="custom", framing_crop_x=0.72, framing_crop_y=0.5,
        )
    ))

    reopened = SettingsService(path=tmp_path / "settings.json")
    saved = reopened.summary().automation
    assert saved.output_aspect_ratio == "9:16"
    assert saved.framing_configured is True
    assert saved.framing_mode == "custom"
    assert saved.framing_crop_x == 0.72

    # There is one preference slot, not a growing list: a later save replaces the tuple.
    reopened.update(SettingsUpdateRequest(automation=AutomationSettingsUpdate(
        output_aspect_ratio="16:9", framing_mode="center",
        framing_crop_x=0.5, framing_crop_y=0.5,
    )))
    replaced = reopened.summary().automation
    assert replaced.output_aspect_ratio == "16:9"
    assert replaced.framing_mode == "center"
    assert replaced.framing_crop_x == 0.5

    # Clearing means original frame; the stale tuple is deliberately hidden and ignored.
    reopened.update(SettingsUpdateRequest(automation=AutomationSettingsUpdate(
        framing_configured=False,
    )))
    cleared = reopened.summary().automation
    assert cleared.framing_configured is False
    assert cleared.output_aspect_ratio is None
    with pytest.raises(ValidationError):
        AutomationSettingsUpdate(output_aspect_ratio="1:1")


def test_seedance_model_is_preserved_when_settings_form_leaves_it_blank(tmp_path):
    service = SettingsService(path=tmp_path / "settings.json")
    service.replace_for_development({
        "seedance": {
            "enabled": True,
            "api_key": "ark-secret",
            "model": "ark-endpoint-id",
            "tos_access_key_id": "tos-ak",
            "tos_secret_access_key": "tos-sk",
        }
    })

    service.update(SettingsUpdateRequest.model_validate({"seedance": {"model": "", "daily_limit": 12}}))

    cfg = service.seedance_config()
    assert cfg["model"] == "ark-endpoint-id"
    assert cfg["daily_limit"] == 12


@pytest.mark.asyncio
async def test_tts_generation_creates_audio_asset(monkeypatch, tmp_path):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "tts": {
            "enabled": True,
            "app_id": "app-id",
            "access_token": "access-token",
            "voice_type": "BV001_streaming",
            "cluster": "volcano_tts",
        }
    })
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    service = TTSService(settings, media)

    async def fake_request(text):
        return b"fake-mp3", {"duration_ms": 320, "words": [{"word": "你", "start_time": 0, "end_time": 120}], "phonemes": []}

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    result = await service.synthesize(
        TTSGenerateRequest(title="unit voice", text="你好"),
        "你好",
    )

    try:
        assert result.asset.word_count == 1
        assert result.asset.duration_ms == 320
        assert result.media_item.kind == "audio"
        assert result.media_item.metadata["role"] == "tts_voice"
        assert result.words[0]["word"] == "你"
    finally:
        Path(result.asset.audio_path).unlink(missing_ok=True)
        if result.asset.metadata_path:
            Path(result.asset.metadata_path).unlink(missing_ok=True)


def test_seedance_summary_surfaces_both_models(tmp_path):
    """Seedance makes video, Seedream makes images: two separate model fields. Omitting
    image_model from the summary left the settings screen blank while the value was stored."""
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.update(SettingsUpdateRequest(seedance=SeedanceSettingsUpdate(
        model="doubao-seedance-2-0-fast-260128",
        image_model="doubao-seedream-5-0-260128",
    )))

    summary = settings.summary().seedance
    assert summary.model == "doubao-seedance-2-0-fast-260128"
    assert summary.image_model == "doubao-seedream-5-0-260128"

    # Blank means "keep what is stored", same as the other model field.
    settings.update(SettingsUpdateRequest(seedance=SeedanceSettingsUpdate(image_model="")))
    assert settings.summary().seedance.image_model == "doubao-seedream-5-0-260128"
