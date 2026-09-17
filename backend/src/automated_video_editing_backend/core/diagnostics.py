from __future__ import annotations

import atexit
import json
import logging
import re
import time
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any
from urllib.parse import urlsplit, urlunsplit

LOGGER = logging.getLogger("uvicorn.error")
DIAGNOSTIC_LOGGER = logging.getLogger("automated_video_editing.diagnostics")
URL_PATTERN = re.compile(r"(?:https?|wss?)://[^\s\"']+")
SENSITIVE_KEYS = {"authorization", "token", "api_key", "access_token", "secret_access_key"}
DIAGNOSTIC_QUEUE_CAPACITY = 2048
_DIAGNOSTIC_LISTENER: QueueListener | None = None
_DIAGNOSTIC_TARGET: str | None = None


class _BoundedQueueHandler(QueueHandler):
    """Keep robot receive paths independent from diagnostic disk latency."""

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
            return
        except Full:
            pass
        try:
            self.queue.get_nowait()
        except Empty:
            return
        try:
            self.queue.put_nowait(record)
        except Full:
            return


def _stop_diagnostic_listener() -> None:
    global _DIAGNOSTIC_LISTENER, _DIAGNOSTIC_TARGET
    listener = _DIAGNOSTIC_LISTENER
    if listener is None:
        return
    # Detach producers first. Once one queue slot is freed below, no concurrent log record can
    # fill it again before QueueListener enqueues its shutdown sentinel.
    for existing in list(DIAGNOSTIC_LOGGER.handlers):
        if isinstance(existing, QueueHandler):
            DIAGNOSTIC_LOGGER.removeHandler(existing)
            existing.close()
    try:
        try:
            listener.stop()
        except Full:
            # QueueListener uses a non-blocking sentinel; make one slot if shutdown races a burst.
            try:
                listener.queue.get_nowait()
            except Empty:
                pass
            listener.stop()
    finally:
        # QueueListener.stop drains and joins the worker but does not close its target handlers.
        # Closing explicitly matters on Windows, where a leaked handle prevents later rotation.
        for target in listener.handlers:
            target.close()
    _DIAGNOSTIC_LISTENER = None
    _DIAGNOSTIC_TARGET = None


atexit.register(_stop_diagnostic_listener)


def configure_diagnostics(log_path: Path) -> None:
    """Keep actionable app diagnostics even when Uvicorn's stderr configuration changes."""
    global _DIAGNOSTIC_LISTENER, _DIAGNOSTIC_TARGET
    log_path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(log_path.resolve())
    if _DIAGNOSTIC_LISTENER is not None and _DIAGNOSTIC_TARGET == resolved:
        return

    _stop_diagnostic_listener()
    for existing in list(DIAGNOSTIC_LOGGER.handlers):
        DIAGNOSTIC_LOGGER.removeHandler(existing)
        existing.close()

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    records: Queue[logging.LogRecord] = Queue(maxsize=DIAGNOSTIC_QUEUE_CAPACITY)
    DIAGNOSTIC_LOGGER.addHandler(_BoundedQueueHandler(records))
    DIAGNOSTIC_LOGGER.setLevel(logging.INFO)
    DIAGNOSTIC_LOGGER.propagate = False
    _DIAGNOSTIC_LISTENER = QueueListener(
        records,
        file_handler,
        respect_handler_level=True,
    )
    _DIAGNOSTIC_TARGET = resolved
    _DIAGNOSTIC_LISTENER.start()


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
    # Electron captures Uvicorn's stderr without the file handler's formatter, so include an
    # explicit timestamp in the JSON as well.  A copied backend.log can then reconstruct real
    # robot timing instead of leaving us to infer it from line order alone.
    # Keep ``at`` owned by the logger even if a caller accidentally supplies the same field.
    payload_fields = {
        **fields,
        "at": datetime.now(UTC).isoformat(),
        "monotonic_seconds": round(time.monotonic(), 6),
    }
    payload = json.dumps(
        _safe(payload_fields),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    writer = getattr(LOGGER, level.lower(), LOGGER.info)
    writer("[ave] %s %s", event, payload)
    diagnostic_writer = getattr(DIAGNOSTIC_LOGGER, level.lower(), DIAGNOSTIC_LOGGER.info)
    diagnostic_writer("[ave] %s %s", event, payload)
