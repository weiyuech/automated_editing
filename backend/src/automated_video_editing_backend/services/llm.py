from __future__ import annotations

import httpx

from automated_video_editing_backend.core.models import ProviderTestResult
from automated_video_editing_backend.services.settings import SettingsService

DOUBAO_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

# Written in Chinese because the operator and the TTS voice are Chinese.
VOICEOVER_SYSTEM_PROMPT = """你为机器人拍摄的短视频撰写口播文稿。

你会收到运营希望表达的文案或要求。以它为唯一事实来源，整理成适合短视频配音的自然口播。

规则：
- 用中文输出。
- 只使用文案中已有的信息。不得编造价格、品牌、名称或任何未给出的说法。
- 如果文案没有可用信息，返回空字符串，不要用套话凑数。
- 只返回口播正文，不要前言、标题、解释或引号。"""


class LLMService:
    def __init__(self, settings: SettingsService) -> None:
        self.settings = settings

    async def test(self) -> ProviderTestResult:
        cfg = self.settings.llm_config()
        if not self._configured(cfg):
            return ProviderTestResult(ok=False, provider="doubao", message="LLM settings are incomplete")
        try:
            text = await self._chat(
                cfg,
                system="You are a concise health check for a desktop video editing app.",
                user="Reply with exactly: ready",
                max_tokens=20,
                temperature=0.0,
            )
            return ProviderTestResult(
                ok=True,
                provider="doubao",
                message="LLM responded",
                details={"model": cfg.get("model"), "sample": text[:80]},
            )
        except Exception as exc:  # noqa: BLE001 - provider health check reports every failure
            return ProviderTestResult(ok=False, provider="doubao", message=str(exc))

    async def draft_voiceover(self, raw_text: str) -> str:
        """Draft narration solely from text the operator deliberately supplied for speech.

        Capture notes have a different job: they describe which point contains which subject
        so the local semantic matcher can align an existing narration with the picture. Keeping
        them out of this service makes it impossible for a filming reminder to become dialogue.
        """
        cfg = self.settings.llm_config()
        if not bool(cfg.get("enabled")):
            return raw_text.strip()
        if not self._configured(cfg):
            raise ValueError("LLM is enabled but settings are incomplete")

        user = f"文案或要求：\n{raw_text.strip() or '（无）'}"
        return await self._chat(
            cfg,
            system=VOICEOVER_SYSTEM_PROMPT,
            user=user,
            max_tokens=380,
            temperature=0.55,
        )

    async def _chat(
        self,
        cfg: dict,
        *,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
    ) -> str:
        timeout_ms = int(cfg.get("timeout_ms") or 20000)
        payload = {
            "model": cfg["model"],
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        async with httpx.AsyncClient(timeout=timeout_ms / 1000) as client:
            response = await client.post(
                DOUBAO_API_URL,
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json=payload,
            )
        if response.status_code >= 400:
            raise ValueError(self._api_error_message(response))
        data = response.json()
        return str(data["choices"][0]["message"]["content"]).strip()

    def _api_error_message(self, response: httpx.Response) -> str:
        try:
            payload = response.json()
            code = payload.get("error", {}).get("code") or payload.get("code")
        except ValueError:
            code = None
        suffix = f": {code}" if code else ""
        return f"LLM API error {response.status_code}{suffix}"

    def _configured(self, cfg: dict) -> bool:
        return bool(cfg.get("api_key") and cfg.get("model"))
