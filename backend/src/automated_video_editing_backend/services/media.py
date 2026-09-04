from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from uuid import uuid4

import httpx
from pydantic import ValidationError

from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.core.paths import (
    GENERATED_DIRS,
    ensure_inside_root,
    generated_path,
)
from automated_video_editing_backend.core.store import read_json, write_json

LOGGER = logging.getLogger(__name__)

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
MEDIA_POOL_FIELDS = {
    "source_media_ids": ("video", "raw_video"),
    "music_media_ids": ("audio", "music"),
    "voiceover_media_ids": ("audio", "tts_voice"),
    "effect_media_ids": ("video", "seedance_effect"),
}


class MediaPoolPersistenceError(ValueError):
    """The curated working set could not be read or durably changed."""


class GeneratedMetadataPersistenceError(RuntimeError):
    """A generated export could not be durably registered in the media library."""


class MediaLibraryPersistenceError(ValueError):
    """Imported-media records could not be read or durably changed."""


def _is_managed_export_path(path: str | Path) -> bool:
    """Whether the physical path belongs to the app's output directory."""
    try:
        resolved = Path(path).expanduser().resolve()
        export_root = GENERATED_DIRS["exports"].resolve()
    except (OSError, RuntimeError):
        return False
    return resolved == export_root or export_root in resolved.parents


def _is_flat_managed_export_file(path: str | Path) -> bool:
    """Exports are durable only as direct children of the directory scanned on restart."""
    try:
        resolved = Path(path).expanduser().resolve()
        export_root = GENERATED_DIRS["exports"].resolve()
    except (OSError, RuntimeError, ValueError):
        return False
    return resolved.parent == export_root


def _is_allowed_automatic_source_path(path: str | Path, source: object) -> bool:
    """Fail closed unless a raw-video path belongs to one of the two input domains.

    ``data/downloads`` is the app-owned inbox used for robot captures and URL imports. Local
    imports may live elsewhere, including a user's footage folder inside a development checkout.
    Only the known generated roots are excluded: an old or forged ``raw_video`` role must never
    turn an export, effect, preview, cache or log into automatic footage.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        downloads_root = (GENERATED_DIRS["data"] / "downloads").resolve()
        generated_roots = (
            GENERATED_DIRS["data"].resolve(),
            GENERATED_DIRS["exports"].resolve(),
            GENERATED_DIRS["previews"].resolve(),
            GENERATED_DIRS["cache"].resolve(),
            GENERATED_DIRS["logs"].resolve(),
        )
    except (OSError, RuntimeError, ValueError):
        return False

    in_downloads = resolved == downloads_root or downloads_root in resolved.parents
    if in_downloads:
        return True
    in_generated_root = any(
        resolved == root or root in resolved.parents for root in generated_roots
    )
    return not in_generated_root and source == "local_import"


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
        self._imports_blocked = False
        self.media_library_problem = ""
        self.pool_path = self.path.with_name(f"{self.path.stem}-pool.json")
        # Generated files are found again by scanning their folders, but a directory entry
        # cannot tell us that two exports came from one render. Keep that app-owned context in
        # a separate path-keyed manifest. It deliberately does not infer groups from filenames:
        # old exports and coincidentally similar names must remain independent assets.
        self.generated_metadata_path = self.path.with_name(
            f"{self.path.stem}-generated-metadata.json"
        )
        self._generated_metadata: dict[str, dict] = {}
        self._generated_metadata_blocked = False
        self.generated_metadata_problem = ""
        self._pool_paths: dict[str, list[str]] = {field: [] for field in MEDIA_POOL_FIELDS}
        self._pool_initialized = False
        self._pool_blocked = False
        self.media_pool_problem = ""
        self._load_generated_metadata()
        self._load_imports()
        self._load_pool()
        self.refresh_generated_media()

    def _load_generated_metadata(self) -> None:
        existed = self.generated_metadata_path.exists()
        raw, problem = read_json(self.generated_metadata_path)
        if problem:
            self._block_generated_metadata(problem)
            return

        if raw is None:
            if existed:
                self._block_generated_metadata(
                    f"{self.generated_metadata_path.name} 格式无效：顶层内容应为对象"
                )
                return
            try:
                quarantined = sorted(
                    path.name
                    for path in self.generated_metadata_path.parent.glob(
                        f"{self.generated_metadata_path.name}.corrupt-*"
                    )
                )
            except OSError as exc:
                self._block_generated_metadata(
                    f"{self.generated_metadata_path.name} 无法检查：{exc}"
                )
                return
            if quarantined:
                self._block_generated_metadata(
                    f"{self.generated_metadata_path.name} 之前损坏，原文件已保留为 "
                    f"{quarantined[-1]}"
                )
            return

        try:
            self._generated_metadata = self._validate_generated_metadata_payload(raw)
        except (TypeError, ValueError) as exc:
            self._block_generated_metadata(f"{self.generated_metadata_path.name} 格式无效：{exc}")

    @staticmethod
    def _validate_generated_metadata_payload(raw: object) -> dict[str, dict]:
        if not isinstance(raw, dict):
            raise TypeError("顶层内容应为对象")
        if raw.get("version", 1) != 1:
            raise ValueError("版本无法识别")
        entries = raw.get("items")
        if not isinstance(entries, dict):
            raise TypeError("items 应为对象")

        loaded: dict[str, dict] = {}
        for path, metadata in entries.items():
            if not isinstance(path, str) or not path:
                raise ValueError("items 含有无效路径")
            if not isinstance(metadata, dict):
                raise TypeError(f"{path} 的记录应为对象")
            if metadata.get("source") != "exports" or metadata.get("role") != "export":
                raise ValueError(f"{path} 不是有效的成片记录")
            try:
                export_path = Path(path).expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ValueError(f"{path} 不是有效的成片路径") from exc
            if str(export_path) != path or not _is_flat_managed_export_file(export_path):
                raise ValueError(f"{path} 不在受管成片目录中")
            subtitles_path = metadata.get("subtitles_path")
            if subtitles_path is not None and not isinstance(subtitles_path, str):
                raise TypeError(f"{path} 的 subtitles_path 应为字符串")
            if subtitles_path:
                try:
                    sidecar = Path(subtitles_path).expanduser().resolve()
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError(f"{path} 的 subtitles_path 无效") from exc
                expected = export_path.with_suffix(".subtitles.json")
                if str(sidecar) != subtitles_path or sidecar != expected:
                    raise ValueError(f"{path} 的 subtitles_path 不属于该成片")
            loaded[str(export_path)] = dict(metadata)
        return loaded

    def _block_generated_metadata(self, problem: str) -> None:
        self._generated_metadata_blocked = True
        self.generated_metadata_problem = problem
        LOGGER.error("Could not load generated-media metadata: %s", problem)

    def _save_generated_metadata(
        self,
        snapshot: dict[str, dict] | None = None,
        *,
        required: bool = False,
    ) -> bool:
        if self._generated_metadata_blocked:
            if required:
                raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
            return False
        entries = self._generated_metadata if snapshot is None else snapshot
        saved = write_json(
            self.generated_metadata_path,
            {"version": 1, "items": entries},
        )
        if saved:
            if snapshot is not None:
                self._generated_metadata = snapshot
            self._generated_metadata_blocked = False
            self.generated_metadata_problem = ""
            return True
        # Background pruning remains best-effort because the media files themselves are safe.
        # Explicit export registration passes required=True: a job must not claim success when
        # the grouping/subtitle context needed after restart did not become durable.
        consequence = "成片记录没有更新" if required else "重启后成片与母版可能无法分组"
        self.generated_metadata_problem = (
            f"{self.generated_metadata_path.name} 无法保存；{consequence}"
        )
        LOGGER.error("%s", self.generated_metadata_problem)
        if required:
            raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
        return False

    def _generated_metadata_snapshot(self, item: MediaItem) -> dict[str, dict] | None:
        """Return a proposed manifest without changing the live one."""
        if item.metadata.get("source") != "exports" or item.metadata.get("role") != "export":
            return None
        if self._generated_metadata_blocked:
            raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
        metadata = item.model_dump(mode="json")["metadata"]
        if self._generated_metadata.get(item.path) == metadata:
            return None
        snapshot = dict(self._generated_metadata)
        snapshot[item.path] = metadata
        return snapshot

    def _load_pool(self) -> None:
        existed = self.pool_path.exists()
        raw, problem = read_json(self.pool_path)
        if problem:
            self._block_pool(problem)
            return

        if raw is None:
            if existed:
                # JSON `null` is readable, but it is not an absent manifest and must not be
                # overwritten by the first-upgrade seed.
                self._block_pool(f"{self.pool_path.name} 格式无效：顶层内容应为对象")
                return
            # read_json quarantines invalid JSON. On the next process start the primary path
            # is therefore absent, but that is not a fresh install: automatically seeding it
            # from the whole library would silently replace the operator's curated set.
            try:
                quarantined = sorted(
                    path.name
                    for path in self.pool_path.parent.glob(f"{self.pool_path.name}.corrupt-*")
                )
            except OSError as exc:
                self._block_pool(f"{self.pool_path.name} 无法检查：{exc}")
                return
            if quarantined:
                self._block_pool(
                    f"{self.pool_path.name} 之前损坏，原文件已保留为 {quarantined[-1]}"
                )
            # Only this genuinely-absent case remains uninitialised. media_pool() will perform
            # the one-time upgrade seed and must persist it before returning it to the caller.
            return

        try:
            loaded = self._validate_pool_payload(raw)
        except (TypeError, ValueError) as exc:
            self._block_pool(f"{self.pool_path.name} 格式无效：{exc}")
            return

        self._pool_paths = loaded
        self._pool_initialized = True

    @staticmethod
    def _validate_pool_payload(raw: object) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            raise TypeError("顶层内容应为对象")

        loaded: dict[str, list[str]] = {}
        for field in MEDIA_POOL_FIELDS:
            values = raw.get(field, [])
            if not isinstance(values, list):
                raise TypeError(f"{field} 应为列表")
            if any(not isinstance(value, str) or not value for value in values):
                raise ValueError(f"{field} 含有无效路径")
            loaded[field] = list(dict.fromkeys(values))
        return loaded

    def _block_pool(self, problem: str) -> None:
        self._pool_blocked = True
        self.media_pool_problem = problem
        LOGGER.error("Could not load media pool: %s", problem)

    def _save_pool(self, paths: dict[str, list[str]]) -> None:
        """Persist a complete snapshot before exposing it as the current working set."""
        payload = {field: list(paths.get(field, [])) for field in MEDIA_POOL_FIELDS}
        if not write_json(self.pool_path, payload):
            problem = f"{self.pool_path.name} 无法保存；媒体池没有更新"
            self.media_pool_problem = problem
            LOGGER.error("Could not save media pool: %s", problem)
            raise MediaPoolPersistenceError(problem)

        self._pool_paths = payload
        self._pool_initialized = True
        self._pool_blocked = False
        self.media_pool_problem = ""

    @staticmethod
    def _matches_pool(item: MediaItem, field: str) -> bool:
        kind, role = MEDIA_POOL_FIELDS[field]
        if field == "source_media_ids":
            return MediaService.is_automatic_source(item)
        return item.kind == kind and item.metadata.get("role") == role

    @staticmethod
    def is_automatic_source(item: MediaItem) -> bool:
        """The authoritative boundary for footage automatic editing may consume."""
        return (
            item.kind == "video"
            and item.metadata.get("role") == "raw_video"
            and _is_allowed_automatic_source_path(
                item.path,
                item.metadata.get("source"),
            )
        )

    def media_pool(self) -> dict[str, list[str]]:
        """Return the persistent editing working set using current media ids.

        Paths are stored on disk because generated items are rediscovered with fresh ids after
        a backend restart.  The API still returns ids so the existing editing request contract
        remains unchanged.
        """
        if self._pool_blocked:
            raise MediaPoolPersistenceError(self.media_pool_problem)
        if self._imports_blocked:
            raise MediaPoolPersistenceError(
                f"{self.media_library_problem}；为保护已选素材，媒体池没有更新"
            )

        items = self.list_items()
        if not self._pool_initialized:
            # Upgrade compatibility: the old studio treated every compatible library item as
            # pooled. Preserve that working set once, then only explicit pool edits change it.
            seeded = {
                field: [item.path for item in items if self._matches_pool(item, field)]
                for field in MEDIA_POOL_FIELDS
            }
            self._save_pool(seeded)

        by_path = {item.path: item for item in items}
        cleaned: dict[str, list[str]] = {}
        result: dict[str, list[str]] = {}
        for field, paths in self._pool_paths.items():
            valid_paths = [
                path
                for path in paths
                if path in by_path and self._matches_pool(by_path[path], field)
            ]
            cleaned[field] = valid_paths
            result[field] = [by_path[path].id for path in valid_paths]
        if cleaned != self._pool_paths:
            self._save_pool(cleaned)
        return result

    def update_media_pool(self, media_ids: dict[str, list[str]]) -> dict[str, list[str]]:
        """Replace the working set without deleting anything from the media library."""
        if self._pool_blocked:
            raise MediaPoolPersistenceError(self.media_pool_problem)
        if self._imports_blocked:
            raise MediaPoolPersistenceError(
                f"{self.media_library_problem}；为保护已选素材，媒体池没有更新"
            )
        self.list_items()
        updated: dict[str, list[str]] = {}
        for field in MEDIA_POOL_FIELDS:
            ids = list(dict.fromkeys(media_ids.get(field, [])))
            paths: list[str] = []
            for media_id in ids:
                item = self._items.get(media_id)
                if item is None:
                    raise ValueError("That media item no longer exists")
                if not self._matches_pool(item, field):
                    raise ValueError(f"Invalid media type for {field}")
                paths.append(item.path)
            updated[field] = paths
        # Write first so a full disk or permissions error cannot make a failed PUT look
        # successful until the process restarts. The old in-memory and on-disk set both stay
        # intact when the atomic write does not land.
        self._save_pool(updated)
        return self.media_pool()

    def _load_imports(self) -> None:
        existed = self.path.exists()
        raw, problem = read_json(self.path)
        if problem:
            self._block_imports(problem)
            return

        if raw is None:
            if existed:
                self._block_imports(f"{self.path.name} 格式无效：顶层内容应为列表")
                return
            try:
                quarantined = sorted(
                    path.name for path in self.path.parent.glob(f"{self.path.name}.corrupt-*")
                )
            except OSError as exc:
                self._block_imports(f"{self.path.name} 无法检查：{exc}")
                return
            if quarantined:
                self._block_imports(f"{self.path.name} 之前损坏，原文件已保留为 {quarantined[-1]}")
            return
        if not isinstance(raw, list):
            self._block_imports(f"{self.path.name} 格式无效：顶层内容应为列表")
            return

        loaded: dict[str, MediaItem] = {}
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("path"):
                self._block_imports(f"{self.path.name} 格式无效：含有不完整的素材记录")
                return
            try:
                item = MediaItem(**entry)
            except (ValidationError, TypeError) as exc:
                self._block_imports(f"{self.path.name} 格式无效：{exc}")
                return
            if item.metadata.get("source") != "local_import":
                self._block_imports(f"{self.path.name} 格式无效：含有非导入素材记录")
                return
            # Old builds could persist generated video as a local raw-video import. Physical
            # provenance is authoritative: leave managed outputs for the corresponding scanner
            # (or vault) instead of preserving a source identity the pool and jobs must reject.
            # This changes no file and guesses no export grouping.
            if (
                item.kind == "video"
                and item.metadata.get("role") == "raw_video"
                and not self.is_automatic_source(item)
            ):
                continue
            # Drop clips the operator has since moved or deleted rather than showing a
            # library row that cannot be played.
            if Path(item.path).is_file():
                loaded[item.id] = item
        self._items.update(loaded)

    def _block_imports(self, problem: str) -> None:
        self._imports_blocked = True
        self.media_library_problem = problem
        LOGGER.error("Could not load imported-media library: %s", problem)

    def _save_imports(self, items: dict[str, MediaItem] | None = None) -> None:
        if self._imports_blocked:
            raise MediaLibraryPersistenceError(self.media_library_problem)
        payload = [
            item.model_dump(mode="json")
            for item in (self._items if items is None else items).values()
            if item.metadata.get("source") == "local_import"
        ]
        if not write_json(self.path, payload):
            self.media_library_problem = f"{self.path.name} 无法保存；媒体库没有更新"
            LOGGER.error("Could not save imported-media library: %s", self.media_library_problem)
            raise MediaLibraryPersistenceError(self.media_library_problem)
        self.media_library_problem = ""

    def forget(self, media_id: str) -> MediaItem | None:
        """Remove an imported clip from the library, leaving the operator's file untouched.

        Only imported clips can be forgotten. Generated media is rediscovered by scanning,
        so forgetting it would achieve nothing but a confusing reappearance.
        """
        item = self._items.get(media_id)
        if item is None or item.metadata.get("source") != "local_import":
            return None
        if self._pool_blocked:
            raise MediaPoolPersistenceError(self.media_pool_problem)
        remaining = dict(self._items)
        remaining.pop(media_id, None)
        updated_pool = {field: list(paths) for field, paths in self._pool_paths.items()}
        for field, paths in self._pool_paths.items():
            if item.path in paths:
                updated_pool[field] = [path for path in paths if path != item.path]
        pool_changed = updated_pool != self._pool_paths
        previous_pool = {field: list(paths) for field, paths in self._pool_paths.items()}
        if pool_changed:
            self._save_pool(updated_pool)
        try:
            self._save_imports(remaining)
        except Exception as exc:
            if pool_changed:
                try:
                    self._save_pool(previous_pool)
                except MediaPoolPersistenceError as rollback_exc:
                    raise MediaLibraryPersistenceError(
                        f"{exc}；媒体池回滚失败：{rollback_exc}"
                    ) from exc
            raise
        self._items.pop(media_id, None)
        return item

    def forget_missing_imports(self) -> int:
        """Drop imported entries whose file no longer exists. Returns how many went."""
        gone = [
            item_id
            for item_id, item in self._items.items()
            if item.metadata.get("source") == "local_import" and not Path(item.path).is_file()
        ]
        if gone:
            remaining = {
                item_id: item for item_id, item in self._items.items() if item_id not in gone
            }
            self._save_imports(remaining)
            self._items = remaining
        return len(gone)

    def refresh_generated_media(self) -> None:
        generated_sources = {
            "data/downloads": GENERATED_DIRS["data"] / "downloads",
            "data/tts": GENERATED_DIRS["data"] / "tts",
            "data/seedance/effects": GENERATED_DIRS["data"] / "seedance" / "effects",
            "exports": GENERATED_DIRS["exports"],
        }
        discoverable_sources = {
            source: folder
            for source, folder in generated_sources.items()
            if source != "exports" or not self._generated_metadata_blocked
        }
        existing_paths = {
            str(path.resolve())
            for folder in discoverable_sources.values()
            for path in folder.glob("*")
            if path.is_file() and self.infer_kind(path) != "unknown"
        }
        stale_ids = [
            item_id
            for item_id, item in self._items.items()
            if item.metadata.get("source") in discoverable_sources
            and (
                item.path not in existing_paths
                or (
                    item.metadata.get("source") == "exports"
                    and item.path not in self._generated_metadata
                )
            )
        ]
        for item_id in stale_ids:
            self._items.pop(item_id, None)

        # The desktop moves deleted files to the system trash before asking the backend to
        # refresh. Remove their auxiliary records here so the manifest cannot accumulate dead
        # paths or accidentally decorate a different file later created at the same name.
        if not self._generated_metadata_blocked:
            stale_metadata_paths = [
                path for path in self._generated_metadata if path not in existing_paths
            ]
            for path in stale_metadata_paths:
                self._generated_metadata.pop(path, None)
            if stale_metadata_paths:
                self._save_generated_metadata()

        known_paths = {item.path for item in self._items.values()}
        for source, folder in discoverable_sources.items():
            folder.mkdir(parents=True, exist_ok=True)
            for path in sorted(folder.glob("*")):
                if not path.is_file() or self.infer_kind(path) == "unknown":
                    continue
                resolved = str(ensure_inside_root(path))
                if resolved in known_paths:
                    continue
                if source == "exports" and resolved not in self._generated_metadata:
                    # A top-level MP4 is not a completed export until its durable record exists.
                    # FFmpeg creates the delivery early and may be interrupted; exposing that file
                    # during a refresh (or after a crash) turns a partial encode into selectable
                    # media. Storage/vault views may still account for the orphan on disk.
                    continue
                kind = self.infer_kind(path)
                role = (
                    "tts_voice"
                    if source == "data/tts"
                    else "seedance_effect"
                    if source == "data/seedance/effects"
                    else "export"
                    if source == "exports"
                    else role_for_kind(kind)
                )
                metadata = {"source": source, "role": role}
                if source == "exports":
                    # Only explicit records are restored. In particular, do not pair
                    # `name.mp4` with `name 母版.mp4` merely because both happen to exist.
                    metadata.update(self._generated_metadata.get(resolved, {}))
                    metadata["source"] = "exports"
                    metadata["role"] = "export"
                item = MediaItem(
                    path=resolved,
                    kind=kind,
                    metadata=metadata,
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
        from automated_video_editing_backend.services.capture import inspect_sidecar

        for item in self._items.values():
            if item.kind != "video":
                continue
            capability = inspect_sidecar(item.path)
            item.metadata["cruise_points"] = capability["point_count"]
            item.metadata["successful_cruise_points"] = capability["successful_points"]
            item.metadata["failed_cruise_points"] = capability["failed_points"]
            item.metadata["point_evidence"] = capability["evidence"]
            item.metadata["point_evidence_message"] = capability["message"]

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

        if _is_managed_export_path(path):
            # A finished output may be selected later for manual fine tuning, but importing it
            # can never turn it into automatic-mode source footage. Let the generated-media
            # scan create/return the authoritative export entry instead.
            self.refresh_generated_media()
            generated = next(
                (item for item in self._items.values() if item.path == str(path)), None
            )
            if generated is not None:
                return generated
            if self._generated_metadata_blocked:
                raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
            raise ValueError("成片只能用于手动微调，不能作为源视频素材导入")
        if self._imports_blocked:
            raise MediaLibraryPersistenceError(self.media_library_problem)

        # Importing the same file twice used to mint a second id for one path. The vault
        # keys assets by path so it still showed one row, while Edit Studio lists items by
        # id and showed two — the same clip, selectable twice.
        resolved = str(path)
        existing = next((item for item in self._items.values() if item.path == resolved), None)
        if existing is not None:
            return existing

        kind = self.infer_kind(path)
        item = MediaItem(
            path=resolved,
            kind=kind,
            metadata={"source": "local_import", "role": role_for_kind(kind)},
        )
        if (
            item.kind == "video"
            and item.metadata.get("role") == "raw_video"
            and not self.is_automatic_source(item)
        ):
            raise ValueError("应用生成的视频只能用于对应素材类型，不能作为源视频导入")
        updated = dict(self._items)
        updated[item.id] = item
        self._save_imports(updated)
        self._items[item.id] = item
        return item

    def repoint(self, media_id: str, new_path: str) -> MediaItem | None:
        """Point a library entry at a file that has been renamed on disk."""
        item = self._items.get(media_id)
        if item is None:
            return None
        old_path = item.path
        candidate = item.model_copy(deep=True)
        candidate.path = new_path

        is_export = (
            candidate.metadata.get("source") == "exports"
            and candidate.metadata.get("role") == "export"
        )
        if not is_export and self._pool_blocked:
            raise MediaPoolPersistenceError(self.media_pool_problem)
        if is_export:
            if self._generated_metadata_blocked:
                raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
            if new_path != old_path and new_path in self._generated_metadata:
                raise GeneratedMetadataPersistenceError(
                    f"{Path(new_path).name} 已有另一条成片记录，无法更新媒体库"
                )
            generated_snapshot = dict(self._generated_metadata)
            generated_snapshot.pop(old_path, None)
            generated_snapshot[new_path] = candidate.model_dump(mode="json")["metadata"]
            if generated_snapshot != self._generated_metadata:
                # Persist before changing the MediaItem. MediaRenameService can then roll its
                # filesystem and job pointers back knowing this object still names old_path.
                self._save_generated_metadata(generated_snapshot, required=True)

        updated_pool = {field: list(paths) for field, paths in self._pool_paths.items()}
        for field, paths in self._pool_paths.items():
            if old_path in paths:
                updated_pool[field] = [new_path if path == old_path else path for path in paths]
        pool_changed = not is_export and updated_pool != self._pool_paths
        previous_pool = {field: list(paths) for field, paths in self._pool_paths.items()}
        is_local_import = item.metadata.get("source") == "local_import"
        if is_local_import and self._imports_blocked:
            raise MediaLibraryPersistenceError(self.media_library_problem)
        if pool_changed:
            self._save_pool(updated_pool)

        if is_local_import:
            imports_snapshot = dict(self._items)
            imports_snapshot[item.id] = candidate
            try:
                self._save_imports(imports_snapshot)
            except Exception as exc:
                # Pool and imported-media manifests are two files describing the same renamed
                # path. If the second commit fails, restore the first before the outer rename
                # transaction puts the actual file back; otherwise the next pool refresh prunes
                # the user's selection even though the rename API reported failure.
                if pool_changed:
                    try:
                        self._save_pool(previous_pool)
                    except MediaPoolPersistenceError as rollback_exc:
                        raise MediaLibraryPersistenceError(
                            f"{exc}；媒体池回滚失败：{rollback_exc}"
                        ) from exc
                raise
        item.path = new_path
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
            async with (
                httpx.AsyncClient(follow_redirects=True, timeout=60) as client,
                client.stream("GET", url) as response,
            ):
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

    def register_generated_path(
        self, path: Path, kind: str | None = None, metadata: dict | None = None
    ) -> MediaItem:
        return self.register_generated_paths([(path, kind, metadata)])[0]

    def register_generated_paths(
        self,
        registrations: list[tuple[Path, str | None, dict | None]],
    ) -> list[MediaItem]:
        """Publish one generated asset group with one durable metadata commit.

        A subtitled delivery and its clean master are one logical result. Building every
        candidate and writing their combined manifest before touching ``_items`` prevents a
        failed second registration from exposing half a group in memory or after restart.
        Single-asset callers use the same transaction through ``register_generated_path``.
        """
        if not registrations:
            return []

        existing_by_path = {item.path: item for item in self._items.values()}
        proposed: list[tuple[MediaItem | None, MediaItem]] = []
        seen_paths: set[str] = set()
        generated_snapshot = dict(self._generated_metadata)
        generated_changed = False

        for path, kind, metadata in registrations:
            resolved = ensure_inside_root(path)
            resolved_path = str(resolved)
            if resolved_path in seen_paths:
                raise ValueError(f"同一生成文件重复登记：{resolved.name}")
            seen_paths.add(resolved_path)

            current = existing_by_path.get(resolved_path)
            if current is None:
                candidate = MediaItem(
                    path=resolved_path,
                    kind=kind or self.infer_kind(resolved),
                    metadata=dict(metadata or {}),
                )
            else:
                candidate = current.model_copy(deep=True)
                if metadata:
                    candidate.metadata.update(metadata)

            if (
                candidate.metadata.get("source") == "exports"
                and candidate.metadata.get("role") == "export"
            ):
                if not _is_flat_managed_export_file(resolved):
                    raise ValueError(
                        "Generated export metadata can only be registered for a file directly "
                        "inside the managed exports folder"
                    )
                if not resolved.is_file():
                    raise ValueError("A generated export must exist before it can be registered")
                subtitles_path = candidate.metadata.get("subtitles_path")
                if subtitles_path is not None and not isinstance(subtitles_path, str):
                    raise ValueError("Generated export subtitles_path must be a string")
                if subtitles_path:
                    try:
                        actual_sidecar = Path(subtitles_path).expanduser().resolve()
                        expected_sidecar = resolved.with_suffix(".subtitles.json").resolve()
                    except (OSError, RuntimeError, ValueError) as exc:
                        raise ValueError("Generated export subtitles_path is invalid") from exc
                    if str(actual_sidecar) != subtitles_path or actual_sidecar != expected_sidecar:
                        raise ValueError(
                            "Generated export subtitles_path must be the canonical sidecar "
                            "belonging to the same video"
                        )
                    if not actual_sidecar.is_file():
                        raise ValueError(
                            "A generated export subtitle sidecar must exist before registration"
                        )
                if self._generated_metadata_blocked:
                    raise GeneratedMetadataPersistenceError(
                        self.generated_metadata_problem
                    )
                dumped = candidate.model_dump(mode="json")["metadata"]
                if generated_snapshot.get(candidate.path) != dumped:
                    generated_snapshot[candidate.path] = dumped
                    generated_changed = True
            proposed.append((current, candidate))

        if generated_changed:
            # One atomic file replacement commits the whole group. _save_generated_metadata
            # changes the live manifest only after write_json reports success.
            self._save_generated_metadata(generated_snapshot, required=True)

        published: list[MediaItem] = []
        for current, candidate in proposed:
            if current is None:
                self._items[candidate.id] = candidate
                published.append(candidate)
            else:
                current.metadata = candidate.metadata
                published.append(current)
        return published
