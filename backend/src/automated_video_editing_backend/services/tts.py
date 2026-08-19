from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from automated_video_editing_backend.core.models import (
    ProviderTestResult,
    TTSAsset,
    TTSGenerateRequest,
    TTSGenerateResult,
    TTSQuota,
)
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services.naming import stamped_name
from automated_video_editing_backend.core.paths import ensure_inside_root, generated_path
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.settings import SettingsService

VOLCENGINE_SYNC_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"


class TTSService:
    def __init__(self, settings: SettingsService, media: MediaService) -> None:
        self.settings = settings
        self.media = media
        self.tts_dir = generated_path("data", "tts")
        self.tts_dir.mkdir(parents=True, exist_ok=True)
        self.usage_path = self.tts_dir / "usage.json"

    async def test(self) -> ProviderTestResult:
        cfg = self.settings.tts_config()
        if not self._configured(cfg):
            return ProviderTestResult(ok=False, provider="volcengine_sync", message="TTS settings are incomplete")
        try:
            _audio, timing = await self._request_sync_tts("你好，这是一次语音时间戳测试。")
            words = timing.get("words") or []
            return ProviderTestResult(
                ok=True,
                provider="volcengine_sync",
                message="TTS responded with timestamps" if words else "TTS responded without word timestamps",
                details={
                    "duration_ms": timing.get("duration_ms", 0),
                    "word_count": len(words),
                    "has_words": bool(words),
                },
            )
        except Exception as exc:
            return ProviderTestResult(ok=False, provider="volcengine_sync", message=str(exc))

    async def synthesize(self, request: TTSGenerateRequest, final_text: str) -> TTSGenerateResult:
        clean_text = self._clean_text(final_text)
        if not clean_text:
            raise ValueError("TTS text is empty after cleaning")
        quota = self.quota()
        if quota.remaining <= 0:
            raise ValueError(f"今日旁白生成已达上限（{quota.limit}），可在设置中调整每日上限")
        audio, timing = await self._request_sync_tts(clean_text)
        cfg = self.settings.tts_config()
        ext = ".wav" if cfg.get("encoding") == "wav" else ".mp3"
        base_name = self._asset_basename(request.title, clean_text)
        audio_path = ensure_inside_root(self.tts_dir / f"{base_name}{ext}")
        metadata_path = ensure_inside_root(self.tts_dir / f"{base_name}.json")
        audio_path.write_bytes(audio)

        metadata = {
            "title": request.title,
            "text": clean_text,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": timing.get("duration_ms", 0),
            "words": timing.get("words") or [],
            "phonemes": timing.get("phonemes") or [],
            "provider": "volcengine_sync",
            "voice_type": cfg.get("voice_type"),
            "cluster": cfg.get("cluster"),
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        media_item = self.media.register_generated_path(
            audio_path,
            kind="audio",
            metadata={
                "source": "data/tts",
                "role": "tts_voice",
                "metadata_path": str(metadata_path),
                "duration_ms": metadata["duration_ms"],
            },
        )
        # A voiceover was produced: spend one of today's allowance, whether or not the LLM
        # assist drafted the text. Only a successful synthesis counts, so a provider error
        # never burns the operator's quota.
        self._increment_usage(quota.date)
        asset = self._asset_from_files(audio_path, metadata_path, media_item.id)
        return TTSGenerateResult(
            media_item=media_item,
            asset=asset,
            words=metadata["words"],
            final_text=clean_text,
            quota=self.quota(),
        )

    def list_assets(self) -> list[TTSAsset]:
        assets: list[TTSAsset] = []
        for path in sorted(self.tts_dir.glob("*"), key=lambda item: item.stat().st_mtime, reverse=True):
            if path.suffix.lower() not in {".mp3", ".wav"}:
                continue
            metadata_path = path.with_suffix(".json")
            media_item = self.media.register_generated_path(
                path,
                kind="audio",
                metadata={"source": "data/tts", "role": "tts_voice", "metadata_path": str(metadata_path)},
            )
            assets.append(self._asset_from_files(path, metadata_path if metadata_path.exists() else None, media_item.id))
        return assets

    def quota(self) -> TTSQuota:
        """Today's voiceover allowance. The TTS analogue of SeedanceService.quota()."""
        cfg = self.settings.tts_config()
        limit = int(cfg.get("daily_limit") or 100)
        today = date.today().isoformat()
        used = int(self._read_usage().get(today, 0))
        return TTSQuota(date=today, used=used, limit=limit, remaining=max(0, limit - used))

    def _read_usage(self) -> dict[str, int]:
        data, _problem = read_json(self.usage_path)
        if not isinstance(data, dict):
            return {}
        usage: dict[str, int] = {}
        for key, value in data.items():
            try:
                usage[str(key)] = max(0, int(value))
            except (TypeError, ValueError):
                continue
        return usage

    def _increment_usage(self, day: str) -> None:
        """Spend one voiceover from the day's allowance.

        store.write_json writes whole and moves into place: a torn write would read back as
        nothing spent today, handing the whole day's budget back by accident.
        """
        usage = self._read_usage()
        usage[day] = int(usage.get(day, 0)) + 1
        write_json(self.usage_path, usage)

    async def _request_sync_tts(self, text: str) -> tuple[bytes, dict[str, Any]]:
        cfg = self.settings.tts_config()
        if not self._configured(cfg):
            raise ValueError("TTS settings are incomplete")
        payload = {
            "app": {"appid": cfg["app_id"], "cluster": cfg.get("cluster") or "volcano_tts"},
            "user": {"uid": "automated_video_editing"},
            "audio": {
                "voice_type": cfg["voice_type"],
                "encoding": cfg.get("encoding") or "mp3",
                "speed_ratio": float(cfg.get("speed_ratio") or 1.0),
                "volume_ratio": float(cfg.get("volume_ratio") or 1.0),
                "pitch_ratio": float(cfg.get("pitch_ratio") or 1.0),
            },
            "request": {
                "reqid": str(uuid.uuid4()),
                "text": text,
                "operation": "query",
                "with_timestamp": 1,
            },
        }
        headers = {"Content-Type": "application/json", "x-api-key": cfg["access_token"]}
        resource_id = self._resource_id(cfg)
        if resource_id:
            headers["X-Api-Resource-Id"] = resource_id
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                VOLCENGINE_SYNC_TTS_URL,
                headers=headers,
                json=payload,
            )
        if response.status_code >= 400:
            raise ValueError(f"TTS API error {response.status_code}: {response.text[:500]}")
        data = response.json()
        if data.get("code") != 3000:
            raise ValueError(f"TTS error {data.get('code')}: {data.get('message')}")
        audio = base64.b64decode(data.get("data") or "")
        addition = data.get("addition") or {}
        frontend = self._parse_frontend(addition.get("frontend"))
        words = frontend.get("words") or []
        phonemes = frontend.get("phonemes") or []
        duration_ms = self._duration_ms(addition, words)
        return audio, {"duration_ms": duration_ms, "words": words, "phonemes": phonemes}

    def _parse_frontend(self, raw: Any) -> dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
        return {}

    def _duration_ms(self, addition: dict[str, Any], words: list[dict[str, Any]]) -> int:
        try:
            return int(float(addition.get("duration") or 0))
        except (TypeError, ValueError):
            pass
        if words:
            try:
                return int(max(float(word.get("end_time", 0)) for word in words))
            except (TypeError, ValueError):
                return 0
        return 0

    def _asset_from_files(self, audio_path: Path, metadata_path: Path | None, media_id: str) -> TTSAsset:
        metadata: dict[str, Any] = {}
        if metadata_path and metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                metadata = {}
        stat = audio_path.stat()
        return TTSAsset(
            id=media_id,
            name=audio_path.name,
            audio_path=str(audio_path),
            metadata_path=str(metadata_path) if metadata_path else None,
            text=str(metadata.get("text") or ""),
            duration_ms=int(metadata.get("duration_ms") or 0),
            word_count=len(metadata.get("words") or []),
            created_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc),
        )

    def _asset_basename(self, title: str, text: str) -> str:
        """A readable, timestamped stem. The old text-hash + uuid form was unique but
        unreadable, and the audio and its .json are paired by this stem."""
        stem = (title or "").strip() or "旁白"
        taken = {path.stem for path in self.tts_dir.glob("*")}
        return Path(stamped_name(stem, "", taken)).name

    def _clean_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()

    def _configured(self, cfg: dict[str, Any]) -> bool:
        return bool(cfg.get("app_id") and cfg.get("access_token") and cfg.get("voice_type"))

    def _resource_id(self, cfg: dict[str, Any]) -> str:
        """The X-Api-Resource-Id a 声音复刻 (cloned) voice needs to behave fully.

        A standard voice needs no resource id, so the header is omitted and that path is left
        exactly as it was. A cloned voice on the ``volcano_icl`` cluster synthesises audio
        without it — but only *returns the per-word timestamps* (the ``addition.frontend``
        block) when the request names this resource, and without those timestamps subtitles
        cannot be built. ``volcano_icl`` maps to the standard clone model, ``volcano_icl_concurr``
        to the concurrent one. The voice's owning account must have the resource granted; a
        different account answers 3001 "resource not granted". An explicit ``resource_id`` in
        settings overrides the mapping for voices these rules do not anticipate.
        """
        explicit = str(cfg.get("resource_id") or "").strip()
        if explicit:
            return explicit
        cluster = str(cfg.get("cluster") or "").strip().lower()
        if cluster.startswith("volcano_icl"):
            return "volc.megatts.concurr" if cluster.endswith("concurr") else "volc.megatts.default"
        return ""
