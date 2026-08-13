from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from uuid import uuid4

import httpx
from pydantic import ValidationError

from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.core.paths import GENERATED_DIRS, ensure_inside_root, generated_path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".m4a", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DOWNLOAD_EXT_BY_CONTENT_TYPE = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
MEDIA_EXTS = VIDEO_EXTS | AUDIO_EXTS | IMAGE_EXTS


def resolve_robot_media_url(raw_url: str, robot_websocket_url: str) -> str:
    """Turn a robot response into a URL the desktop can actually download.

    Website imports already arrive as complete HTTP(S) URLs and do not use this function.
    Robot firmware may instead return a complete URL, a host/path without a scheme, or a path
    relative to its websocket endpoint. A filesystem URI is never reachable across machines and
    is rejected with an explicit error instead of being mistaken for a successful recording.
    """
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("机器人成功响应中没有媒体地址")
    local_posix = ("/home/", "/tmp/", "/var/", "/mnt/", "/Users/")
    if (
        value.lower().startswith("file:")
        or re.match(r"^[a-zA-Z]:[\\/]", value)
        or value.startswith("\\\\")
        or value.startswith(local_posix)
    ):
        raise ValueError("机器人返回了其本机文件路径；请让机器人返回可下载的 HTTP(S) 地址")

    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"}:
        return value
    if parsed.scheme:
        raise ValueError(f"机器人返回了不支持的媒体地址协议：{parsed.scheme}")

    socket = urlparse(str(robot_websocket_url or "").strip())
    if socket.scheme not in {"ws", "wss"} or not socket.hostname:
        raise ValueError("无法根据机器人 WebSocket 地址解析媒体下载地址")
    download_scheme = "https" if socket.scheme == "wss" else "http"
    netloc = socket.netloc.rsplit("@", 1)[-1]
    base = urlunparse((download_scheme, netloc, "/", "", "", ""))

    # Some firmware omits only the scheme: `10.73.2.199:8000/media/file.mp4`.
    if re.match(r"^(?:\[[0-9a-fA-F:]+\]|[^/:\s]+):\d+(?:/|$)", value):
        return f"{download_scheme}://{value}"
    if re.match(r"^(?:\d{1,3}\.){3}\d{1,3}(?:/|$)", value):
        return f"{download_scheme}://{value}"
    return urljoin(base, value)


def _content_type(content_type: str | None) -> str:
    return (content_type or "").split(";")[0].strip().lower()


def _is_supported_download(url: str, content_type: str | None = None) -> bool:
    ext = Path(unquote(urlparse(url).path)).suffix.lower()
    mime = _content_type(content_type)
    return ext in MEDIA_EXTS or mime.startswith(("video/", "audio/", "image/"))


def _safe_download_name(url: str, content_type: str | None = None) -> str:
    parsed = urlparse(url)
    raw_name = Path(unquote(parsed.path)).name
    stem = Path(raw_name).stem or "download"
    ext = Path(raw_name).suffix.lower()
    if ext not in MEDIA_EXTS:
        ext = DOWNLOAD_EXT_BY_CONTENT_TYPE.get(_content_type(content_type), ".mp4")
    safe_stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", stem).strip(".-_") or "download"
    return f"{safe_stem}-{uuid4().hex[:8]}{ext}"


def role_for_kind(kind: str) -> str:
    if kind == "video":
        return "raw_video"
    if kind == "audio":
        return "music"
    if kind == "image":
        return "image"
    return "unknown"


class MediaService:
    def __init__(self, path: Path | None = None) -> None:
        self._items: dict[str, MediaItem] = {}
        # Generated media is rediscovered by scanning the app's own folders, but imported
        # clips live wherever the operator keeps them and were forgotten on every restart.
        self.path = path or generated_path("data", "media-library.json")
        self._load_imports()
        self.refresh_generated_media()

    def _load_imports(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return

        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("path"):
                continue
            try:
                item = MediaItem(**entry)
            except (ValidationError, TypeError):
                continue
            # Drop clips the operator has since moved or deleted rather than showing a
            # library row that cannot be played.
            if Path(item.path).is_file():
                self._items[item.id] = item

    def _save_imports(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = [
                item.model_dump(mode="json")
                for item in self._items.values()
                if item.metadata.get("source") == "local_import"
            ]
            self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    def forget(self, media_id: str) -> MediaItem | None:
        """Remove an imported clip from the library, leaving the operator's file untouched.

        Only imported clips can be forgotten. Generated media is rediscovered by scanning,
        so forgetting it would achieve nothing but a confusing reappearance.
        """
        item = self._items.get(media_id)
        if item is None or item.metadata.get("source") != "local_import":
            return None
        self._items.pop(media_id, None)
        self._save_imports()
        return item

    def forget_missing_imports(self) -> int:
        """Drop imported entries whose file no longer exists. Returns how many went."""
        gone = [
            item_id
            for item_id, item in self._items.items()
            if item.metadata.get("source") == "local_import" and not Path(item.path).is_file()
        ]
        for item_id in gone:
            self._items.pop(item_id, None)
        if gone:
            self._save_imports()
        return len(gone)

    def refresh_generated_media(self) -> None:
        generated_sources = {
            "data/downloads": GENERATED_DIRS["data"] / "downloads",
            "data/tts": GENERATED_DIRS["data"] / "tts",
            "data/seedance/effects": GENERATED_DIRS["data"] / "seedance" / "effects",
            "exports": GENERATED_DIRS["exports"],
        }
        existing_paths = {
            str(path.resolve())
            for folder in generated_sources.values()
            for path in folder.glob("*")
            if path.is_file() and self.infer_kind(path) != "unknown"
        }
        stale_ids = [
            item_id
            for item_id, item in self._items.items()
            if item.metadata.get("source") in generated_sources and item.path not in existing_paths
        ]
        for item_id in stale_ids:
            self._items.pop(item_id, None)

        known_paths = {item.path for item in self._items.values()}
        for source, folder in generated_sources.items():
            folder.mkdir(parents=True, exist_ok=True)
            for path in sorted(folder.glob("*")):
                if not path.is_file() or self.infer_kind(path) == "unknown":
                    continue
                resolved = str(ensure_inside_root(path))
                if resolved in known_paths:
                    continue
                kind = self.infer_kind(path)
                role = (
                    "tts_voice" if source == "data/tts"
                    else "seedance_effect" if source == "data/seedance/effects"
                    else "export" if source == "exports"
                    else role_for_kind(kind)
                )
                item = MediaItem(
                    path=resolved,
                    kind=kind,
                    metadata={"source": source, "role": role},
                )
                self._items[item.id] = item

    def list_items(self) -> list[MediaItem]:
        self.refresh_generated_media()
        # The vault hides imports whose file has gone, so without this Edit Studio would
        # keep offering a clip that no longer exists.
        self.forget_missing_imports()
        self._tag_cruise_points()
        return list(self._items.values())

    def _tag_cruise_points(self) -> None:
        """Record how many cruise points each video carries, if any.

        Most editing choices act on the points a cruise recorded, and an operator looking at
        the controls has no way to know whether their footage has any. Answering it here means
        the UI can say which settings will do something instead of offering all of them and
        quietly ignoring half. Recomputed on listing rather than at import, because a cruise
        writes its spans after the recording already exists.
        """
        from automated_video_editing_backend.services.capture import read_sidecar

        for item in self._items.values():
            if item.kind != "video":
                continue
            sidecar = read_sidecar(item.path)
            segments = (sidecar or {}).get("segments") or []
            item.metadata["cruise_points"] = len({
                f"{segment.get('path_name')}#{segment.get('goal_id')}"
                for segment in segments
                if isinstance(segment, dict)
            })

    def get(self, media_id: str) -> MediaItem | None:
        return self._items.get(media_id)

    def infer_kind(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in VIDEO_EXTS:
            return "video"
        if ext in AUDIO_EXTS:
            return "audio"
        if ext in IMAGE_EXTS:
            return "image"
        return "unknown"

    def import_path(self, raw_path: str) -> MediaItem:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(raw_path)

        # Importing the same file twice used to mint a second id for one path. The vault
        # keys assets by path so it still showed one row, while Edit Studio lists items by
        # id and showed two — the same clip, selectable twice.
        resolved = str(path)
        existing = next((item for item in self._items.values() if item.path == resolved), None)
        if existing is not None:
            return existing

        kind = self.infer_kind(path)
        item = MediaItem(path=resolved, kind=kind, metadata={"source": "local_import", "role": role_for_kind(kind)})
        self._items[item.id] = item
        self._save_imports()
        return item

    def repoint(self, media_id: str, new_path: str) -> MediaItem | None:
        """Point a library entry at a file that has been renamed on disk."""
        item = self._items.get(media_id)
        if item is None:
            return None
        item.path = new_path
        if item.metadata.get("source") == "local_import":
            self._save_imports()
        return item

    def is_known_path(self, path: str) -> bool:
        """Whether this exact file was imported or generated by the app."""
        return any(item.path == path for item in self._items.values())

    async def download_url(
        self,
        url: str,
        metadata: dict | None = None,
        filename_prefix: str = "",
    ) -> MediaItem:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only direct http(s) media URLs are supported")

        target: Path | None = None
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=60) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    content_type = response.headers.get("content-type")
                    if not _is_supported_download(str(response.url), content_type):
                        raise ValueError("Only direct video, audio, or image URLs are supported")
                    target = generated_path(
                        "data",
                        "downloads",
                        f"{filename_prefix}{_safe_download_name(str(response.url), content_type)}",
                    )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            if chunk:
                                handle.write(chunk)

            if not target.is_file() or target.stat().st_size == 0:
                raise ValueError("下载完成但服务器返回了空文件")
        except Exception:
            if target:
                target.unlink(missing_ok=True)
            raise

        kind = self.infer_kind(target)
        item_metadata = {"source": "data/downloads", "source_url": url, "role": role_for_kind(kind)}
        if metadata:
            item_metadata.update(metadata)
        item = MediaItem(
            path=str(ensure_inside_root(target)),
            kind=kind,
            metadata=item_metadata,
        )
        self._items[item.id] = item
        return item

    def register_generated_path(self, path: Path, kind: str | None = None, metadata: dict | None = None) -> MediaItem:
        resolved = ensure_inside_root(path)
        for item in self._items.values():
            if item.path == str(resolved):
                if metadata:
                    item.metadata.update(metadata)
                return item
        item = MediaItem(path=str(resolved), kind=kind or self.infer_kind(resolved), metadata=metadata or {})
        self._items[item.id] = item
        return item
