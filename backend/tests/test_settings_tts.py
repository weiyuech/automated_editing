import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from automated_video_editing_backend.api.routes import build_router
from automated_video_editing_backend.core.models import (
    AutomationSettingsUpdate,
    CameraworkConfig,
    RobotState,
    SeedanceSettingsUpdate,
    SettingsUpdateRequest,
    TTSGenerateRequest,
)
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.admin_access import AdminAccessService
from automated_video_editing_backend.services.media import (
    MediaPoolPersistenceError,
    MediaService,
)
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.tts import TTSService


@pytest.fixture
def tts_root():
    root = generated_path("cache", "test-tts", uuid4().hex[:8])
    root.mkdir(parents=True, exist_ok=True)
    yield root
    shutil.rmtree(root, ignore_errors=True)


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


def test_failed_settings_save_restores_the_in_memory_configuration(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    service = SettingsService(path=path)
    service.update(
        SettingsUpdateRequest.model_validate({"robot": {"websocket_url": "ws://old.local:8765"}})
    )

    def fail_save():
        raise OSError("settings disk is read-only")

    monkeypatch.setattr(service, "_save", fail_save)
    with pytest.raises(OSError, match="read-only"):
        service.update(
            SettingsUpdateRequest.model_validate(
                {"robot": {"websocket_url": "ws://new.local:8765"}}
            )
        )

    assert service.summary().robot.websocket_url == "ws://old.local:8765"
    assert json.loads(path.read_text(encoding="utf-8"))["robot"]["websocket_url"] == (
        "ws://old.local:8765"
    )


@pytest.mark.asyncio
async def test_settings_endpoint_repairs_robot_url_without_releasing_active_capture(tmp_path):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.update(
        SettingsUpdateRequest.model_validate(
            {"robot": {"websocket_url": "ws://wrong.local:8765"}}
        )
    )

    class Robot:
        def __init__(self):
            self.preserve_capture_recovery = None

        async def status(self):
            return RobotState(connected=False, recording=False)

        async def configure_websocket_url_and_commit(
            self,
            _url,
            commit,
            *,
            preserve_capture_recovery=False,
        ):
            self.preserve_capture_recovery = preserve_capture_recovery
            return commit()

    class Capture:
        session = object()

        def active_session(self):
            return self.session

    class Cruise:
        is_running = False

    class FramingTest:
        def status(self):
            return {"running": False}

    robot = Robot()
    capture = Capture()
    router = build_router(
        robot,
        capture,
        Cruise(),
        None,
        None,
        None,
        None,
        settings,
        None,
        None,
        None,
        None,
        FramingTest(),
        AdminAccessService(),
    )
    update_endpoint = next(
        route.endpoint
        for route in router.routes
        if route.path == "/settings" and "PUT" in (route.methods or set())
    )

    summary = await update_endpoint(
        SettingsUpdateRequest.model_validate(
            {"robot": {"websocket_url": "ws://correct.local:8765"}}
        )
    )

    assert robot.preserve_capture_recovery is True
    assert capture.active_session() is capture.session
    assert summary.robot.websocket_url == "ws://correct.local:8765"


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


def test_camerawork_profile_is_unconfigured_until_saved_and_persists_absolute_limits(tmp_path):
    path = tmp_path / "settings.json"
    service = SettingsService(path=path)
    assert service.summary().automation.camerawork.configured is False

    profile = CameraworkConfig(
        configured=True,
        anchor_yaw=5,
        anchor_pitch=1,
        anchor_zoom=1.2,
        yaw_min=-20,
        yaw_max=35,
        pitch_min=-6,
        pitch_max=8,
        zoom_min=1,
        zoom_max=1.8,
        speed_min=2,
        speed_max=4,
        anchor_time_percent=35,
        anchor_dwell_seconds=8.5,
    )
    service.update(SettingsUpdateRequest(
        automation=AutomationSettingsUpdate(camerawork=profile)
    ))

    saved = SettingsService(path=path).camerawork_config()
    assert saved == profile


def test_legacy_camerawork_settings_migrate_to_fixed_origin_and_program(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "automation": {
            "camerawork": {
                "configured": True,
                "anchor_yaw": 4,
                "anchor_pitch": -2,
                "anchor_zoom": 1.1,
                "yaw_min": -30,
                "yaw_max": 40,
                "pitch_min": -10,
                "pitch_max": 8,
                "zoom_min": 1,
                "zoom_max": 1.6,
                "speed_min": 2,
                "speed_max": 4,
            },
        },
    }), encoding="utf-8")

    loaded = SettingsService(path=path).camerawork_config()

    assert loaded.configured is True
    assert loaded.anchor_yaw == 0
    assert loaded.anchor_pitch == 0
    assert loaded.point_mode == 4
    assert "anchor_time_percent" not in loaded.model_dump()
    assert "anchor_dwell_seconds" not in loaded.model_dump()
    assert CameraworkConfig(anchor_zoom=3.5).anchor_zoom == 3.5


@pytest.mark.parametrize("patch", [
    {"yaw_min": 10, "yaw_max": 10},
    {"pitch_min": 0, "pitch_max": 5},
    {"anchor_zoom": 3.6},
    {"speed_max": 1},
    {"point_mode": 5},
    {"piece_ids": ["upper-left"]},
])
def test_camerawork_rejects_invalid_ranges_and_anchors(patch):
    with pytest.raises(ValidationError):
        CameraworkConfig(configured=True, **patch)


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
    # Publishing a generated narration makes it available in the media library, but the
    # operator's working pool changes only through the explicit media-pool endpoint.
    media.update_media_pool({})
    service = TTSService(settings, media)
    service.usage_path = tmp_path / "usage.json"  # keep the daily counter out of real data/tts

    async def fake_request(text):
        return b"fake-mp3", {"duration_ms": 320, "words": [{"word": "六合桥", "start_time": 0, "end_time": 120}], "phonemes": []}

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    result = await service.synthesize(
        TTSGenerateRequest(title="unit voice", text="六和桥"),
        "六和桥",
    )

    try:
        assert result.asset.word_count == 1
        assert result.asset.duration_ms == 320
        assert result.asset.timing_quality == "exact"
        assert result.media_item.kind == "audio"
        assert result.media_item.metadata["role"] == "tts_voice"
        assert result.media_item.metadata["timing_quality"] == "exact"
        assert result.words[0]["word"] == "六和桥"
        assert result.words[0]["start_time"] == 0
        assert result.words[0]["end_time"] == 120
        assert json.loads(Path(result.asset.metadata_path).read_text(encoding="utf-8"))[
            "timing_quality"
        ] == "exact"
        assert media.media_pool()["voiceover_media_ids"] == []
    finally:
        Path(result.asset.audio_path).unlink(missing_ok=True)
        if result.asset.metadata_path:
            Path(result.asset.metadata_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_tts_first_generation_seeds_only_preexisting_narrations(
    monkeypatch, tts_root
):
    import automated_video_editing_backend.services.media as media_module

    managed = {
        "data": tts_root / "data",
        "cache": tts_root / "cache",
        "logs": tts_root / "logs",
        "exports": tts_root / "exports",
        "previews": tts_root / "previews",
    }
    monkeypatch.setattr(media_module, "GENERATED_DIRS", managed)
    narration_dir = managed["data"] / "tts"
    narration_dir.mkdir(parents=True)
    older = narration_dir / "older.mp3"
    older.write_bytes(b"old-audio")

    settings = SettingsService(path=tts_root / "settings.json")
    settings.replace_for_development({
        "tts": {
            "enabled": True,
            "app_id": "app-id",
            "access_token": "access-token",
            "voice_type": "BV001_streaming",
            "cluster": "volcano_tts",
        }
    })
    media = MediaService(path=tts_root / "media-library.json")
    assert not media.pool_path.exists()
    service = TTSService(settings, media)
    service.tts_dir = narration_dir
    service.usage_path = tts_root / "usage.json"

    async def fake_request(_text):
        return b"new-audio", {
            "duration_ms": 200,
            "words": [{"word": "新", "start_time": 0, "end_time": 200}],
            "phonemes": [],
        }

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    result = await service.synthesize(
        TTSGenerateRequest(title="new narration", text="新"),
        "新",
    )

    pool_ids = media.media_pool()["voiceover_media_ids"]
    pooled_paths = {media.get(media_id).path for media_id in pool_ids}
    assert str(older.resolve()) in pooled_paths
    assert result.media_item.id not in pool_ids
    assert media.get(result.media_item.id) is result.media_item


@pytest.mark.asyncio
async def test_tts_refuses_generation_before_quota_when_pool_cannot_initialize(
    monkeypatch, tmp_path
):
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
    media = MediaService(path=tmp_path / "media-library.json")
    service = TTSService(settings, media)

    def broken_pool():
        raise MediaPoolPersistenceError("媒体池无法保存")

    def must_not_read_quota():
        raise AssertionError("quota must not be touched before the pool is durable")

    async def must_not_request(_text):
        raise AssertionError("provider must not be called before the pool is durable")

    monkeypatch.setattr(media, "ensure_media_pool_initialized", broken_pool)
    monkeypatch.setattr(service, "quota", must_not_read_quota)
    monkeypatch.setattr(service, "_request_sync_tts", must_not_request)

    with pytest.raises(MediaPoolPersistenceError, match="媒体池无法保存"):
        await service.synthesize(TTSGenerateRequest(text="一句旁白"), "一句旁白")


@pytest.mark.asyncio
async def test_tts_metadata_failure_never_publishes_orphan_audio(monkeypatch, tmp_path):
    import automated_video_editing_backend.services.tts as tts_module

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
    media = MediaService(path=tmp_path / "media-library.json")
    service = TTSService(settings, media)
    service.tts_dir = tmp_path / "tts"
    service.tts_dir.mkdir()
    service.usage_path = tmp_path / "usage.json"
    durable_write = tts_module.write_json

    async def fake_request(_text):
        return b"fake-mp3", {
            "duration_ms": 320,
            "words": [{"word": "六和桥", "start_time": 0, "end_time": 320}],
            "phonemes": [],
        }

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    monkeypatch.setattr(
        "automated_video_editing_backend.services.tts.ensure_inside_root",
        lambda path: Path(path).resolve(),
    )

    def fail_only_metadata(path, payload):
        if Path(path).parent == service.tts_dir:
            return False
        return durable_write(path, payload)

    monkeypatch.setattr(tts_module, "write_json", fail_only_metadata)

    with pytest.raises(RuntimeError, match="旁白文字和时间数据无法保存"):
        await service.synthesize(TTSGenerateRequest(title="失败旁白", text="六和桥"), "六和桥")

    assert list(service.tts_dir.iterdir()) == []
    assert service.list_assets() == []
    assert service.quota().used == 1
    assert service.quota().pending == 0


def test_resource_id_is_derived_from_cluster(tmp_path):
    """A cloned voice needs X-Api-Resource-Id to return the per-word timestamps subtitles are
    built from. Standard voices need none; the volcano_icl clusters map to the clone models."""
    settings = SettingsService(path=tmp_path / "settings.json")
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    service = TTSService(settings, media)

    assert service._resource_id({"cluster": "volcano_tts"}) == ""
    assert service._resource_id({"cluster": "volcano_icl"}) == "volc.megatts.default"
    assert service._resource_id({"cluster": "volcano_icl_concurr"}) == "volc.megatts.concurr"
    assert service._resource_id({"cluster": "volcano_icl", "resource_id": "volc.custom"}) == "volc.custom"


def test_tts_duration_falls_back_to_the_latest_valid_word_timestamp(tmp_path):
    service = TTSService(
        SettingsService(path=tmp_path / "settings.json"),
        MediaService(path=tmp_path / "media-library.json"),
    )

    assert service._duration_ms(
        {},
        [
            {"word": "六和", "end_time": "720"},
            {"word": "桥", "end_time": 1250.8},
            {"word": "损坏", "end_time": "later"},
        ],
    ) == 1250
    assert service._duration_ms(
        {"duration": "0"},
        [{"word": "六和桥", "end_time": 900}],
    ) == 900
    assert service._duration_ms(
        {"duration": "640"},
        [{"word": "六和桥", "end_time": 900}],
    ) == 640


@pytest.mark.asyncio
async def test_cloned_voice_request_sends_resource_id_header(monkeypatch, tmp_path):
    """volcano_icl must carry X-Api-Resource-Id (or the cloned voice returns no timestamps);
    a standard volcano_tts request must not send it."""
    captured: dict[str, dict] = {}

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"code": 3000, "data": "ZmFrZQ==", "addition": {"duration": "100"}}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("automated_video_editing_backend.services.tts.httpx.AsyncClient", FakeClient)
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")

    icl = SettingsService(path=tmp_path / "icl.json")
    icl.replace_for_development({"tts": {"enabled": True, "app_id": "a", "access_token": "t",
                                         "voice_type": "S_nFIhsvHX1", "cluster": "volcano_icl"}})
    await TTSService(icl, media)._request_sync_tts("你好")
    assert captured["headers"]["X-Api-Resource-Id"] == "volc.megatts.default"

    std = SettingsService(path=tmp_path / "std.json")
    std.replace_for_development({"tts": {"enabled": True, "app_id": "a", "access_token": "t",
                                         "voice_type": "BV001_streaming", "cluster": "volcano_tts"}})
    await TTSService(std, media)._request_sync_tts("你好")
    assert "X-Api-Resource-Id" not in captured["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("encoded_audio", [None, "", "not-valid-base64%%%"])
async def test_empty_or_invalid_provider_audio_never_publishes_but_keeps_reserved_quota(
    monkeypatch,
    tmp_path,
    encoded_audio,
):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"tts": {
        "enabled": True,
        "app_id": "a",
        "access_token": "t",
        "voice_type": "BV001_streaming",
        "cluster": "volcano_tts",
        "daily_limit": 1,
    }})
    media = MediaService(path=tmp_path / "media-library.json")
    service = TTSService(settings, media)
    service.tts_dir = tmp_path / "tts"
    service.tts_dir.mkdir()
    service.usage_path = tmp_path / "usage.json"

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {
                "code": 3000,
                "data": encoded_audio,
                "addition": {"duration": "100"},
            }

    class FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "automated_video_editing_backend.services.tts.httpx.AsyncClient",
        FakeClient,
    )

    with pytest.raises(ValueError, match="音频"):
        await service.synthesize(TTSGenerateRequest(title="bad", text="六和桥"), "六和桥")

    assert service.quota().used == 1
    assert service.quota().pending == 0
    assert list(service.tts_dir.iterdir()) == []
    assert service.usage_path.exists()
    assert all(Path(item.path).parent != service.tts_dir for item in media.list_items())


@pytest.mark.asyncio
async def test_voiceover_daily_limit_counts_and_blocks(monkeypatch, tmp_path):
    """Every successful 旁白 spends one of the day's allowance (regardless of LLM assist,
    which happens upstream in the route), and generation is blocked once it is used up."""
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"tts": {
        "enabled": True, "app_id": "a", "access_token": "t",
        "voice_type": "BV001_streaming", "cluster": "volcano_tts", "daily_limit": 2,
    }})
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    service = TTSService(settings, media)
    service.usage_path = tmp_path / "usage.json"

    async def fake_request(text):
        return b"fake-mp3", {"duration_ms": 100, "words": [], "phonemes": []}

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)

    assert service.quota().remaining == 2
    made = []
    try:
        first = await service.synthesize(TTSGenerateRequest(title="a", text="一"), "一")
        assert (first.quota.used, first.quota.remaining) == (1, 1)
        assert first.words == []
        assert first.asset.timing_quality == "unavailable"
        assert first.media_item.metadata["timing_quality"] == "unavailable"
        assert json.loads(Path(first.asset.metadata_path).read_text(encoding="utf-8"))[
            "timing_quality"
        ] == "unavailable"
        made.append(first)
        second = await service.synthesize(TTSGenerateRequest(title="b", text="二"), "二")
        assert (second.quota.used, second.quota.remaining) == (2, 0)
        made.append(second)
        with pytest.raises(ValueError):
            await service.synthesize(TTSGenerateRequest(title="c", text="三"), "三")
    finally:
        for result in made:
            Path(result.asset.audio_path).unlink(missing_ok=True)
            if result.asset.metadata_path:
                Path(result.asset.metadata_path).unlink(missing_ok=True)


def test_corrupt_tts_usage_blocks_quota_across_restart(tmp_path):
    settings = SettingsService(path=tmp_path / "settings.json")
    usage_path = tmp_path / "usage.json"
    usage_path.write_text('{"broken":', encoding="utf-8")

    service = TTSService(settings, MediaService(path=tmp_path / "media-library.json"))
    service.usage_path = usage_path
    with pytest.raises(RuntimeError, match="旁白额度记录不可用"):
        service.quota()

    quarantined = list(tmp_path.glob("usage.json.corrupt-*"))
    assert len(quarantined) == 1
    reopened = TTSService(settings, MediaService(path=tmp_path / "reopened-media.json"))
    reopened.usage_path = usage_path
    with pytest.raises(RuntimeError, match="已保留的损坏文件"):
        reopened.quota()


def test_tts_usage_accepts_legacy_integer_counts_and_restores_that_shape_after_settlement(
    tmp_path,
):
    usage_path = tmp_path / "usage.json"
    service = TTSService(
        SettingsService(path=tmp_path / "settings.json"),
        MediaService(path=tmp_path / "media-library.json"),
    )
    service.usage_path = usage_path
    today = service.quota().date
    usage_path.write_text(json.dumps({today: 2}), encoding="utf-8")

    before = service.quota()
    assert (before.used, before.pending) == (2, 0)

    reservation_id = service._reserve_usage(today)
    reserved = json.loads(usage_path.read_text(encoding="utf-8"))[today]
    assert reserved == {"used": 3, "pending": [reservation_id]}

    service._settle_usage(today, reservation_id)
    assert json.loads(usage_path.read_text(encoding="utf-8")) == {today: 3}
    after = service.quota()
    assert (after.used, after.pending) == (3, 0)


def test_pending_tts_reservation_remains_counted_across_restart(tmp_path):
    usage_path = tmp_path / "usage.json"
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"tts": {"daily_limit": 1}})
    service = TTSService(settings, MediaService(path=tmp_path / "media-library.json"))
    service.usage_path = usage_path
    today = service.quota().date

    reservation_id = service._reserve_usage(today)
    assert reservation_id

    reopened = TTSService(settings, MediaService(path=tmp_path / "reopened-media.json"))
    reopened.usage_path = usage_path
    quota = reopened.quota()
    assert (quota.used, quota.pending, quota.remaining) == (1, 1, 0)


def test_unreadable_tts_usage_blocks_quota(monkeypatch, tmp_path):
    service = TTSService(
        SettingsService(path=tmp_path / "settings.json"),
        MediaService(path=tmp_path / "media-library.json"),
    )
    service.usage_path = tmp_path / "usage.json"
    monkeypatch.setattr(
        "automated_video_editing_backend.services.tts.read_json",
        lambda _path: (None, "usage.json 无法读取：permission denied"),
    )

    with pytest.raises(RuntimeError, match="permission denied"):
        service.quota()
    with pytest.raises(RuntimeError, match="旁白额度记录不可用"):
        service.quota()


def test_tts_usage_write_failure_latches_generation_closed(monkeypatch, tmp_path):
    service = TTSService(
        SettingsService(path=tmp_path / "settings.json"),
        MediaService(path=tmp_path / "media-library.json"),
    )
    service.usage_path = tmp_path / "usage.json"
    monkeypatch.setattr(
        "automated_video_editing_backend.services.tts.write_json",
        lambda *_args, **_kwargs: False,
    )

    with pytest.raises(RuntimeError, match="usage.json 无法保存"):
        service._increment_usage("2026-09-04")
    with pytest.raises(RuntimeError, match="已停止生成"):
        service.quota()


@pytest.mark.asyncio
async def test_tts_reservation_write_failure_prevents_provider_call(
    monkeypatch,
    tmp_path,
    tts_root,
):
    import automated_video_editing_backend.services.tts as tts_module

    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "tts": {
            "enabled": True,
            "app_id": "a",
            "access_token": "t",
            "voice_type": "BV001_streaming",
            "cluster": "volcano_tts",
            "daily_limit": 1,
        }
    })
    media = MediaService(path=tmp_path / "media-library.json")
    service = TTSService(settings, media)
    service.tts_dir = tts_root
    service.usage_path = tts_root / "usage.json"
    durable_write = tts_module.write_json

    provider_calls = 0

    async def fake_request(_text):
        nonlocal provider_calls
        provider_calls += 1
        return b"fake-mp3", {"duration_ms": 100, "words": [], "phonemes": []}

    def fail_only_usage(path, payload):
        if Path(path) == service.usage_path:
            return False
        return durable_write(path, payload)

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    monkeypatch.setattr(tts_module, "write_json", fail_only_usage)

    with pytest.raises(RuntimeError, match="usage.json 无法保存"):
        await service.synthesize(TTSGenerateRequest(title="unaccounted", text="一"), "一")

    assert list(tts_root.iterdir()) == []
    assert not [item for item in media.list_items() if Path(item.path).parent == tts_root]
    assert provider_calls == 0
    with pytest.raises(RuntimeError, match="已停止生成"):
        service.quota()


@pytest.mark.asyncio
async def test_provider_failure_settles_pending_but_never_refunds_daily_slot(
    monkeypatch,
    tmp_path,
    tts_root,
):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"tts": {
        "enabled": True,
        "app_id": "a",
        "access_token": "t",
        "voice_type": "BV001_streaming",
        "cluster": "volcano_tts",
        "daily_limit": 1,
    }})
    service = TTSService(settings, MediaService(path=tmp_path / "media-library.json"))
    service.tts_dir = tts_root
    service.usage_path = tts_root / "usage.json"
    today = service.quota().date
    provider_calls = 0
    reservation_seen = False

    async def failed_request(_text):
        nonlocal provider_calls, reservation_seen
        provider_calls += 1
        persisted = json.loads(service.usage_path.read_text(encoding="utf-8"))
        record = persisted[today]
        reservation_seen = record["used"] == 1 and len(record["pending"]) == 1
        raise ValueError("语音服务暂时不可用")

    monkeypatch.setattr(service, "_request_sync_tts", failed_request)

    with pytest.raises(ValueError, match="暂时不可用.*本次已计入今日旁白次数"):
        await service.synthesize(TTSGenerateRequest(title="failed", text="一"), "一")

    assert reservation_seen is True
    quota = service.quota()
    assert (quota.used, quota.pending, quota.remaining) == (1, 0, 0)
    assert json.loads(service.usage_path.read_text(encoding="utf-8")) == {
        today: 1,
    }
    with pytest.raises(ValueError, match="今日旁白生成已达上限"):
        await service.synthesize(TTSGenerateRequest(title="retry", text="二"), "二")
    assert provider_calls == 1


@pytest.mark.asyncio
async def test_tts_settlement_write_failure_leaves_durable_pending_slot(
    monkeypatch,
    tmp_path,
    tts_root,
):
    import automated_video_editing_backend.services.tts as tts_module

    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"tts": {
        "enabled": True,
        "app_id": "a",
        "access_token": "t",
        "voice_type": "BV001_streaming",
        "cluster": "volcano_tts",
        "daily_limit": 1,
    }})
    media = MediaService(path=tmp_path / "media-library.json")
    service = TTSService(settings, media)
    service.tts_dir = tts_root
    service.usage_path = tts_root / "usage.json"
    today = service.quota().date
    durable_write = tts_module.write_json
    usage_writes = 0

    async def fake_request(_text):
        return b"fake-mp3", {"duration_ms": 100, "words": [], "phonemes": []}

    def fail_settlement(path, payload):
        nonlocal usage_writes
        if Path(path) == service.usage_path:
            usage_writes += 1
            if usage_writes == 2:
                return False
        return durable_write(path, payload)

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    monkeypatch.setattr(tts_module, "write_json", fail_settlement)

    with pytest.raises(RuntimeError, match="usage.json 无法保存"):
        await service.synthesize(TTSGenerateRequest(title="settlement", text="一"), "一")

    persisted = json.loads(service.usage_path.read_text(encoding="utf-8"))
    record = persisted[today]
    assert record["used"] == 1
    assert len(record["pending"]) == 1

    reopened = TTSService(settings, MediaService(path=tmp_path / "reopened-media.json"))
    reopened.usage_path = service.usage_path
    quota = reopened.quota()
    assert (quota.used, quota.pending, quota.remaining) == (1, 1, 0)


@pytest.mark.asyncio
async def test_last_tts_slot_cannot_be_spent_by_two_concurrent_requests(
    monkeypatch,
    tmp_path,
    tts_root,
):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "tts": {
            "enabled": True,
            "app_id": "a",
            "access_token": "t",
            "voice_type": "BV001_streaming",
            "cluster": "volcano_tts",
            "daily_limit": 1,
        }
    })
    service = TTSService(settings, MediaService(path=tmp_path / "media-library.json"))
    service.tts_dir = tts_root
    service.usage_path = tts_root / "usage.json"
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()
    provider_calls = 0

    async def fake_request(_text):
        nonlocal provider_calls
        provider_calls += 1
        provider_started.set()
        await release_provider.wait()
        return b"fake-mp3", {"duration_ms": 100, "words": [], "phonemes": []}

    monkeypatch.setattr(service, "_request_sync_tts", fake_request)
    first_task = asyncio.create_task(
        service.synthesize(TTSGenerateRequest(title="first", text="一"), "一")
    )
    await provider_started.wait()
    second_task = asyncio.create_task(
        service.synthesize(TTSGenerateRequest(title="second", text="二"), "二")
    )
    await asyncio.sleep(0)
    assert provider_calls == 1

    release_provider.set()
    first = await first_task
    with pytest.raises(ValueError, match="今日旁白生成已达上限"):
        await second_task

    assert provider_calls == 1
    assert service.quota().used == 1
    Path(first.asset.audio_path).unlink(missing_ok=True)
    if first.asset.metadata_path:
        Path(first.asset.metadata_path).unlink(missing_ok=True)


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
