
from automated_video_editing_backend.core.security import _token_from_subprotocol, token_matches


def test_token_matches_exact_value(monkeypatch):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "secret")
    assert token_matches("secret") is True
    assert token_matches("wrong") is False
    assert token_matches(None) is False


def test_token_from_websocket_subprotocol():
    assert _token_from_subprotocol("ave.bridge, ave-token-secret") == "secret"
    assert _token_from_subprotocol("ave.bridge") is None
