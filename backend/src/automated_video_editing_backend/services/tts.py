from __future__ import annotations

import asyncio
import base64
import binascii
import json
import math
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
from automated_video_editing_backend.core.paths import ensure_inside_root, generated_path
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.naming import stamped_name
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.subtitles import restore_source_spelling

VOLCENGINE_SYNC_TTS_URL = "https://openspeech.bytedance.com/api/v1/tts"


class TTSService:
    def __init__(self, settings: SettingsService, media: MediaService) -> None:
        self.settings = settings
        self.media = media
        self.tts_dir = generated_path("data", "tts")
        self.tts_dir.mkdir(parents=True, exist_ok=True)
        self.usage_path = self.tts_dir / "usage.json"
        # The allowance check and the paid provider call are one operation. Without this lock,
        # two requests seeing the last remaining slot could both call the provider before either
        # one recorded its spend.
        self._quota_lock = asyncio.Lock()
        self._usage_problem = ""

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
        async with self._quota_lock:
            return await self._synthesize_locked(request, clean_text)

    async def _synthesize_locked(
        self,
        request: TTSGenerateRequest,
        clean_text: str,
    ) -> TTSGenerateResult:
        cfg = self.settings.tts_config()
        if not self._configured(cfg):
            raise ValueError("TTS settings are incomplete")
        quota = self.quota()
        if quota.remaining <= 0:
            raise ValueError(f"今日旁白生成已达上限（{quota.limit}），可在设置中调整每日上限")
        # The reservation is the durable cost boundary. From this point onward the provider may
        # have accepted a billable request, so success, failure, cancellation, and restart all
        # keep the slot counted. ``finally`` only settles its pending marker; it never refunds it.
        reservation_id = self._reserve_usage(quota.date)
        try:
            audio, timing = await self._request_sync_tts(clean_text)
            if not isinstance(audio, (bytes, bytearray)) or not audio:
                raise ValueError("语音服务未返回可用音频，未生成旁白")
            words = restore_source_spelling(
                timing.get("words") or [],
                clean_text,
                duration_ms=timing.get("duration_ms"),
            )
            ext = ".wav" if cfg.get("encoding") == "wav" else ".mp3"
            base_name = self._asset_basename(request.title, clean_text)
            audio_path = ensure_inside_root(self.tts_dir / f"{base_name}{ext}")
            metadata_path = ensure_inside_root(self.tts_dir / f"{base_name}.json")
            audio_stage = ensure_inside_root(
                self.tts_dir / f".{audio_path.name}.{uuid.uuid4().hex}.tmp"
            )

            metadata = {
                "title": request.title,
                "text": clean_text,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "duration_ms": timing.get("duration_ms", 0),
                "words": words,
                "phonemes": timing.get("phonemes") or [],
                "provider": "volcengine_sync",
                "voice_type": cfg.get("voice_type"),
                "cluster": cfg.get("cluster"),
            }
            try:
                # A .tmp suffix keeps a half-written response out of list_assets after a crash.
                # Publish the audio only after its reviewed text and exact timing are durable.
                audio_stage.write_bytes(audio)
                if not write_json(metadata_path, metadata):
                    raise RuntimeError(f"旁白文字和时间数据无法保存：{metadata_path.name}")
                audio_stage.replace(audio_path)
            except Exception:
                audio_stage.unlink(missing_ok=True)
                audio_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                raise
            try:
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
            except Exception:
                audio_path.unlink(missing_ok=True)
                metadata_path.unlink(missing_ok=True)
                try:
                    self.media.refresh_generated_media()
                except Exception:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001 - preserve each provider/persistence error type
            # The caller otherwise sees only the provider/persistence failure and may retry while
            # the screen still shows its old quota snapshot. Keep the original exception class
            # (and therefore the route's HTTP status) while making the no-refund boundary clear.
            counted_note = "（本次已计入今日旁白次数）"
            if counted_note not in str(exc):
                if exc.args and isinstance(exc.args[0], str):
                    exc.args = (f"{exc.args[0]}{counted_note}", *exc.args[1:])
                else:
                    exc.args = (f"{exc!s}{counted_note}",)
            raise
        finally:
            self._settle_usage(quota.date, reservation_id)
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
        today_usage = self._read_usage().get(today, {"used": 0, "pending": []})
        used = int(today_usage["used"])
        pending = len(today_usage["pending"])
        return TTSQuota(
            date=today,
            used=used,
            limit=limit,
            remaining=max(0, limit - used),
            pending=pending,
        )

    def _read_usage(self) -> dict[str, dict[str, Any]]:
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
        usage: dict[str, dict[str, Any]] = {}
        for key, value in data.items():
            if isinstance(value, dict):
                unknown = set(value) - {"used", "pending"}
                if unknown:
                    self._fail_usage(f"文件格式无效：{key!s} 含有未知字段")
                raw_count = value.get("used")
                raw_pending = value.get("pending", [])
            else:
                # The original on-disk format was ``{date: integer}``. Keep accepting it and
                # serialise settled days back to that exact shape for downgrade/readability.
                raw_count = value
                raw_pending = []
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                self._fail_usage(f"文件格式无效：{key!s} 的用量不是整数")
            if count < 0:
                self._fail_usage(f"文件格式无效：{key!s} 的用量不能为负数")
            if not isinstance(raw_pending, list):
                self._fail_usage(f"文件格式无效：{key!s} 的待结算记录应为列表")
            pending: list[str] = []
            for reservation_id in raw_pending:
                if not isinstance(reservation_id, str) or not reservation_id.strip():
                    self._fail_usage(f"文件格式无效：{key!s} 含有无效待结算记录")
                pending.append(reservation_id)
            if len(set(pending)) != len(pending):
                self._fail_usage(f"文件格式无效：{key!s} 含有重复待结算记录")
            if len(pending) > count:
                self._fail_usage(f"文件格式无效：{key!s} 的待结算数超过已用次数")
            usage[str(key)] = {"used": count, "pending": pending}
        return usage

    @staticmethod
    def _usage_payload(usage: dict[str, dict[str, Any]]) -> dict[str, int | dict[str, Any]]:
        payload: dict[str, int | dict[str, Any]] = {}
        for day, entry in usage.items():
            used = int(entry["used"])
            pending = list(entry["pending"])
            payload[day] = {"used": used, "pending": pending} if pending else used
        return payload

    def _write_usage(self, usage: dict[str, dict[str, Any]]) -> None:
        if not write_json(self.usage_path, self._usage_payload(usage)):
            self._fail_usage(f"{self.usage_path.name} 无法保存")

    def _increment_usage(self, day: str) -> None:
        """Spend one voiceover from the day's allowance.

        store.write_json writes whole and moves into place: a torn write would read back as
        nothing spent today, handing the whole day's budget back by accident.
        """
        usage = self._read_usage()
        entry = usage.setdefault(day, {"used": 0, "pending": []})
        entry["used"] = int(entry["used"]) + 1
        self._write_usage(usage)

    def _reserve_usage(self, day: str) -> str:
        """Durably count a provider attempt and mark it pending before network I/O."""
        usage = self._read_usage()
        entry = usage.setdefault(day, {"used": 0, "pending": []})
        reservation_id = uuid.uuid4().hex
        entry["used"] = int(entry["used"]) + 1
        entry["pending"].append(reservation_id)
        self._write_usage(usage)
        return reservation_id

    def _settle_usage(self, day: str, reservation_id: str) -> None:
        """Clear an attempt's pending marker without refunding its already-counted slot."""
        usage = self._read_usage()
        entry = usage.get(day)
        if entry is None or reservation_id not in entry["pending"]:
            self._fail_usage("待结算的旁白额度记录丢失")
        entry["pending"].remove(reservation_id)
        self._write_usage(usage)

    def _fail_usage(self, detail: str) -> None:
        self._usage_problem = f"旁白额度记录不可用，已停止生成以避免重复消费：{detail}"
        raise RuntimeError(self._usage_problem)

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
        encoded_audio = data.get("data")
        if not isinstance(encoded_audio, str) or not encoded_audio.strip():
            raise ValueError("语音服务未返回音频数据")
        try:
            audio = base64.b64decode(encoded_audio.strip(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("语音服务返回的音频数据无效") from exc
        if not audio:
            raise ValueError("语音服务未返回可用音频")
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
            duration = float(addition.get("duration"))
        except (TypeError, ValueError):
            duration = 0.0
        if math.isfinite(duration) and duration > 0:
            return int(duration)
        valid_ends: list[float] = []
        for word in words:
            if not isinstance(word, dict):
                continue
            try:
                end = float(word.get("end_time"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(end) and end > 0:
                valid_ends.append(end)
        return int(max(valid_ends, default=0.0))

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
