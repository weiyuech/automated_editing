from __future__ import annotations

import json
import os
from datetime import date, timedelta
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from automated_video_editing_backend.core.models import (
    LLMSettingsSummary,
    AutomationSettingsSummary,
    CameraworkConfig,
    RobotSettingsSummary,
    SeedanceSettingsSummary,
    SecretStatus,
    SettingsSummary,
    SettingsUpdateRequest,
    TTSSettingsSummary,
)
from automated_video_editing_backend.core.paths import generated_path


DEFAULT_SETTINGS: dict[str, Any] = {
    "llm": {
        "enabled": False,
        "provider": "doubao",
        "api_key": "",
        "model": "",
        "timeout_ms": 20000,
    },
    "tts": {
        "enabled": False,
        "provider": "volcengine_sync",
        "app_id": "",
        "access_token": "",
        "voice_type": "BV001_streaming",
        "cluster": "volcano_tts",
        "encoding": "mp3",
        "speed_ratio": 1.0,
        "volume_ratio": 1.0,
        "pitch_ratio": 1.0,
        "with_timestamp": True,
        "daily_limit": 100,
    },
    "seedance": {
        "enabled": False,
        "provider": "volcengine_ark",
        "api_key": "",
        "model": "",
        "image_model": "",
        "image_size": "",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "tos_access_key_id": "",
        "tos_secret_access_key": "",
        "tos_security_token": "",
        "tos_bucket": "",
        "tos_region": "cn-beijing",
        "tos_endpoint": "tos-cn-beijing.volces.com",
        "tos_object_prefix": "seedance/staging",
        "tos_url_expires_seconds": 86400,
        "daily_limit": 10,
        "default_duration_seconds": 5,
        "resolution": "720p",
        "ratio": "16:9",
    },
    "robot": {
        "websocket_url": "",
    },
    # Not robot configuration, but it lives beside it because this is where an operator goes
    # to set the limits the app runs under rather than the ones a single job runs under.
    "automation": {
        "daily_output_limit": 100,
        "output_aspect_ratio": None,
        "framing_configured": False,
        "framing_mode": "center",
        "framing_crop_x": 0.5,
        "framing_crop_y": 0.5,
        "camerawork": CameraworkConfig().model_dump(mode="json"),
    },
}


class SettingsService:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or generated_path("data", "settings.local.json")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._settings = self._load()

    def summary(self) -> SettingsSummary:
        llm = self.llm_config()
        tts = self.tts_config()
        seedance = self.seedance_config()
        robot = self.robot_config()
        return SettingsSummary(
            settings_path=str(self.path),
            automation=self.output_quota(),
            llm=LLMSettingsSummary(
                enabled=bool(llm.get("enabled")),
                provider=llm.get("provider", "doubao"),
                model=str(llm.get("model") or ""),
                timeout_ms=int(llm.get("timeout_ms") or 20000),
                api_key=self._secret_status(str(llm.get("api_key") or "")),
            ),
            tts=TTSSettingsSummary(
                enabled=bool(tts.get("enabled")),
                provider=tts.get("provider", "volcengine_sync"),
                app_id=self._secret_status(str(tts.get("app_id") or "")),
                access_token=self._secret_status(str(tts.get("access_token") or "")),
                voice_type=str(tts.get("voice_type") or ""),
                cluster=str(tts.get("cluster") or "volcano_tts"),
                encoding=tts.get("encoding", "mp3"),
                speed_ratio=float(tts.get("speed_ratio") or 1.0),
                volume_ratio=float(tts.get("volume_ratio") or 1.0),
                pitch_ratio=float(tts.get("pitch_ratio") or 1.0),
                with_timestamp=True,
                daily_limit=int(tts.get("daily_limit") or 100),
            ),
            seedance=SeedanceSettingsSummary(
                enabled=bool(seedance.get("enabled")),
                provider=seedance.get("provider", "volcengine_ark"),
                api_key=self._secret_status(str(seedance.get("api_key") or "")),
                model=str(seedance.get("model") or ""),
                image_model=str(seedance.get("image_model") or ""),
                image_size=str(seedance.get("image_size") or ""),
                base_url=str(seedance.get("base_url") or "https://ark.cn-beijing.volces.com/api/v3"),
                tos_access_key_id=self._secret_status(str(seedance.get("tos_access_key_id") or "")),
                tos_secret_access_key=self._secret_status(str(seedance.get("tos_secret_access_key") or "")),
                tos_security_token=self._secret_status(str(seedance.get("tos_security_token") or "")),
                tos_bucket=str(seedance.get("tos_bucket") or ""),
                tos_region=str(seedance.get("tos_region") or "cn-beijing"),
                tos_endpoint=str(seedance.get("tos_endpoint") or "tos-cn-beijing.volces.com"),
                tos_object_prefix=str(seedance.get("tos_object_prefix") or "seedance/staging"),
                tos_url_expires_seconds=int(seedance.get("tos_url_expires_seconds") or 86400),
                daily_limit=int(seedance.get("daily_limit") or 10),
                default_duration_seconds=int(seedance.get("default_duration_seconds") or 5),
                resolution=str(seedance.get("resolution") or "720p"),
                ratio=str(seedance.get("ratio") or "16:9"),
            ),
            robot=RobotSettingsSummary(
                enabled=bool(robot.get("websocket_url")),
                websocket_url=str(robot.get("websocket_url") or ""),
            ),
        )

    def llm_config(self) -> dict[str, Any]:
        return deepcopy(self._settings["llm"])

    def tts_config(self) -> dict[str, Any]:
        return deepcopy(self._settings["tts"])

    def seedance_config(self) -> dict[str, Any]:
        return deepcopy(self._settings["seedance"])

    def robot_config(self) -> dict[str, Any]:
        return deepcopy(self._settings["robot"])

    def automation_config(self) -> dict[str, Any]:
        return deepcopy(self._settings["automation"])

    def camerawork_config(self) -> CameraworkConfig:
        """Return one validated snapshot for a cruise to keep for its whole run."""
        raw = self.automation_config().get("camerawork")
        try:
            return CameraworkConfig.model_validate(raw if isinstance(raw, dict) else {})
        except ValidationError:
            # An operator must be able to open 镜头设置 and replace a bad hand-edited profile;
            # surfacing it as unconfigured is safer than making the whole settings API fail.
            return CameraworkConfig()

    def output_quota(self) -> AutomationSettingsSummary:
        """How many more videos may be produced today.

        A day's worth of output is a property of the app, not of any one batch, so it is held
        here beside the other limits rather than inside the job service — and on disk, because
        a limit that resets whenever the process restarts is not a limit.
        """
        config = self.automation_config()
        limit = int(config.get("daily_output_limit") or 100)
        used = int(self._read_output_usage().get(date.today().isoformat(), 0))
        configured = bool(config.get("framing_configured"))
        ratio = str(config.get("output_aspect_ratio") or "")
        if not configured or ratio not in {"16:9", "9:16"}:
            ratio = None
        return AutomationSettingsSummary(
            daily_output_limit=limit,
            used_today=used,
            remaining_today=max(0, limit - used),
            output_aspect_ratio=ratio,
            framing_configured=configured and ratio is not None,
            framing_mode=(
                str(config.get("framing_mode"))
                if str(config.get("framing_mode")) in {"center", "custom"}
                else "center"
            ),
            framing_crop_x=max(0.0, min(1.0, float(config.get("framing_crop_x", 0.5)))),
            framing_crop_y=max(0.0, min(1.0, float(config.get("framing_crop_y", 0.5)))),
            camerawork=self.camerawork_config(),
        )

    def record_outputs(self, count: int) -> None:
        if count <= 0:
            return
        today = date.today().isoformat()
        usage = self._read_output_usage()
        usage[today] = int(usage.get(today, 0)) + count
        # Only today and yesterday are of any use; keeping every day forever grows a file
        # nobody reads.
        keep = {today, (date.today() - timedelta(days=1)).isoformat()}
        payload = json.dumps({key: value for key, value in usage.items() if key in keep}, indent=2)
        # Written whole, then moved into place. A limit is exactly the thing that must not be
        # resettable by bad luck: a crash midway through a plain write leaves a half-written
        # file, which reads back as unparseable, which reads as "nothing used today" — and the
        # day's allowance silently starts over. A rename is atomic, so the file on disk is
        # always one complete version or the previous one.
        target = self._output_usage_path()
        temporary = target.with_suffix(".json.tmp")
        try:
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(target)
        except OSError:
            temporary.unlink(missing_ok=True)

    def _output_usage_path(self) -> Path:
        return self.path.parent / "automation-usage.json"

    def _read_output_usage(self) -> dict[str, int]:
        try:
            data = json.loads(self._output_usage_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(key): int(value) for key, value in data.items() if str(value).isdigit()}

    def update(self, request: SettingsUpdateRequest) -> SettingsSummary:
        if request.llm:
            patch = request.llm.model_dump(exclude_unset=True)
            self._apply_patch("llm", patch, secret_keys={"api_key"})
        if request.tts:
            patch = request.tts.model_dump(exclude_unset=True)
            self._apply_patch("tts", patch, secret_keys={"app_id", "access_token"})
        if request.seedance:
            patch = request.seedance.model_dump(exclude_unset=True)
            self._apply_patch("seedance", patch, secret_keys={"api_key", "model", "image_model", "tos_access_key_id", "tos_secret_access_key", "tos_security_token"})
        if request.robot:
            patch = request.robot.model_dump(exclude_unset=True)
            self._apply_patch("robot", patch, secret_keys=set())
        if request.automation:
            patch = request.automation.model_dump(exclude_unset=True)
            self._apply_patch("automation", patch, secret_keys=set())
        self._save()
        return self.summary()

    def replace_for_development(self, settings: dict[str, Any]) -> SettingsSummary:
        merged = self._merged_defaults()
        for section in ("llm", "tts", "seedance", "robot", "automation"):
            merged[section].update(settings.get(section, {}))
        self._settings = merged
        self._save()
        return self.summary()

    def _apply_patch(self, section: str, patch: dict[str, Any], secret_keys: set[str]) -> None:
        target = self._settings[section]
        for key, value in patch.items():
            if value is None:
                continue
            if key in secret_keys and value == "":
                continue
            target[key] = value

    def _load(self) -> dict[str, Any]:
        settings = self._merged_defaults()
        if self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    for section in ("llm", "tts", "seedance", "robot", "automation"):
                        if isinstance(loaded.get(section), dict):
                            settings[section].update(loaded[section])
            except json.JSONDecodeError as exc:
                raise ValueError(f"Settings file is not valid JSON: {self.path}") from exc
        return settings

    def _save(self) -> None:
        self.path.write_text(json.dumps(self._settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def _merged_defaults(self) -> dict[str, Any]:
        settings = deepcopy(DEFAULT_SETTINGS)
        env_llm_key = os.environ.get("AVE_LLM_API_KEY") or os.environ.get("DOUBAO_API_KEY")
        env_llm_model = os.environ.get("AVE_LLM_MODEL") or os.environ.get("DOUBAO_MODEL")
        if env_llm_key:
            settings["llm"].update({"enabled": True, "api_key": env_llm_key})
        if env_llm_model:
            settings["llm"].update({"model": env_llm_model})

        env_tts_app_id = os.environ.get("AVE_TTS_APP_ID") or os.environ.get("TTS_APP_ID")
        env_tts_token = os.environ.get("AVE_TTS_ACCESS_TOKEN") or os.environ.get("TTS_ACCESS_TOKEN")
        env_tts_voice = os.environ.get("AVE_TTS_VOICE_TYPE") or os.environ.get("TTS_VOICE_TYPE")
        env_tts_cluster = os.environ.get("AVE_TTS_CLUSTER") or os.environ.get("TTS_CLUSTER")
        if env_tts_app_id and env_tts_token:
            settings["tts"].update({"enabled": True, "app_id": env_tts_app_id, "access_token": env_tts_token})
        if env_tts_voice:
            settings["tts"].update({"voice_type": env_tts_voice})
        if env_tts_cluster:
            settings["tts"].update({"cluster": env_tts_cluster})

        env_seedance_key = os.environ.get("AVE_SEEDANCE_API_KEY") or os.environ.get("SEEDANCE_API_KEY")
        env_seedance_model = os.environ.get("AVE_SEEDANCE_MODEL") or os.environ.get("SEEDANCE_MODEL")
        env_seedance_tos_ak = os.environ.get("AVE_SEEDANCE_TOS_ACCESS_KEY_ID") or os.environ.get("TOS_ACCESS_KEY_ID")
        env_seedance_tos_sk = os.environ.get("AVE_SEEDANCE_TOS_SECRET_ACCESS_KEY") or os.environ.get("TOS_SECRET_ACCESS_KEY")
        env_seedance_tos_token = os.environ.get("AVE_SEEDANCE_TOS_SECURITY_TOKEN") or os.environ.get("TOS_SECURITY_TOKEN")
        env_seedance_tos_bucket = os.environ.get("AVE_SEEDANCE_TOS_BUCKET") or os.environ.get("TOS_BUCKET")
        env_seedance_tos_region = os.environ.get("AVE_SEEDANCE_TOS_REGION") or os.environ.get("TOS_REGION")
        env_seedance_tos_endpoint = os.environ.get("AVE_SEEDANCE_TOS_ENDPOINT") or os.environ.get("TOS_ENDPOINT")
        env_seedance_tos_prefix = os.environ.get("AVE_SEEDANCE_TOS_OBJECT_PREFIX") or os.environ.get("TOS_OBJECT_PREFIX")
        if env_seedance_key:
            settings["seedance"].update({"enabled": True, "api_key": env_seedance_key})
        if env_seedance_model:
            settings["seedance"].update({"model": env_seedance_model})
        if env_seedance_tos_ak:
            settings["seedance"].update({"tos_access_key_id": env_seedance_tos_ak.strip()})
        if env_seedance_tos_sk:
            settings["seedance"].update({"tos_secret_access_key": env_seedance_tos_sk.strip()})
        if env_seedance_tos_token:
            settings["seedance"].update({"tos_security_token": env_seedance_tos_token.strip()})
        if env_seedance_tos_bucket:
            settings["seedance"].update({"tos_bucket": env_seedance_tos_bucket.strip()})
        if env_seedance_tos_region:
            settings["seedance"].update({"tos_region": env_seedance_tos_region.strip()})
        if env_seedance_tos_endpoint:
            settings["seedance"].update({"tos_endpoint": env_seedance_tos_endpoint.strip()})
        if env_seedance_tos_prefix:
            settings["seedance"].update({"tos_object_prefix": env_seedance_tos_prefix.strip()})

        env_robot_ws = os.environ.get("AVE_ROBOT_WEBSOCKET_URL") or os.environ.get("ROBOT_WEBSOCKET_URL")
        if env_robot_ws:
            settings["robot"].update({"websocket_url": env_robot_ws.strip()})
        return settings

    def _secret_status(self, value: str) -> SecretStatus:
        return SecretStatus(configured=bool(value), masked=self._mask(value) if value else None)

    def _mask(self, value: str) -> str:
        if len(value) <= 8:
            return "configured"
        return f"{value[:4]}...{value[-4:]}"
