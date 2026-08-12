from __future__ import annotations

import hmac
import os
from fastapi import Header, HTTPException, WebSocket


TOKEN_ENV = "APP_BRIDGE_TOKEN"


def bridge_token() -> str:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is required")
    return token


def token_matches(candidate: str | None) -> bool:
    expected = bridge_token()
    if not candidate:
        return False
    return hmac.compare_digest(candidate, expected)


async def require_http_token(x_bridge_token: str | None = Header(default=None)) -> None:
    if not token_matches(x_bridge_token):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _token_from_subprotocol(header_value: str | None) -> str | None:
    if not header_value:
        return None
    for protocol in (part.strip() for part in header_value.split(",")):
        if protocol.startswith("ave-token-"):
            return protocol.removeprefix("ave-token-")
    return None


async def require_ws_token(websocket: WebSocket) -> str | None:
    query_token = websocket.query_params.get("token")
    header_token = websocket.headers.get("x-bridge-token")
    protocol_token = _token_from_subprotocol(websocket.headers.get("sec-websocket-protocol"))
    if not token_matches(protocol_token or query_token or header_token):
        await websocket.close(code=1008)
        raise HTTPException(status_code=401, detail="Unauthorized")
    return "ave.bridge" if protocol_token else None
