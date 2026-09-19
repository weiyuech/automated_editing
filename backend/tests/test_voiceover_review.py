from fastapi.testclient import TestClient

import automated_video_editing_backend.main as main_module
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.seedance import SeedanceService
from automated_video_editing_backend.services.tts import TTSService


def test_prompt_preview_is_read_only_and_keeps_baseline_for_custom_rules(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "review-test-token")

    async def must_not_call_provider(*args, **kwargs):
        raise AssertionError("查看提示词不得调用大模型或语音服务")

    monkeypatch.setattr(LLMService, "_chat", must_not_call_provider)
    monkeypatch.setattr(TTSService, "synthesize", must_not_call_provider)
    client = TestClient(main_module.create_app())
    headers = {"x-bridge-token": "review-test-token"}
    custom = client.post("/api/tts/prompt", headers=headers, json={
        "text": "产品介绍", "instructions": "简洁自然", "system_prompt": "自定义完整规则",
    })
    assert custom.status_code == 200
    assert custom.json()["system"] == "自定义完整规则"
    assert "简洁自然" in custom.json()["user"]
    baseline = client.post("/api/tts/prompt", headers=headers, json={"text": "产品介绍"})
    assert baseline.status_code == 200
    assert baseline.json()["system"] == custom.json()["baseline_system"]


def test_draft_endpoint_returns_review_text_without_calling_tts(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "review-test-token")
    calls = {"llm": 0, "tts": 0}

    async def draft(_self, raw_text, target_seconds=None, *, instructions="", system_prompt=None):
        calls["llm"] += 1
        assert raw_text == "原始 OPC 文案"
        assert target_seconds == 30
        assert instructions == "不要添加开场白"
        assert system_prompt is None
        return "改写后的 OPC 旁白"

    async def must_not_synthesise(_self, *_args, **_kwargs):
        calls["tts"] += 1
        raise AssertionError("draft review must not call TTS")

    monkeypatch.setattr(LLMService, "draft_voiceover", draft)
    monkeypatch.setattr(TTSService, "synthesize", must_not_synthesise)
    client = TestClient(main_module.create_app())

    response = client.post(
        "/api/tts/draft",
        headers={"x-bridge-token": "review-test-token"},
        json={"text": "  原始 OPC 文案  ", "target_seconds": 30, "instructions": "不要添加开场白"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "source_text": "原始 OPC 文案",
        "draft_text": "改写后的 OPC 旁白",
        "target_seconds": 30.0,
    }
    assert calls == {"llm": 1, "tts": 0}


def test_draft_endpoint_rejects_blank_source_before_llm(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "review-test-token")

    async def must_not_draft(_self, *_args, **_kwargs):
        raise AssertionError("blank source must not call LLM")

    monkeypatch.setattr(LLMService, "draft_voiceover", must_not_draft)
    client = TestClient(main_module.create_app())

    response = client.post(
        "/api/tts/draft",
        headers={"x-bridge-token": "review-test-token"},
        json={"text": "   "},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Voiceover needs text"


def test_generate_endpoint_cannot_rewrite_and_spend_quota_in_one_step(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "review-test-token")
    calls = {"llm": 0, "tts": 0}

    async def must_not_draft(_self, *_args, **_kwargs):
        calls["llm"] += 1
        raise AssertionError("generation must not rewrite an unreviewed draft")

    async def must_not_synthesise(_self, *_args, **_kwargs):
        calls["tts"] += 1
        raise AssertionError("an unreviewed draft must not spend narration quota")

    monkeypatch.setattr(LLMService, "draft_voiceover", must_not_draft)
    monkeypatch.setattr(TTSService, "synthesize", must_not_synthesise)
    client = TestClient(main_module.create_app())

    response = client.post(
        "/api/tts/generate",
        headers={"x-bridge-token": "review-test-token"},
        json={"text": "尚未审阅的原文", "use_llm": True},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Voiceover draft must be reviewed before synthesis"
    assert calls == {"llm": 0, "tts": 0}


def test_paid_generation_quota_store_failures_are_explained_to_the_client(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "quota-failure-token")

    def broken_tts_quota(_self):
        raise RuntimeError("旁白额度记录不可用，已停止生成以避免重复消费")

    def broken_effect_quota(_self):
        raise RuntimeError("特效额度记录不可用，已停止生成以避免重复消费")

    async def broken_tts_generate(_self, *_args, **_kwargs):
        return broken_tts_quota(_self)

    async def broken_effect_generate(_self, *_args, **_kwargs):
        return broken_effect_quota(_self)

    monkeypatch.setattr(TTSService, "quota", broken_tts_quota)
    monkeypatch.setattr(TTSService, "synthesize", broken_tts_generate)
    monkeypatch.setattr(SeedanceService, "quota", broken_effect_quota)
    monkeypatch.setattr(SeedanceService, "generate", broken_effect_generate)
    client = TestClient(main_module.create_app(), raise_server_exceptions=False)
    headers = {"x-bridge-token": "quota-failure-token"}

    responses = [
        client.get("/api/tts/quota", headers=headers),
        client.post("/api/tts/generate", headers=headers, json={"text": "一句旁白"}),
        client.get("/api/seedance/quota", headers=headers),
        client.post("/api/seedance/generate", headers=headers, json={"prompt": "一个转场"}),
    ]

    assert [response.status_code for response in responses] == [500, 500, 500, 500]
    assert "旁白额度记录不可用" in responses[0].json()["detail"]
    assert "旁白额度记录不可用" in responses[1].json()["detail"]
    assert "特效额度记录不可用" in responses[2].json()["detail"]
    assert "特效额度记录不可用" in responses[3].json()["detail"]
