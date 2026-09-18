from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from automated_video_editing_backend.core.models import (
    MediaItem,
    ProviderTestResult,
    SeedanceAsset,
    SeedanceGenerateRequest,
    SeedanceGenerateResult,
    SeedanceQuota,
    utc_now,
)
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.naming import safe_stem, stamped_name
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.settings import SettingsService

# Seedream refuses any output smaller than this, so small sources must be scaled up.
SEEDREAM_MIN_PIXELS = 3_686_400
# The provider charges every short video as at least five seconds. This is deliberately separate
# from the operator's preferred default duration: changing a 2-second default must not turn ten
# paid generations into twenty-five.
MINIMUM_BILLABLE_VIDEO_SECONDS = 5


def _tos_error_message(response: Any, cfg: dict[str, Any]) -> str:
    """Turn a TOS error response into something an operator can act on."""
    code = message = ""
    try:
        payload = response.json()
        code = str(payload.get("Code") or "")
        message = str(payload.get("Message") or "")
    except Exception:
        message = (response.text or "")[:200]

    bucket = str(cfg.get("tos_bucket") or "")
    region = str(cfg.get("tos_region") or "")
    if code == "NoSuchBucket":
        return f"TOS bucket '{bucket}' does not exist in {region}. Create it, or correct the bucket name in Settings."
    if code in {"SignatureDoesNotMatch", "InvalidAccessKeyId"}:
        return f"TOS rejected the credentials ({code}). Check the Access Key ID and Secret Access Key."
    if code == "AccessDenied":
        return f"TOS denied access to bucket '{bucket}'. The key may lack write permission."
    detail = f"{code}: {message}".strip(": ")
    return f"TOS upload failed ({response.status_code}){' - ' + detail if detail else ''}"


def _ark_error_message(response: Any, cfg: dict[str, Any]) -> str:
    """Turn an Ark error response into something an operator can act on."""
    code = message = ""
    try:
        error = response.json().get("error") or {}
        code = str(error.get("code") or "")
        message = str(error.get("message") or "")
    except Exception:
        message = (response.text or "")[:200]

    model = str(cfg.get("model") or cfg.get("image_model") or "")
    if code == "ModelNotOpen":
        return f"Ark model '{model}' is not activated for this account. Activate it in the Ark console."
    if code in {"AuthenticationError", "InvalidApiKey"}:
        return "Ark rejected the API key. Check the Seedance API key in Settings."
    detail = f"{code}: {message}".strip(": ")
    return f"Ark request failed ({response.status_code}){' - ' + detail if detail else ''}"


class SeedanceService:
    def __init__(
        self,
        settings: SettingsService,
        media: MediaService,
        renderer: RenderService,
        root: Path | None = None,
    ) -> None:
        self.settings = settings
        self.media = media
        self.renderer = renderer
        # Overridable so tests do not write effects and the daily quota counter into the
        # real data/seedance directory.
        self.root = root or generated_path("data", "seedance")
        self.effects_dir = self.root / "effects"
        self.cache_dir = self.root / "cache"
        self.usage_path = self.root / "usage.json"
        self.effects_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._usage_problem = ""

    def list_assets(self) -> list[SeedanceAsset]:
        assets: list[SeedanceAsset] = []
        for metadata_path in sorted(self.effects_dir.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            asset = self._load_asset(metadata_path)
            if asset is None:
                continue
            # 媒体库 can trash the media file directly, which would otherwise leave this
            # .json describing a file that no longer exists.
            if asset.status == "succeeded" and asset.output_path and not Path(asset.output_path).exists():
                metadata_path.unlink(missing_ok=True)
                continue
            if asset:
                assets.append(asset)
                if asset.status == "succeeded" and Path(asset.output_path).exists():
                    self.media.register_generated_path(
                        Path(asset.output_path),
                        kind=asset.kind,
                        metadata={"source": "data/seedance/effects", "role": "seedance_effect", "seedance_asset_id": asset.id},
                    )
        return assets

    def get_asset(self, asset_id: str) -> SeedanceAsset | None:
        return self._load_asset(self.effects_dir / f"{asset_id}.json")

    def delete_asset(self, asset_id: str) -> bool:
        """Remove an effect and its output file. Failed attempts leave clutter that cannot
        otherwise be cleared from the effects list."""
        metadata_path = self.effects_dir / f"{asset_id}.json"
        asset = self._load_asset(metadata_path)
        if asset is None:
            return False
        if asset.output_path:
            Path(asset.output_path).unlink(missing_ok=True)
        metadata_path.unlink(missing_ok=True)
        return True

    def quota(self) -> SeedanceQuota:
        cfg = self.settings.seedance_config()
        limit = int(cfg.get("daily_limit") or 10)
        default_duration = max(2, min(15, int(cfg.get("default_duration_seconds") or 5)))
        seconds_limit = limit * MINIMUM_BILLABLE_VIDEO_SECONDS
        today = date.today().isoformat()
        usage = self._read_usage()
        today_usage = usage.get(today, {})
        used = int(today_usage.get("count", 0))
        used_seconds = int(today_usage.get("video_seconds", 0))
        count_remaining = max(0, limit - used)
        remaining_seconds = max(0, seconds_limit - used_seconds)
        # ``remaining`` remains the simple number older clients display. It now reports how
        # many minimum-billed effects fit inside both limits, so one 15-second generation
        # correctly consumes three of a 10 x 5-second daily allowance while a 2-second
        # generation still consumes one full allowance.
        remaining = min(
            count_remaining,
            remaining_seconds // MINIMUM_BILLABLE_VIDEO_SECONDS,
        )
        return SeedanceQuota(
            date=today,
            used=used,
            limit=limit,
            remaining=remaining,
            count_remaining=count_remaining,
            used_seconds=used_seconds,
            seconds_limit=seconds_limit,
            remaining_seconds=remaining_seconds,
            default_duration_seconds=default_duration,
            minimum_billable_seconds=MINIMUM_BILLABLE_VIDEO_SECONDS,
        )

    async def test(self) -> ProviderTestResult:
        """Verify Ark authentication and the video-task API without buying a generation."""
        cfg = self.settings.seedance_config()
        try:
            self._validate_config(cfg, {"path": None, "url": None}, "video")
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(
                    f"{self._base_url(cfg)}/contents/generations/tasks",
                    headers=self._headers(cfg),
                    params={
                        "page_num": 1,
                        "page_size": 1,
                        "filter.model": str(cfg["model"]),
                    },
                )
            if response.status_code >= 400:
                raise ValueError(_ark_error_message(response, cfg))
            data = response.json()
            return ProviderTestResult(
                ok=True,
                provider="volcengine_ark",
                message="Seedance connection succeeded",
                details={
                    "model": str(cfg["model"]),
                    "task_count": int(data.get("total") or 0),
                    "billing": "No generation was submitted",
                },
            )
        except Exception as exc:
            return ProviderTestResult(
                ok=False,
                provider="volcengine_ark",
                message=str(exc),
                details={"model": str(cfg.get("model") or "")},
            )

    async def generate(self, request: SeedanceGenerateRequest) -> SeedanceGenerateResult:
        cfg = self.settings.seedance_config()
        kind = request.output
        suffix = ".mp4" if kind == "video" else ".png"
        source = await self._resolve_source(request)
        # Ark's video endpoint rejects JSON floats such as 4.0 even though the value is
        # mathematically whole. Normalise once and keep every downstream representation whole.
        duration = max(
            2,
            min(15, int(round(request.duration_seconds or cfg.get("default_duration_seconds") or 5))),
        )
        cache_key = self._cache_key(request.prompt, source["path"], source["url"], cfg, duration, kind)
        if request.reuse_existing:
            existing = self._find_existing(cache_key)
            if existing:
                return SeedanceGenerateResult(media_item=self._media_item_for_asset(existing), asset=existing, reused=True, quota=self.quota())

        quota = self.quota()
        if quota.count_remaining <= 0:
            raise ValueError(f"Seedance daily limit reached ({quota.limit})")
        billable_seconds = max(duration, MINIMUM_BILLABLE_VIDEO_SECONDS) if kind == "video" else 0
        if kind == "video" and billable_seconds > quota.remaining_seconds:
            raise ValueError(
                f"Seedance daily duration limit reached "
                f"({quota.remaining_seconds}s remaining, {billable_seconds}s requested)"
            )

        self._validate_config(cfg, source, kind)
        if not source.get("url") and source.get("path"):
            source["object_key"], source["url"] = await self._upload_source_to_tos(Path(source["path"]), cfg)
        # Uploading can yield to another request. Recheck immediately before the synchronous
        # usage write so two simultaneous submissions cannot both spend the same allowance.
        quota = self.quota()
        if quota.count_remaining <= 0:
            raise ValueError(f"Seedance daily limit reached ({quota.limit})")
        if kind == "video" and billable_seconds > quota.remaining_seconds:
            raise ValueError(
                f"Seedance daily duration limit reached "
                f"({quota.remaining_seconds}s remaining, {billable_seconds}s requested)"
            )
        self._increment_usage(quota.date, video_seconds=billable_seconds)
        quota = self.quota()

        asset_id = uuid4().hex
        # The .json keeps a uuid name: it is internal bookkeeping and is how an effect is
        # found by id. Only the media file gets a name a person would recognise.
        metadata_path = self.effects_dir / f"{asset_id}.json"
        taken = {path.name for path in self.effects_dir.glob("*")}
        default_prefix = "图片特效" if kind == "image" else "视频特效"
        friendly = safe_stem(request.title, default_prefix)
        output_file = self.effects_dir / stamped_name(friendly, suffix, taken)
        asset = SeedanceAsset(
            id=asset_id,
            name=output_file.name,
            kind=kind,
            output_path=str(output_file),
            metadata_path=str(metadata_path),
            prompt=request.prompt,
            source_image_path=source.get("path"),
            source_image_url=source.get("url"),
            source_object_key=source.get("object_key"),
            source_video_path=source.get("video_path"),
            timestamp_seconds=source.get("timestamp"),
            model=str((cfg.get("image_model") if kind == "image" else cfg.get("model")) or ""),
            duration_seconds=duration,
            status="queued",
            created_at=utc_now(),
        )
        self._write_asset(asset, extra={"cache_key": cache_key})
        asyncio.create_task(self._submit_and_download(asset.id))
        return SeedanceGenerateResult(asset=asset, reused=False, quota=quota)

    async def _resolve_source(self, request: SeedanceGenerateRequest) -> dict[str, Any]:
        # Both outputs can work from a prompt alone: Seedream generates an image from text and
        # Seedance generates a clip from text. A source picture is an option, not a requirement.
        if not (
            request.source_image_media_id or request.source_video_media_id or request.source_image_url
        ):
            return {"path": None, "url": None}

        if request.source_video_media_id:
            item = self.media.get(request.source_video_media_id)
            if not item or item.kind != "video" or item.metadata.get("role") != "raw_video":
                raise ValueError("Seedance source video must be a source video")
            timestamp = float(request.timestamp_seconds or 0)
            frame_path = await self._extract_frame(Path(item.path), timestamp)
            return {
                "path": str(frame_path),
                "url": request.source_image_url,
                "video_path": item.path,
                "timestamp": timestamp,
            }

        if request.source_image_media_id:
            item = self.media.get(request.source_image_media_id)
            if not item or item.kind != "image":
                raise ValueError("Seedance source image must be an image")
            image_path = self._copy_image_to_cache(Path(item.path))
            return {"path": str(image_path), "url": request.source_image_url}

        return {"path": None, "url": request.source_image_url}

    async def extract_frame(self, media_id: str, timestamp: float) -> dict[str, Any]:
        """Cut the frame the operator is looking at, and hand it back so they can confirm it."""
        item = self.media.get(media_id)
        if not item or item.kind != "video" or item.metadata.get("role") != "raw_video":
            raise ValueError("Seedance source video must be a source video")
        moment = max(0.0, float(timestamp or 0))
        # Asking past the end makes ffmpeg exit 234 with a wall of its own output. Checking the
        # length first turns that into something an operator can act on.
        length = self.renderer.probe_duration(item.path)
        if length and moment >= length:
            raise ValueError(f"这个时间点超出视频长度（{length:.1f} 秒），请换一个位置")
        frame_path = await self._extract_frame(Path(item.path), moment)
        return {
            "source_video_media_id": media_id,
            "timestamp_seconds": moment,
            "frame_path": str(frame_path),
        }

    async def _extract_frame(self, video_path: Path, timestamp: float) -> Path:
        source = Path(video_path).expanduser().resolve()
        if not source.exists():
            raise ValueError("Source video file does not exist")
        digest = hashlib.sha256(f"{source}:{timestamp:.3f}".encode("utf-8")).hexdigest()[:16]
        target = self.cache_dir / f"frame-{digest}.jpg"
        if target.exists():
            return target
        process = await asyncio.create_subprocess_exec(
            self.renderer.ffmpeg_binary(),
            "-y",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(target),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace")[-2000:])
        # ffmpeg can report success having written nothing; returning the path regardless would
        # push the failure downstream to the upload, where it reads as a storage error.
        if not target.exists():
            raise ValueError("这个时间点取不到画面，请换一个位置")
        return target

    def _copy_image_to_cache(self, image_path: Path) -> Path:
        source = Path(image_path).expanduser().resolve()
        if not source.exists():
            raise ValueError("Source image file does not exist")
        suffix = source.suffix.lower() or ".jpg"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        target = self.cache_dir / f"image-{digest}{suffix}"
        if not target.exists():
            target.write_bytes(source.read_bytes())
        return target

    def _validate_config(self, cfg: dict[str, Any], source: dict[str, Any], kind: str = "video") -> None:
        if not cfg.get("enabled"):
            raise ValueError("Seedance is disabled")
        if not cfg.get("api_key"):
            raise ValueError("Seedance API key is not configured")
        if kind == "image":
            if not cfg.get("image_model"):
                raise ValueError("Seedream image model is not configured")
        elif not cfg.get("model"):
            raise ValueError("Seedance model is not configured")
        if source.get("path") and not source.get("url"):
            self._validate_tos_config(cfg)

    def _validate_tos_config(self, cfg: dict[str, Any]) -> None:
        if not cfg.get("tos_bucket"):
            raise ValueError("Seedance TOS bucket is not configured")
        if not cfg.get("tos_region"):
            raise ValueError("Seedance TOS region is not configured")
        if not cfg.get("tos_endpoint"):
            raise ValueError("Seedance TOS endpoint is not configured")
        if not cfg.get("tos_access_key_id") or not cfg.get("tos_secret_access_key"):
            raise ValueError("Seedance TOS credentials are incomplete")

    async def _upload_source_to_tos(self, path: Path, cfg: dict[str, Any]) -> tuple[str, str]:
        source = Path(path).expanduser().resolve()
        if not source.exists():
            raise ValueError("Seedance source file does not exist")
        object_key = self._tos_object_key(source, cfg)
        host = self._tos_host(cfg)
        canonical_uri = "/" + quote(object_key, safe="/~")
        upload_url = f"https://{host}{canonical_uri}"
        body = source.read_bytes()
        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        headers = self._tos_authorization_headers(
            "PUT",
            object_key,
            cfg,
            hashlib.sha256(body).hexdigest(),
            extra_headers={"content-type": content_type},
        )
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.put(
                upload_url,
                content=body,
                headers=headers,
            )
        if response.status_code >= 400:
            # TOS explains itself in the body (NoSuchBucket, SignatureDoesNotMatch,
            # AccessDenied). raise_for_status discards that, turning a precise answer into
            # an opaque 500 in the UI.
            raise ValueError(_tos_error_message(response, cfg))

        download_url = self._presigned_tos_url("GET", object_key, cfg)
        return object_key, download_url

    def _tos_object_key(self, path: Path, cfg: dict[str, Any]) -> str:
        prefix = str(cfg.get("tos_object_prefix") or "seedance/staging").strip("/")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:24]
        suffix = path.suffix.lower() or ".jpg"
        date_key = date.today().strftime("%Y/%m/%d")
        return "/".join(part for part in [prefix, date_key, f"{digest}{suffix}"] if part)

    def _presigned_tos_url(self, method: str, object_key: str, cfg: dict[str, Any]) -> str:
        now = datetime.now(timezone.utc)
        current_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        expires = int(cfg.get("tos_url_expires_seconds") or 86400)
        access_key = str(cfg["tos_access_key_id"])
        credential_scope = f"{datestamp}/{cfg['tos_region']}/tos/request"
        signed_headers = "host"
        host = self._tos_host(cfg)
        credential = f"{access_key}/{credential_scope}"
        query: dict[str, str] = {
            "X-Tos-Algorithm": "TOS4-HMAC-SHA256",
            "X-Tos-Credential": credential,
            "X-Tos-Date": current_date,
            "X-Tos-Expires": str(expires),
            "X-Tos-SignedHeaders": signed_headers,
        }
        token = str(cfg.get("tos_security_token") or "")
        if token:
            query["X-Tos-Security-Token"] = token
        canonical_query = self._canonical_query(query)
        canonical_uri = "/" + quote(object_key, safe="/~")
        canonical_headers = f"host:{host}\n"
        # A presigned URL is signed before any body exists, so the payload hash is the
        # literal UNSIGNED-PAYLOAD. Signing an empty-body digest instead produced a URL TOS
        # always rejected with SignatureDoesNotMatch, which meant every staged image was
        # uploaded successfully and then undownloadable by Ark.
        payload_hash = "UNSIGNED-PAYLOAD"
        canonical_request = "\n".join([
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        string_to_sign = "\n".join([
            "TOS4-HMAC-SHA256",
            current_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(
            self._tos_signing_key(str(cfg["tos_secret_access_key"]), datestamp, str(cfg["tos_region"])),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"https://{host}{canonical_uri}?{canonical_query}&X-Tos-Signature={signature}"

    def _tos_authorization_headers(
        self,
        method: str,
        object_key: str,
        cfg: dict[str, Any],
        payload_hash: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        current_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        host = self._tos_host(cfg)
        headers = {
            "host": host,
            "x-tos-content-sha256": payload_hash,
            "x-tos-date": current_date,
        }
        for key, value in (extra_headers or {}).items():
            headers[key.lower()] = str(value).strip()
        token = str(cfg.get("tos_security_token") or "")
        if token:
            headers["x-tos-security-token"] = token
        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{key}:{headers[key]}\n" for key in sorted(headers))
        canonical_request = "\n".join([
            method.upper(),
            "/" + quote(object_key, safe="/~"),
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{datestamp}/{cfg['tos_region']}/tos/request"
        string_to_sign = "\n".join([
            "TOS4-HMAC-SHA256",
            current_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])
        signature = hmac.new(
            self._tos_signing_key(str(cfg["tos_secret_access_key"]), datestamp, str(cfg["tos_region"])),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers["authorization"] = (
            "TOS4-HMAC-SHA256 "
            f"Credential={cfg['tos_access_key_id']}/{credential_scope},"
            f"SignedHeaders={signed_headers},"
            f"Signature={signature}"
        )
        return headers

    def _canonical_query(self, query: dict[str, str]) -> str:
        pairs = []
        for key, value in sorted(query.items()):
            pairs.append(f"{quote(str(key), safe='~')}={quote(str(value), safe='~')}")
        return "&".join(pairs)

    def _tos_signing_key(self, secret_key: str, datestamp: str, region: str) -> bytes:
        key = hmac.new(secret_key.encode("utf-8"), datestamp.encode("utf-8"), hashlib.sha256).digest()
        key = hmac.new(key, region.encode("utf-8"), hashlib.sha256).digest()
        key = hmac.new(key, b"tos", hashlib.sha256).digest()
        return hmac.new(key, b"request", hashlib.sha256).digest()

    def _tos_host(self, cfg: dict[str, Any]) -> str:
        bucket = str(cfg.get("tos_bucket") or "").strip()
        endpoint = str(cfg.get("tos_endpoint") or "tos-cn-beijing.volces.com").strip()
        endpoint = endpoint.removeprefix("https://").removeprefix("http://").strip("/")
        if endpoint.startswith(f"{bucket}."):
            return endpoint
        return f"{bucket}.{endpoint}"

    async def _submit_and_download(self, asset_id: str) -> None:
        asset = self._load_asset(self.effects_dir / f"{asset_id}.json")
        if not asset:
            return
        cfg = self.settings.seedance_config()
        try:
            if asset.kind == "image":
                # Seedream returns the image directly: no task to poll.
                image_url = await self._create_image(asset, cfg)
                await self._download_video(image_url, Path(asset.output_path))
                asset.status = "succeeded"
                asset.error = None
                self._write_asset(asset)
                self._media_item_for_asset(asset)
                return

            task_id = await self._create_task(asset, cfg)
            asset.task_id = task_id
            asset.status = "running"
            self._write_asset(asset)
            video_url = await self._wait_for_video_url(task_id, cfg)
            await self._download_video(video_url, Path(asset.output_path))
            asset.status = "succeeded"
            asset.error = None
            self._write_asset(asset)
            self._media_item_for_asset(asset)
        except Exception as exc:
            asset.status = "failed"
            asset.error = str(exc)
            self._write_asset(asset)

    async def _create_image(self, asset: SeedanceAsset, cfg: dict[str, Any]) -> str:
        """Seedream image-to-image. Synchronous, unlike the video task queue."""
        payload: dict[str, Any] = {
            "model": cfg["image_model"],
            "prompt": asset.prompt,
            "size": await asyncio.to_thread(self._image_size, cfg, asset),
            "response_format": "url",
        }
        if asset.source_image_url:
            payload["image"] = asset.source_image_url

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{self._base_url(cfg)}/images/generations",
                headers=self._headers(cfg),
                json=payload,
            )
        if response.status_code >= 400:
            raise ValueError(_ark_error_message(response, cfg))

        data = response.json().get("data") or []
        url = next((entry.get("url") for entry in data if isinstance(entry, dict) and entry.get("url")), "")
        if not url:
            raise ValueError("Seedream returned no image url")
        return str(url)

    def _image_size(self, cfg: dict[str, Any], asset: SeedanceAsset) -> str:
        """The output size Seedream should render.

        Ark accepts 'WIDTHxHEIGHT', '2k', '3k' or '4k', and refuses anything under
        SEEDREAM_MIN_PIXELS. A 766x576 screenshot is far below that, so some upscaling is
        unavoidable. Left on auto we scale the source up by the smallest factor that clears
        the floor, keeping its aspect ratio, rather than jumping to a preset -- a preset
        enlarges more than necessary and makes the result read as an upscale, not an edit.
        """
        configured = str(cfg.get("image_size") or "").strip().lower()
        if configured and configured not in {"auto", "adaptive", "source", "跟随原图"}:
            return configured

        source = asset.source_image_path
        if source and Path(source).exists():
            try:
                size = self.renderer.probe_frame_size(source)
                if size is not None:
                    width, height = size
                    if width > 0 and height > 0:
                        scale = max(1.0, (SEEDREAM_MIN_PIXELS / (width * height)) ** 0.5)
                        out_w, out_h = int(width * scale + 0.5), int(height * scale + 0.5)
                        # Rounding can land a pixel under the floor; nudge until it clears.
                        while out_w * out_h < SEEDREAM_MIN_PIXELS:
                            out_w += 1
                            out_h = int(out_w * height / width + 0.5)
                        return f"{out_w}x{out_h}"
            except Exception:
                pass
        return "2k"

    async def _create_task(self, asset: SeedanceAsset, cfg: dict[str, Any]) -> str:
        duration = int(round(asset.duration_seconds))
        prompt = self._prompt_with_generation_flags(asset.prompt, cfg, duration)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        # Text-to-video sends no picture at all. Including the block with a null url would be
        # rejected outright rather than read as "no first frame".
        if asset.source_image_url:
            content.append(
                {"type": "image_url", "image_url": {"url": asset.source_image_url}, "role": "first_frame"}
            )
        payload = {
            "model": cfg["model"],
            "content": content,
            "duration": duration,
            "resolution": cfg.get("resolution") or "720p",
            "ratio": cfg.get("ratio") or "16:9",
        }
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self._base_url(cfg)}/contents/generations/tasks",
                headers=self._headers(cfg),
                json=payload,
            )
        if response.status_code >= 400:
            # Ark names the problem precisely (ModelNotOpen, InvalidParameter). Letting
            # raise_for_status swallow it turned "activate the model" into a bare 404.
            raise ValueError(_ark_error_message(response, cfg))
        data = response.json()
        task_id = data.get("id") or data.get("task_id") or data.get("data", {}).get("id")
        if not task_id:
            raise ValueError("Seedance task response did not include an id")
        return str(task_id)

    async def _wait_for_video_url(self, task_id: str, cfg: dict[str, Any]) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            for _ in range(90):
                response = await client.get(
                    f"{self._base_url(cfg)}/contents/generations/tasks/{task_id}",
                    headers=self._headers(cfg),
                )
                if response.status_code >= 400:
                    raise ValueError(_ark_error_message(response, cfg))
                data = response.json()
                status = self._task_status(data)
                if status == "succeeded":
                    video_url = self._video_url(data)
                    if video_url:
                        return video_url
                    raise ValueError("Seedance succeeded without video_url")
                if status in {"failed", "cancelled", "canceled", "expired"}:
                    raise ValueError(self._task_error(data) or "Seedance task failed")
                await asyncio.sleep(5)
        raise TimeoutError("Seedance task timed out")

    async def _download_video(self, url: str, target: Path) -> None:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            handle.write(chunk)

    def _media_item_for_asset(self, asset: SeedanceAsset) -> MediaItem | None:
        if asset.status != "succeeded" or not Path(asset.output_path).exists():
            return None
        return self.media.register_generated_path(
            Path(asset.output_path),
            kind=asset.kind,
            metadata={"source": "data/seedance/effects", "role": "seedance_effect", "seedance_asset_id": asset.id},
        )

    def _find_existing(self, cache_key: str) -> SeedanceAsset | None:
        for metadata_path in self.effects_dir.glob("*.json"):
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            if data.get("cache_key") != cache_key:
                continue
            asset = self._load_asset(metadata_path)
            if asset and asset.status == "succeeded" and Path(asset.output_path).exists():
                return asset
        return None

    def _load_asset(self, metadata_path: Path) -> SeedanceAsset | None:
        try:
            data = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        allowed = {name for name in SeedanceAsset.model_fields}
        return SeedanceAsset(**{key: value for key, value in data.items() if key in allowed})

    def _write_asset(self, asset: SeedanceAsset, extra: dict[str, Any] | None = None) -> None:
        data = asset.model_dump(mode="json")
        if extra:
            data.update(extra)
        Path(asset.metadata_path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _cache_key(self, prompt: str, path: str | None, url: str | None, cfg: dict[str, Any], duration: float, kind: str = "video") -> str:
        model = cfg.get("image_model") if kind == "image" else cfg.get("model")
        parts = [kind, prompt, str(model or ""), str(duration), str(cfg.get("resolution") or ""), str(cfg.get("ratio") or "")]
        if path and Path(path).exists():
            parts.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        else:
            parts.append(str(url or ""))
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()

    def _read_usage(self) -> dict[str, dict[str, int]]:
        if self._usage_problem:
            raise RuntimeError(self._usage_problem)
        data, problem = read_json(self.usage_path)
        if problem:
            self._fail_usage(problem)
        if data is None:
            quarantined = sorted(self.usage_path.parent.glob(f"{self.usage_path.name}.corrupt-*"))
            if quarantined:
                self._fail_usage(f"检测到已保留的损坏文件 {quarantined[-1].name}")
            return {}
        if not isinstance(data, dict):
            self._fail_usage("文件格式无效：顶层内容应为对象")
        usage: dict[str, dict[str, int]] = {}
        for key, value in data.items():
            try:
                if isinstance(value, dict):
                    count = int(value.get("count", 0))
                    video_seconds = int(value.get("video_seconds", 0))
                else:
                    # Old releases stored only a count. Treat every old generation as one
                    # minimum-billed effect: this preserves the allowance already spent today.
                    count = int(value)
                    video_seconds = count * MINIMUM_BILLABLE_VIDEO_SECONDS
            except (TypeError, ValueError):
                self._fail_usage(f"文件格式无效：{key!s} 的用量不是整数")
            if count < 0 or video_seconds < 0:
                self._fail_usage(f"文件格式无效：{key!s} 的用量不能为负数")
            usage[str(key)] = {"count": count, "video_seconds": video_seconds}
        return usage

    def _increment_usage(self, day: str, video_seconds: int = 0) -> None:
        """Spend one generation and, for video, its actual duration allowance.

        Written whole and moved into place. These limits stand between the operator and a paid
        API, and a torn write would read back as unparseable, which reads as nothing spent
        today — handing back the whole day's budget by accident.
        """
        usage = self._read_usage()
        today = usage.get(day, {"count": 0, "video_seconds": 0})
        usage[day] = {
            "count": int(today.get("count", 0)) + 1,
            "video_seconds": int(today.get("video_seconds", 0)) + max(0, int(video_seconds)),
        }
        if not write_json(self.usage_path, usage):
            self._fail_usage(f"{self.usage_path.name} 无法保存")

    def _fail_usage(self, detail: str) -> None:
        self._usage_problem = f"特效额度记录不可用，已停止生成以避免重复消费：{detail}"
        raise RuntimeError(self._usage_problem)

    def _base_url(self, cfg: dict[str, Any]) -> str:
        return str(cfg.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

    def _headers(self, cfg: dict[str, Any]) -> dict[str, str]:
        return {"authorization": f"Bearer {cfg['api_key']}", "content-type": "application/json"}

    def _task_status(self, data: dict[str, Any]) -> str:
        return str(data.get("status") or data.get("data", {}).get("status") or "").lower()

    def _task_error(self, data: dict[str, Any]) -> str | None:
        return data.get("error") or data.get("message") or data.get("data", {}).get("error") or data.get("data", {}).get("message")

    def _video_url(self, data: dict[str, Any]) -> str | None:
        content = data.get("content") or data.get("data", {}).get("content") or {}
        if isinstance(content, dict):
            return self._url_from_content_item(content)
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    url = self._url_from_content_item(item)
                    if url:
                        return url
        return None

    def _url_from_content_item(self, item: dict[str, Any]) -> str | None:
        direct_url = item.get("url")
        if isinstance(direct_url, str):
            return direct_url
        video_url = item.get("video_url")
        if isinstance(video_url, str):
            return video_url
        if isinstance(video_url, dict) and isinstance(video_url.get("url"), str):
            return video_url["url"]
        return None

    def _prompt_with_generation_flags(self, prompt: str, cfg: dict[str, Any], duration: float) -> str:
        flags: list[str] = []
        if "--ratio" not in prompt and cfg.get("ratio"):
            flags.append(f"--ratio {cfg['ratio']}")
        if "--dur" not in prompt:
            flags.append(f"--dur {int(round(duration))}")
        return " ".join([prompt.strip(), *flags]).strip()
