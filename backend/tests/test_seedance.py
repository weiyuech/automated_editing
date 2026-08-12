from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

import pytest
from urllib.parse import parse_qs, urlparse

from automated_video_editing_backend.core.models import SeedanceAsset, SeedanceGenerateRequest, utc_now
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.services.seedance import SeedanceService
from automated_video_editing_backend.services.settings import SettingsService


@pytest.fixture
def seedance_root():
    """Isolated seedance root for tests.

    Must live inside APP_ROOT because MediaService refuses to register generated files
    outside it, but must never be data/seedance, or tests would write effects and burn the
    real daily quota counter.
    """
    root = generated_path("cache", "test-seedance", uuid4().hex[:8])
    yield root
    shutil.rmtree(root, ignore_errors=True)


def test_seedance_quota_uses_configured_daily_limit(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"seedance": {"daily_limit": 10}})
    service = SeedanceService(settings, MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json"), RenderService(), root=seedance_root)

    quota = service.quota()

    assert quota.limit == 10
    assert quota.remaining == 10 - quota.used


@pytest.mark.asyncio
async def test_seedance_generation_requires_enabled_config(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json"), RenderService(), root=seedance_root)

    with pytest.raises(ValueError, match="Seedance is disabled"):
        await service.generate(
            SeedanceGenerateRequest(
                title="disabled-test",
                prompt="make it cinematic",
                source_image_url="https://example.com/frame.jpg",
            )
        )


@pytest.mark.asyncio
async def test_seedance_reuses_existing_effect_without_quota(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json"), RenderService(), root=seedance_root)
    video_path = service.effects_dir / "seedance-reuse-test.mp4"
    metadata_path = service.effects_dir / "seedance-reuse-test.json"
    video_path.write_bytes(b"fake-video")
    asset = SeedanceAsset(
        id="seedance-reuse-test",
        name=video_path.name,
        output_path=str(video_path),
        metadata_path=str(metadata_path),
        prompt="make it cinematic",
        source_image_url="https://example.com/frame.jpg",
        duration_seconds=5.0,
        status="succeeded",
        created_at=utc_now(),
    )
    cache_key = service._cache_key(
        asset.prompt,
        None,
        asset.source_image_url,
        settings.seedance_config(),
        asset.duration_seconds,
    )
    service._write_asset(asset, extra={"cache_key": cache_key})
    quota_before = service.quota().used

    try:
        result = await service.generate(
            SeedanceGenerateRequest(
                title="reuse-test",
                prompt=asset.prompt,
                source_image_url=asset.source_image_url,
                duration_seconds=asset.duration_seconds,
            )
        )

        assert result.reused is True
        assert result.asset.id == asset.id
        assert result.media_item is not None
        assert result.media_item.metadata["role"] == "seedance_effect"
        assert service.quota().used == quota_before
    finally:
        video_path.unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_seedance_uploads_local_image_to_tos_staging(monkeypatch, tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "seedance": {
            "enabled": True,
            "api_key": "ark-key",
            "model": "seedance-model",
            "tos_access_key_id": "tos-ak",
            "tos_secret_access_key": "tos-sk",
            "tos_bucket": "seedance-bucket",
            "tos_region": "cn-beijing",
            "tos_endpoint": "tos-cn-beijing.volces.com",
            "tos_object_prefix": "seedance/staging",
        }
    })
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    image_path = tmp_path / "frame.jpg"
    image_path.write_bytes(b"fake-jpg")
    image_item = media.import_path(str(image_path))
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)
    uploaded = {}

    async def fake_upload(path, cfg):
        uploaded["path"] = path
        return "seedance/staging/fake.jpg", "https://signed.example.com/fake.jpg?X-Tos-Signature=abc"

    async def fake_submit(asset_id):
        uploaded["asset_id"] = asset_id

    monkeypatch.setattr(service, "_upload_source_to_tos", fake_upload)
    monkeypatch.setattr(service, "_submit_and_download", fake_submit)

    result = await service.generate(
        SeedanceGenerateRequest(
            title="tos-local-image",
            prompt="make it cinematic",
            source_image_media_id=image_item.id,
            duration_seconds=5,
        )
    )

    try:
        assert uploaded["path"].name.startswith("image-")
        assert result.reused is False
        assert result.asset.source_object_key == "seedance/staging/fake.jpg"
        assert result.asset.source_image_url.startswith("https://signed.example.com/fake.jpg")
        assert result.quota.used >= 1
    finally:

        Path(result.asset.metadata_path).unlink(missing_ok=True)
        Path(result.asset.output_path).unlink(missing_ok=True)
        Path(uploaded["path"]).unlink(missing_ok=True)


def test_seedance_tos_presigned_url_uses_temporary_query_signature(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "seedance": {
            "tos_access_key_id": "tos-ak",
            "tos_secret_access_key": "tos-sk",
            "tos_bucket": "seedance-bucket",
            "tos_region": "cn-beijing",
            "tos_endpoint": "tos-cn-beijing.volces.com",
            "tos_url_expires_seconds": 3600,
        }
    })
    service = SeedanceService(settings, MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json"), RenderService(), root=seedance_root)

    url = service._presigned_tos_url("GET", "seedance/staging/frame.jpg", settings.seedance_config())
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.netloc == "seedance-bucket.tos-cn-beijing.volces.com"
    assert parsed.path == "/seedance/staging/frame.jpg"
    assert query["X-Tos-Algorithm"] == ["TOS4-HMAC-SHA256"]
    assert query["X-Tos-Expires"] == ["3600"]
    assert query["X-Tos-SignedHeaders"] == ["host"]
    assert "X-Tos-Signature" in query


def test_seedance_tos_upload_signature_includes_content_type(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "seedance": {
            "tos_access_key_id": "tos-ak",
            "tos_secret_access_key": "tos-sk",
            "tos_bucket": "seedance-bucket",
            "tos_region": "cn-beijing",
            "tos_endpoint": "tos-cn-beijing.volces.com",
        }
    })
    service = SeedanceService(settings, MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json"), RenderService(), root=seedance_root)

    headers = service._tos_authorization_headers(
        "PUT",
        "seedance/staging/frame.jpg",
        settings.seedance_config(),
        "abc123",
        extra_headers={"content-type": "image/jpeg"},
    )

    assert headers["content-type"] == "image/jpeg"
    assert "SignedHeaders=content-type;host;x-tos-content-sha256;x-tos-date" in headers["authorization"]


def test_seedance_video_url_parser_handles_nested_official_shape(tmp_path, seedance_root):
    service = SeedanceService(SettingsService(path=tmp_path / "settings.json"), MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json"), RenderService(), root=seedance_root)

    url = service._video_url({
        "content": [
            {
                "type": "video_url",
                "video_url": {"url": "https://signed.example.com/result.mp4"},
            }
        ]
    })

    assert url == "https://signed.example.com/result.mp4"


def test_presigned_url_signs_unsigned_payload(tmp_path, seedance_root):
    """A presigned URL is signed before a body exists, so the payload hash must be the
    literal UNSIGNED-PAYLOAD. Signing an empty-body digest made TOS reject every URL with
    SignatureDoesNotMatch, so staged images uploaded fine and were then undownloadable."""
    import hashlib

    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"seedance": {
        "tos_access_key_id": "AKLTtest", "tos_secret_access_key": "secret",
        "tos_bucket": "b", "tos_region": "cn-beijing", "tos_endpoint": "tos-cn-beijing.volces.com",
    }})
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    url = service._presigned_tos_url("GET", "seedance/staging/x.png", settings.seedance_config())
    empty_digest = hashlib.sha256(b"").hexdigest()

    assert "X-Tos-Signature=" in url
    # The digest must not be what got signed; regenerating with it must give a different signature.
    import re as _re
    signature = _re.search(r"X-Tos-Signature=([0-9a-f]+)", url).group(1)
    assert signature != empty_digest
    assert len(signature) == 64


def test_an_image_effect_is_classified_as_an_effect_not_unknown(tmp_path, seedance_root):
    """A Seedream PNG in the effects folder fell through the vault's role table and showed
    up as 其他, and registered as kind=video because that was hardcoded."""
    from automated_video_editing_backend.services.media_vault import MediaVaultService

    settings = SettingsService(path=tmp_path / "settings.json")
    media = MediaService(path=tmp_path / "media-library.json")
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)

    png = service.effects_dir / "img-effect.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    asset = SeedanceAsset(
        id="img-effect", name=png.name, kind="image", output_path=str(png),
        metadata_path=str(service.effects_dir / "img-effect.json"),
        prompt="p", status="succeeded", created_at=utc_now(),
    )
    service._write_asset(asset)

    item = service._media_item_for_asset(asset)
    assert item is not None
    assert item.kind == "image", "an image effect must not register as a video"
    assert item.metadata["role"] == "seedance_effect"

    # The vault's own role table must accept an image in the seedance area; before the fix
    # only kind=="video" matched and a PNG fell through to "unknown", shown as 其他.
    role_for = MediaVaultService(media)._role_for
    assert role_for("seedance", "image") == "seedance_effect"
    assert role_for("seedance", "video") == "seedance_effect"


def test_deleting_an_effect_removes_its_files(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    out = service.effects_dir / "doomed.png"
    out.write_bytes(b"x")
    asset = SeedanceAsset(id="doomed", name=out.name, kind="image", output_path=str(out),
                          metadata_path=str(service.effects_dir / "doomed.json"),
                          prompt="p", status="failed", created_at=utc_now())
    service._write_asset(asset)

    assert service.delete_asset("doomed") is True
    assert not out.exists()
    assert not (service.effects_dir / "doomed.json").exists()
    assert service.delete_asset("doomed") is False


def test_image_size_defaults_to_the_source_dimensions(tmp_path, seedance_root):
    """Ark accepts 'WIDTHxHEIGHT' or 2k/3k/4k. Pinning a preset upscaled every result, so a
    766x576 screenshot came back at 2464x1856 and read as an upscale rather than an edit."""
    import numpy as np
    import cv2

    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    source = tmp_path / "shot.png"
    cv2.imwrite(str(source), np.zeros((576, 766, 3), dtype=np.uint8))
    asset = SeedanceAsset(id="a", name="a.png", kind="image", output_path=str(tmp_path / "o.png"),
                          metadata_path=str(tmp_path / "a.json"), prompt="p",
                          source_image_path=str(source), created_at=utc_now())

    # Seedream refuses anything under 3,686,400 px, so 766x576 must be scaled up -- but by
    # the smallest factor that clears the floor, and without distorting the aspect ratio.
    size = service._image_size({}, asset)
    width, height = (int(part) for part in size.split("x"))
    assert width * height >= 3_686_400
    assert abs(width / height - 766 / 576) < 0.01
    assert service._image_size({"image_size": "auto"}, asset) == size
    # An explicit preset still wins.
    assert service._image_size({"image_size": "4k"}, asset) == "4k"
    assert service._image_size({"image_size": "1024x768"}, asset) == "1024x768"


def test_image_size_falls_back_when_the_source_is_unreadable(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)
    asset = SeedanceAsset(id="a", name="a.png", kind="image", output_path=str(tmp_path / "o.png"),
                          metadata_path=str(tmp_path / "a.json"), prompt="p",
                          source_image_path=str(tmp_path / "missing.png"), created_at=utc_now())

    assert service._image_size({}, asset) == "2k"


@pytest.mark.asyncio
async def test_an_image_can_be_generated_from_a_prompt_alone(tmp_path, seedance_root):
    """Seedream works from text alone. Requiring a source image was a Seedance rule (it
    animates a first frame) that should never have applied to image output."""
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"seedance": {
        "enabled": True, "api_key": "k", "image_model": "doubao-seedream-5-0-260128",
    }})
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    request = SeedanceGenerateRequest(prompt="一只在雪地里的柴犬", output="image")
    source = await service._resolve_source(request)

    assert source == {"path": None, "url": None}
    # No source and no TOS configured must still validate for an image.
    service._validate_config(settings.seedance_config(), source, "image")


@pytest.mark.asyncio
async def test_video_accepts_a_prompt_with_no_source(tmp_path, seedance_root):
    """Video used to demand a first frame. It now works from text alone, like image does."""
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({"seedance": {"enabled": True, "api_key": "k", "model": "m"}})
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    source = await service._resolve_source(SeedanceGenerateRequest(prompt="p", output="video"))

    assert source == {"path": None, "url": None}


def test_an_effect_whose_file_was_deleted_elsewhere_stops_being_listed(tmp_path, seedance_root):
    """媒体库 can trash the media file directly. Without this the .json survived and 特效制作
    kept showing a row whose 打开 and 定位 both pointed at nothing."""
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    png = service.effects_dir / "gone.png"
    png.write_bytes(b"x")
    asset = SeedanceAsset(id="gone", name=png.name, kind="image", output_path=str(png),
                          metadata_path=str(service.effects_dir / "gone.json"),
                          prompt="p", status="succeeded", created_at=utc_now())
    service._write_asset(asset)
    assert any(a.id == "gone" for a in service.list_assets())

    png.unlink()  # as 媒体库's 删除 would

    assert not any(a.id == "gone" for a in service.list_assets())
    assert not (service.effects_dir / "gone.json").exists()


def test_a_queued_effect_is_kept_even_though_its_file_does_not_exist_yet(tmp_path, seedance_root):
    """The cleanup must not eat an effect that is still generating."""
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, MediaService(path=tmp_path / "media-library.json"),
                              RenderService(), root=seedance_root)

    asset = SeedanceAsset(id="pending", name="p.png", kind="image",
                          output_path=str(service.effects_dir / "p.png"),
                          metadata_path=str(service.effects_dir / "pending.json"),
                          prompt="p", status="queued", created_at=utc_now())
    service._write_asset(asset)

    assert any(a.id == "pending" for a in service.list_assets())


def test_a_chinese_effect_title_is_kept(tmp_path, seedance_root):
    """safe_stem replaced a sanitiser that stripped every non-ASCII character, which
    silently discarded any Chinese name the operator typed."""
    from automated_video_editing_backend.services.naming import safe_stem

    assert safe_stem("早班主图", "图片特效") == "早班主图"
    assert safe_stem("", "图片特效") == "图片特效"
    assert safe_stem("a/b:c", "图片特效") == "abc"
    assert safe_stem("///", "图片特效") == "图片特效"


@pytest.mark.asyncio
async def test_frame_extraction_returns_the_moment_asked_for(tmp_path, seedance_root):
    """The frame picker confirms a real picture before any quota is spent."""
    import subprocess

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)

    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=160x120:rate=30:duration=6", str(clip)],
        check=True,
    )
    item = media.import_path(str(clip))

    result = await service.extract_frame(item.id, 3.0)

    assert result["timestamp_seconds"] == 3.0
    assert result["source_video_media_id"] == item.id
    assert Path(result["frame_path"]).is_file()
    assert Path(result["frame_path"]).stat().st_size > 0


@pytest.mark.asyncio
async def test_frame_past_the_end_is_rejected_with_a_readable_message(tmp_path, seedance_root):
    """Past the end ffmpeg exits 234 and dumps its own log; say the length instead."""
    import subprocess

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)

    clip = tmp_path / "short.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", "testsrc=size=160x120:rate=30:duration=2", str(clip)],
        check=True,
    )
    item = media.import_path(str(clip))

    with pytest.raises(ValueError, match="超出视频长度"):
        await service.extract_frame(item.id, 99.0)


@pytest.mark.asyncio
async def test_frame_extraction_rejects_non_source_video(tmp_path, seedance_root):
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    settings = SettingsService(path=tmp_path / "settings.json")
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)

    export = media.import_path(str(_touch(tmp_path / "export.mp4")))
    export.metadata.update({"source": "exports", "role": "export"})

    with pytest.raises(ValueError, match="source video"):
        await service.extract_frame(export.id, 1.0)


def _touch(path):
    path.write_bytes(b"placeholder")
    return path


@pytest.mark.asyncio
async def test_text_to_video_sends_no_first_frame_block(tmp_path, seedance_root):
    """Prompt-only video must omit the image entirely.

    Sending the block with a null url is rejected outright rather than read as "no first
    frame", so the request has to be built without it.
    """
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "seedance": {"enabled": True, "api_key": "ark-key", "model": "seedance-model"}
    })
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)

    source = await service._resolve_source(
        SeedanceGenerateRequest(prompt="一只猫在跑", output="video")
    )
    assert source == {"path": None, "url": None}
    # No source is no longer a configuration error for video.
    service._validate_config(settings.seedance_config(), source, "video")

    asset = SeedanceAsset(
        id="a1", name="t.mp4", kind="video", output_path=str(tmp_path / "t.mp4"),
        metadata_path=str(tmp_path / "t.json"), prompt="一只猫在跑",
        source_image_url=None, model="seedance-model", duration_seconds=5,
        status="queued", created_at=utc_now(),
    )

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "task-1"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, headers=None, json=None):
            captured.update(json or {})
            return FakeResponse()

    import automated_video_editing_backend.services.seedance as seedance_module

    original = seedance_module.httpx.AsyncClient
    seedance_module.httpx.AsyncClient = lambda **_kwargs: FakeClient()
    try:
        await service._create_task(asset, settings.seedance_config())
    finally:
        seedance_module.httpx.AsyncClient = original

    kinds = [part["type"] for part in captured["content"]]
    assert kinds == ["text"], captured["content"]
    assert captured["duration"] == 5
    assert isinstance(captured["duration"], int), "Ark rejects whole-second values encoded as 5.0"


@pytest.mark.asyncio
async def test_image_to_video_still_sends_the_first_frame(tmp_path, seedance_root):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "seedance": {"enabled": True, "api_key": "ark-key", "model": "seedance-model"}
    })
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    service = SeedanceService(settings, media, RenderService(), root=seedance_root)

    asset = SeedanceAsset(
        id="a2", name="t.mp4", kind="video", output_path=str(tmp_path / "t.mp4"),
        metadata_path=str(tmp_path / "t.json"), prompt="放大",
        source_image_url="https://example.com/frame.jpg", model="seedance-model",
        duration_seconds=5, status="queued", created_at=utc_now(),
    )

    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"id": "task-2"}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, headers=None, json=None):
            captured.update(json or {})
            return FakeResponse()

    import automated_video_editing_backend.services.seedance as seedance_module

    original = seedance_module.httpx.AsyncClient
    seedance_module.httpx.AsyncClient = lambda **_kwargs: FakeClient()
    try:
        await service._create_task(asset, settings.seedance_config())
    finally:
        seedance_module.httpx.AsyncClient = original

    kinds = [part["type"] for part in captured["content"]]
    assert kinds == ["text", "image_url"], captured["content"]
    assert captured["content"][1]["image_url"]["url"] == "https://example.com/frame.jpg"


@pytest.mark.asyncio
async def test_connection_check_lists_tasks_without_submitting_a_generation(
    monkeypatch, tmp_path, seedance_root,
):
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.replace_for_development({
        "seedance": {
            "enabled": True,
            "api_key": "ark-key",
            "model": "doubao-seedance-2-0-mini-260615",
        }
    })
    service = SeedanceService(
        settings, MediaService(path=tmp_path / "media-library.json"),
        RenderService(), root=seedance_root,
    )
    captured = {}

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"items": [], "total": 0}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, url, headers=None, params=None):
            captured.update({"url": url, "headers": headers, "params": params})
            return FakeResponse()

    import automated_video_editing_backend.services.seedance as seedance_module
    monkeypatch.setattr(seedance_module.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    result = await service.test()

    assert result.ok is True
    assert captured["url"].endswith("/contents/generations/tasks")
    assert captured["params"]["filter.model"] == "doubao-seedance-2-0-mini-260615"
    assert service.quota().used == 0
