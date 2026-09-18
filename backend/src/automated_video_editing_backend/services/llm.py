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

# Used only when the operator asks for a target length. Length is prioritised, but the ban on
# inventing facts is absolute — the extra characters come from delivery (restating, transitions,
# addressing the listener), never from new information.
VOICEOVER_TARGET_SYSTEM_PROMPT = """你为机器人拍摄的短视频撰写口播文稿。

你会收到运营希望表达的文案，以及一个目标字数。以文案为唯一事实来源，整理成适合短视频配音的自然口播，并把长度写到目标字数。

规则：
- 用中文输出。
- 长度优先：尽量达到目标字数，可以略多，不要明显偏少。字数不够时，用「口播式扩写」补足——换个说法强调重点、加入过渡与节奏、直接对观众说话、适当设问、渲染好处与画面感、自然收尾。
- 扩写只是表达方式，不得新增任何事实：价格、品牌、名称、地点、数字、功能一律以原文为准，原文没有就不要提，也不要跑题、不要生硬地重复同一句话。
- 如果文案完全没有可用信息，返回空字符串，不要凭空编造。
- 只返回口播正文，不要前言、标题、解释或引号。"""

# 口播 rate for the Chinese voices at speed_ratio 1.0. Smooth LLM-drafted narration measured
# ~4.1–4.5 字/秒 live, so 4 lands 预计时长 close to real duration. Scaled by speed_ratio.
CHARS_PER_SECOND = 4.0


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

    async def draft_voiceover(self, raw_text: str, target_seconds: float | None = None) -> str:
        """Draft narration solely from text the operator deliberately supplied for speech.

        Capture notes have a different job: they describe which point contains which subject
        for explicit picture-to-speech mapping in the composition service. Keeping
        them out of this service makes it impossible for a filming reminder to become dialogue.

        ``target_seconds`` asks for a spoken length: it is turned into a target 字数 and the
        prompt switches to the length-first variant. Blank keeps the original prompt exactly.
        """
        cfg = self.settings.llm_config()
        if not bool(cfg.get("enabled")):
            return raw_text.strip()
        if not self._configured(cfg):
            raise ValueError("LLM is enabled but settings are incomplete")

        body = raw_text.strip() or "（无）"
        target_chars = self._target_chars(target_seconds)
        if target_chars:
            system = VOICEOVER_TARGET_SYSTEM_PROMPT
            user = f"文案或要求：\n{body}\n\n目标字数：约 {target_chars} 字。"
            # A Chinese character is a couple of tokens; leave generous head-room so a long
            # target is never cut off mid-sentence. A slightly warmer temperature carries the
            # rhetorical expansion that makes length-first narration read smoothly.
            max_tokens = min(2000, max(380, target_chars * 4))
            temperature = 0.7
        else:
            system = VOICEOVER_SYSTEM_PROMPT
            user = f"文案或要求：\n{body}"
            max_tokens = 380
            temperature = 0.55

        return await self._chat(
            cfg,
            system=system,
            user=user,
            max_tokens=max_tokens,
            temperature=temperature,
        )

    def _target_chars(self, target_seconds: float | None) -> int | None:
        """Seconds → target 字数 at the measured 口播 rate, scaled by the TTS speed_ratio.

        A faster voice fits more characters into the same seconds, so the speed the audio will
        actually be spoken at belongs in the estimate.
        """
        if not target_seconds or float(target_seconds) <= 0:
            return None
        speed = float(self.settings.tts_config().get("speed_ratio") or 1.0)
        return max(1, round(float(target_seconds) * CHARS_PER_SECOND * speed))

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
