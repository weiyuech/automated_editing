from __future__ import annotations

import httpx

from automated_video_editing_backend.core.models import ProviderTestResult
from automated_video_editing_backend.services.settings import SettingsService

DOUBAO_API_URL = "https://ark.cn-beijing.volces.com/api/v3/chat/completions"

# Written in Chinese because the notes, the operator, and the TTS voice are all Chinese.
VOICEOVER_SYSTEM_PROMPT = """你为机器人拍摄的短视频撰写口播文稿。

你会收到两类输入，它们的地位并不相同：
- 文案或要求：运营希望表达的内容。有它时，以它为准，决定内容与语气。
- 备注：拍摄现场随手记录的观察，是素材，绝不是要照着念出来的句子。

备注是混杂的。只取其中描述画面内容的部分——商品、卖点、价格、值得一提的细节。
凡是关于拍摄和现场执行的，一律安静丢弃：运镜抖动、灯光、电量、重拍、机器人状态、
给自己的提醒。任何对素材本身的吐槽都不许出现在口播里。

规则：
- 用中文输出。
- 只使用文案或备注中已有的信息。不得编造价格、品牌、名称或任何未给出的说法。
- 文案与备注冲突时，以文案为准。
- 只有备注、没有文案时，从可用的备注中组织口播。
- 如果筛选后没有任何可用信息，返回空字符串，不要用套话凑数。
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
        except Exception as exc:
            return ProviderTestResult(ok=False, provider="doubao", message=str(exc))

    async def draft_voiceover(
        self,
        raw_text: str,
        notes: list[str] | None = None,
    ) -> str:
        """Draft narration from the operator's text and, optionally, capture notes.

        Notes are raw jottings made while filming and are a mixed bag: some describe the
        subject, some complain about the footage. The prompt's main job is discarding the
        second kind, so a shaky-camera note never becomes a spoken line.
        """
        cfg = self.settings.llm_config()
        if not bool(cfg.get("enabled")):
            return raw_text.strip()
        if not self._configured(cfg):
            raise ValueError("LLM is enabled but settings are incomplete")

        note_lines = [note.strip() for note in (notes or []) if note and note.strip()]
        user = "\n\n".join(
            [
                "备注（原始记录，无顺序）：\n" + ("\n".join(f"- {line}" for line in note_lines) or "（无）"),
                f"文案或要求：\n{raw_text.strip() or '（无）'}",
            ]
        )
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
