from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


LOGGER = logging.getLogger("uvicorn.error")
DIAGNOSTIC_LOGGER = logging.getLogger("automated_video_editing.diagnostics")
URL_PATTERN = re.compile(r"(?:https?|wss?)://[^\s\"']+")
SENSITIVE_KEYS = {"authorization", "token", "api_key", "access_token", "secret_access_key"}


def configure_diagnostics(log_path: Path) -> None:
    """Keep actionable app diagnostics even when Uvicorn's stderr configuration changes."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(log_path.resolve())
    if any(getattr(handler, "baseFilename", None) == resolved for handler in DIAGNOSTIC_LOGGER.handlers):
        return
    handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    DIAGNOSTIC_LOGGER.addHandler(handler)
    DIAGNOSTIC_LOGGER.setLevel(logging.INFO)
    DIAGNOSTIC_LOGGER.propagate = False


def safe_url(value: str) -> str:
    """Keep a URL useful for diagnosis without writing credentials or signed queries."""
    try:
        parsed = urlsplit(value)
    except ValueError:
        return "<invalid-url>"
    if parsed.scheme not in {"http", "https", "ws", "wss"}:
        return value
    host = parsed.hostname or ""
    try:
        port = parsed.port
    except ValueError:
        return "<invalid-url>"
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "<redacted>" if parsed.query else "", ""))


def safe_text(value: str) -> str:
    return URL_PATTERN.sub(lambda match: safe_url(match.group(0)), value)


def _safe(value: Any, key: str = "") -> Any:
    if key.lower() in SENSITIVE_KEYS:
        return "<redacted>"
    if isinstance(value, dict):
        return {str(child_key): _safe(child, str(child_key)) for child_key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe(child) for child in value]
    if isinstance(value, str):
        if "url" in key.lower() or value.startswith(("http://", "https://", "ws://", "wss://")):
            return safe_url(value)
        return safe_text(value)
    return value


def log_event(level: str, event: str, **fields: Any) -> None:
    """Write one searchable JSON diagnostic line into Electron's backend.log."""
    payload = json.dumps(_safe(fields), ensure_ascii=False, separators=(",", ":"), default=str)
    writer = getattr(LOGGER, level.lower(), LOGGER.info)
    writer("[ave] %s %s", event, payload)
    diagnostic_writer = getattr(DIAGNOSTIC_LOGGER, level.lower(), DIAGNOSTIC_LOGGER.info)
    diagnostic_writer("[ave] %s %s", event, payload)
