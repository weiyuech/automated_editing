from __future__ import annotations

from automated_video_editing_backend.services.recording_segments import iter_nodes

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from uuid import uuid4

import httpx
from pydantic import ValidationError

from automated_video_editing_backend.core.models import CaptureSelection, MediaItem
from automated_video_editing_backend.core.paths import (
    GENERATED_DIRS,
    RootPathError,
    ensure_inside_root,
    generated_path,
)
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services.capture_library import CaptureLibrary
from automated_video_editing_backend.services.media_download import (
    MediaDownloadNotReadyError,
    validate_downloaded_video,
)

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


def _managed_media_source(path: str | Path) -> str | None:
    """Return the authoritative app-owned area for a path, most specific first."""
    try:
        resolved = Path(path).expanduser().resolve()
        roots = (
            ("data/downloads", (GENERATED_DIRS["data"] / "downloads").resolve()),
            ("data/tts", (GENERATED_DIRS["data"] / "tts").resolve()),
            (
                "data/seedance/effects",
                (GENERATED_DIRS["data"] / "seedance" / "effects").resolve(),
            ),
            ("exports", GENERATED_DIRS["exports"].resolve()),
            ("previews", GENERATED_DIRS["previews"].resolve()),
            ("cache", GENERATED_DIRS["cache"].resolve()),
            ("logs", GENERATED_DIRS["logs"].resolve()),
            ("data", GENERATED_DIRS["data"].resolve()),
        )
    except (OSError, RuntimeError, ValueError):
        return None
    for source, root in roots:
        if resolved == root or root in resolved.parents:
            return source
    return None


def _discoverable_media_sources() -> dict[str, Path]:
    """Folders whose direct media children are rebuilt by ``refresh_generated_media``."""
    return {
        "data/downloads": GENERATED_DIRS["data"] / "downloads",
        "data/tts": GENERATED_DIRS["data"] / "tts",
        "data/seedance/effects": GENERATED_DIRS["data"] / "seedance" / "effects",
        "exports": GENERATED_DIRS["exports"],
    }


def _discoverable_media_source(path: str | Path) -> str | None:
    """Return the scanner that owns this exact flat media-file location, if any."""
    try:
        resolved = Path(path).expanduser().resolve()
        sources = {
            source: folder.resolve() for source, folder in _discoverable_media_sources().items()
        }
    except (OSError, RuntimeError, ValueError):
        return None
    for source, root in sources.items():
        if resolved.parent == root:
            return source
    return None


def _safe_local_import_name(path: Path) -> str:
    """Keep readable Unicode while removing characters Windows filenames cannot contain."""
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", path.stem).strip(" .-") or "import"
    return f"{stem[:120].rstrip(' .-') or 'import'}{path.suffix.lower()}"


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


def _content_type_matches_kind(content_type: str | None, expected_kind: str) -> bool:
    """Treat absent/generic MIME as unknown, but never accept an explicit different kind."""
    mime = _content_type(content_type)
    if not mime or mime in {"application/octet-stream", "binary/octet-stream"}:
        return True
    return mime.startswith(f"{expected_kind}/")


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
        # This is the durable catalog, including valid references that are temporarily offline.
        # ``_items`` remains the currently usable inventory exposed to callers.
        self._import_catalog: dict[str, MediaItem] = {}
        self._import_lock = asyncio.Lock()
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
        self._capture_selections: list[dict] = []
        self._edit_inputs: dict[str, MediaItem] = {}
        self.captures = CaptureLibrary(
            self.path.with_name("capture-recordings.json"),
            GENERATED_DIRS["data"] / "capture_segments",
        )
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
            self._capture_selections = [
                CaptureSelection.model_validate(s).model_dump()
                for s in raw.get("capture_selections", [])
            ]
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

    def _save_pool(self, paths: dict[str, list[str]], captures: list[dict] | None = None) -> None:
        """Persist a complete snapshot before exposing it as the current working set."""
        payload = {field: list(paths.get(field, [])) for field in MEDIA_POOL_FIELDS}
        choices = self._capture_selections if captures is None else captures
        if choices:
            payload["capture_selections"] = choices
        if not write_json(self.pool_path, payload):
            problem = f"{self.pool_path.name} 无法保存；媒体池没有更新"
            self.media_pool_problem = problem
            LOGGER.error("Could not save media pool: %s", problem)
            raise MediaPoolPersistenceError(problem)

        self._pool_paths = {field: payload[field] for field in MEDIA_POOL_FIELDS}
        self._capture_selections = choices
        self._pool_initialized = True
        self._pool_blocked = False
        self.media_pool_problem = ""

    @staticmethod
    def _matches_pool(item: MediaItem, field: str) -> bool:
        kind, role = MEDIA_POOL_FIELDS[field]
        if field == "source_media_ids":
            return MediaService.is_automatic_source(item) and not item.metadata.get("capture_group")
        return item.kind == kind and item.metadata.get("role") == role

    @staticmethod
    def is_automatic_source(item: MediaItem) -> bool:
        if item.metadata.get("capture_input") and item.metadata.get("source") == "capture_input":
            root = (GENERATED_DIRS["data"] / "capture_segments").resolve()
            return item.kind == "video" and root in Path(item.path).resolve().parents
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
            # pooled. Include temporarily offline catalog entries so upgrading while a removable
            # drive is detached does not silently change that working set.
            seed_candidates = {item.path: item for item in items}
            seed_candidates.update({item.path: item for item in self._import_catalog.values()})
            seeded = {
                field: [
                    item.path
                    for item in seed_candidates.values()
                    if self._matches_pool(item, field)
                ]
                for field in MEDIA_POOL_FIELDS
            }
            self._save_pool(seeded)

        by_path: dict[str, MediaItem] = {}
        for item in items:
            by_path.setdefault(item.path, item)
        catalog_by_path: dict[str, MediaItem] = {}
        for item in self._import_catalog.values():
            catalog_by_path.setdefault(item.path, item)
        cleaned: dict[str, list[str]] = {}
        result: dict[str, list[str]] = {}
        for field, paths in self._pool_paths.items():
            retained_paths: list[str] = []
            visible_ids: list[str] = []
            retained_path_set: set[str] = set()
            for path in paths:
                if path in retained_path_set:
                    continue
                visible = by_path.get(path)
                if visible is not None and self._matches_pool(visible, field):
                    retained_paths.append(path)
                    visible_ids.append(visible.id)
                    retained_path_set.add(path)
                    continue
                offline = catalog_by_path.get(path)
                if offline is not None and self._matches_pool(offline, field):
                    # Return the stable catalog id even while the file is unavailable. The UI can
                    # then preserve it in a full pool update or explicitly clear it with [].
                    retained_paths.append(path)
                    visible_ids.append(offline.id)
                    retained_path_set.add(path)
            cleaned[field] = retained_paths
            result[field] = visible_ids
        for source_id in result["source_media_ids"]:
            source = self._items.get(source_id)
            voice = self._items.get(source.metadata.get("bound_voice_id")) if source else None
            if voice and voice.id not in result["voiceover_media_ids"]:
                result["voiceover_media_ids"].append(voice.id)
                cleaned["voiceover_media_ids"].append(voice.path)
        keep = []
        for mid in result["voiceover_media_ids"]:
            item = self._items.get(mid)
            if (
                not item
                or not item.metadata.get("binding_id")
                or item.metadata.get("bound_source_id") in result["source_media_ids"]
            ):
                keep.append(mid)
        result["voiceover_media_ids"] = keep
        cleaned["voiceover_media_ids"] = [
            self._items.get(mid, self._import_catalog.get(mid)).path for mid in keep
        ]
        if cleaned != self._pool_paths:
            self._save_pool(cleaned)
        if self._capture_selections:
            result["capture_selections"] = self._capture_selections
        return result

    def ensure_media_pool_initialized(self) -> None:
        """Persist the pre-existing working set before publishing a new generated asset.

        Older releases treated every compatible library item as pooled, so the first pool read
        performs a one-time compatibility seed.  A generator must cross that boundary before it
        creates a discoverable file; otherwise its brand-new output is mistaken for an older
        item and silently enters the working set.
        """
        if not self._pool_initialized:
            self.media_pool()

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
        result_ids: dict[str, list[str]] = {}
        for field in MEDIA_POOL_FIELDS:
            ids = list(dict.fromkeys(media_ids.get(field, [])))
            paths: list[str] = []
            accepted_ids: list[str] = []
            seen_paths: set[str] = set()
            for media_id in ids:
                item = self._items.get(media_id)
                if item is None:
                    item = self._import_catalog.get(media_id)
                    if item is None or Path(item.path).is_file():
                        raise ValueError("That media item no longer exists")
                if not self._matches_pool(item, field):
                    raise ValueError(f"Invalid media type for {field}")
                if item.path in seen_paths:
                    continue
                paths.append(item.path)
                accepted_ids.append(media_id)
                seen_paths.add(item.path)
            updated[field] = paths
            result_ids[field] = accepted_ids
        # A saved composition and its active voice enter the working set together.
        voice_ids = result_ids["voiceover_media_ids"]
        for source_id in result_ids["source_media_ids"]:
            source = self._items.get(source_id)
            voice_id = source.metadata.get("bound_voice_id") if source else None
            voice = self._items.get(voice_id)
            if voice and voice.id not in voice_ids:
                voice_ids.append(voice.id)
                updated["voiceover_media_ids"].append(voice.path)
        # Old bound voices are not independent choices once their source leaves the pool.
        retained = [
            mid
            for mid in voice_ids
            if not (item := self._items.get(mid))
            or not item.metadata.get("binding_id")
            or item.metadata.get("bound_source_id") in result_ids["source_media_ids"]
        ]
        result_ids["voiceover_media_ids"] = retained
        updated["voiceover_media_ids"] = [
            self._items.get(mid, self._import_catalog.get(mid)).path for mid in retained
        ]
        # Write first so a full disk or permissions error cannot make a failed PUT look
        # successful until the process restarts. The old in-memory and on-disk set both stay
        # intact when the atomic write does not land.
        choices = media_ids.get("capture_selections")
        if choices is None:
            choices = self._capture_selections
        choices = [CaptureSelection.model_validate(s).model_dump() for s in choices]
        selected_groups = {
            item.metadata["capture_group"]["id"]
            for mid in result_ids["source_media_ids"]
            if (item := self._items.get(mid)) and item.metadata.get("capture_group")
        }
        choices = [s for s in choices if s["capture_id"] in selected_groups]
        if len({s["capture_id"] for s in choices}) != len(choices):
            raise ValueError("同一次拍摄只能保存一份片段选择")
        for choice in choices:
            self.captures.validate_selection(choice)
        self._save_pool(updated, choices)
        # The ids above were validated against the inventory snapshot from list_items().
        # Calling media_pool() here used to run that same full scan a second time immediately.
        if choices:
            result_ids["capture_selections"] = choices
        return result_ids

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
        migrated = False
        if isinstance(raw, dict) and isinstance(raw.get("items"), list):
            # Some early packaged builds wrapped the same records in an object. Accept the
            # explicit records and rewrite the current list format.
            raw = raw["items"]
            migrated = True
        elif not isinstance(raw, list):
            self._block_imports(f"{self.path.name} 格式无效：顶层内容应为列表")
            return

        catalog: dict[str, MediaItem] = {}
        catalog_id_by_path: dict[str, str] = {}
        reserved_catalog_ids = {
            str(entry.get("id")) for entry in raw if isinstance(entry, dict) and entry.get("id")
        }
        loaded: dict[str, MediaItem] = {}
        legacy_exports: dict[str, dict] = {}
        for entry in raw:
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("path"):
                self._block_imports(f"{self.path.name} 格式无效：含有不完整的素材记录")
                return
            try:
                item = MediaItem(**entry)
            except (ValidationError, TypeError) as exc:
                self._block_imports(f"{self.path.name} 格式无效：{exc}")
                return
            try:
                item_path = Path(item.path).expanduser().resolve()
            except (OSError, RuntimeError, ValueError) as exc:
                self._block_imports(f"{self.path.name} 格式无效：素材路径无效（{exc}）")
                return
            resolved = str(item_path)
            kind = self.infer_kind(item_path)
            if kind == "unknown":
                self._block_imports(
                    f"{self.path.name} 格式无效：{item_path.name} 不是受支持的媒体文件"
                )
                return
            if item.path != resolved or item.kind != kind:
                item = item.model_copy(update={"path": resolved, "kind": kind})
                migrated = True

            source = item.metadata.get("source")
            role = item.metadata.get("role")
            managed_source = _managed_media_source(item_path)
            discoverable_source = _discoverable_media_source(item_path)

            # Only an explicit old export record is migrated. Preserve all grouping, variant,
            # and subtitles fields verbatim; never infer a master/subtitle pair from filenames.
            if source == "exports" or role == "export":
                if not _is_flat_managed_export_file(item_path):
                    self._block_imports(
                        f"{self.path.name} 格式无效：明确的成片记录不在受管成片目录中"
                    )
                    return
                if item_path.is_file():
                    metadata = dict(item.metadata)
                    metadata["source"] = "exports"
                    metadata["role"] = "export"
                    previous = legacy_exports.get(resolved)
                    if previous is not None and previous != metadata:
                        self._block_imports(
                            f"{self.path.name} 格式无效：同一成片含有互相冲突的旧版记录"
                        )
                        return
                    legacy_exports[resolved] = metadata
                migrated = True
                continue

            # Generated/downloaded app-owned files are rediscovered by their authoritative
            # scanners. Keeping old local-import identities would misclassify effects or TTS.
            if discoverable_source is not None:
                migrated = True
                continue
            if managed_source is not None:
                if source not in {None, "", "local_import", "import", "external", "local"}:
                    self._block_imports(f"{self.path.name} 格式无效：含有无法识别的素材来源")
                    return
                # Old builds could persist a cache, preview, or another non-scanned app-owned
                # file as though it were an external import. It is unsafe to retain that identity,
                # but blocking the whole catalog also strands every valid external reference and
                # prevents the operator from repairing this entry with explicit copy mode. Drop
                # only the stale record during migration; never touch the physical managed file.
                migrated = True
                LOGGER.warning(
                    "Ignoring legacy imported-media reference %s in managed area %s; "
                    "the physical file was left untouched",
                    item_path.name,
                    managed_source,
                )
                continue

            if source not in {None, "", "local_import", "import", "external", "local"}:
                self._block_imports(f"{self.path.name} 格式无效：含有无法识别的素材来源")
                return

            metadata = dict(item.metadata)
            metadata["source"] = "local_import"
            metadata["role"] = role_for_kind(kind)
            if metadata != item.metadata:
                item = item.model_copy(update={"metadata": metadata})
                migrated = True
            existing_id = catalog_id_by_path.get(resolved)
            if existing_id is not None:
                # Early builds could assign more than one identity to the same external file.
                # Keep the first manifest identity deterministically. The pool is path-backed,
                # so this repairs duplicate rows without changing the operator's selection.
                migrated = True
                LOGGER.warning(
                    "Ignoring duplicate imported-media id %s for %s; keeping %s",
                    item.id,
                    item_path.name,
                    existing_id,
                )
                continue
            if item.id in catalog:
                # A duplicated UUID must not make one otherwise-valid external path overwrite the
                # other. Keep the first record's public identity and durably re-key each later
                # path; the path-backed pool can then restore every original selection.
                old_id = item.id
                replacement_id = str(uuid4())
                while replacement_id in reserved_catalog_ids or replacement_id in catalog:
                    replacement_id = str(uuid4())
                reserved_catalog_ids.add(replacement_id)
                item = item.model_copy(update={"id": replacement_id})
                migrated = True
                LOGGER.warning(
                    "Re-keying duplicate imported-media id %s for %s as %s",
                    old_id,
                    item_path.name,
                    replacement_id,
                )
            catalog[item.id] = item
            catalog_id_by_path[resolved] = item.id
            # Offline references remain in the durable catalog but are hidden from the current
            # inventory until their drive/path becomes available again.
            if item_path.is_file():
                loaded[item.id] = item

        if migrated:
            generated = dict(self._generated_metadata)
            generated_changed = False
            for export_path, metadata in legacy_exports.items():
                current = generated.get(export_path)
                if current is not None and current != metadata:
                    self._block_imports(
                        f"{self.path.name} 旧版成片记录与现有成片元数据冲突："
                        f"{Path(export_path).name}"
                    )
                    return
                if current is None:
                    generated[export_path] = metadata
                    generated_changed = True
            try:
                if legacy_exports:
                    generated = self._validate_generated_metadata_payload(
                        {"version": 1, "items": generated}
                    )
                    if generated_changed:
                        self._save_generated_metadata(generated, required=True)
                self._save_imports(catalog)
            except (OSError, TypeError, ValueError, GeneratedMetadataPersistenceError) as exc:
                self._block_imports(f"{self.path.name} 旧版记录迁移失败：{exc}")
                return
        self._import_catalog = catalog
        self._items.update(loaded)

    def _block_imports(self, problem: str) -> None:
        self._imports_blocked = True
        self.media_library_problem = problem
        LOGGER.error("Could not load imported-media library: %s", problem)

    def _save_imports(self, catalog: dict[str, MediaItem] | None = None) -> None:
        if self._imports_blocked:
            raise MediaLibraryPersistenceError(self.media_library_problem)
        payload = [
            item.model_dump(mode="json")
            for item in (self._import_catalog if catalog is None else catalog).values()
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
        remaining_catalog = dict(self._import_catalog)
        remaining_catalog.pop(media_id, None)
        updated_pool = {field: list(paths) for field, paths in self._pool_paths.items()}
        for field, paths in self._pool_paths.items():
            if item.path in paths:
                updated_pool[field] = [path for path in paths if path != item.path]
        pool_changed = updated_pool != self._pool_paths
        previous_pool = {field: list(paths) for field, paths in self._pool_paths.items()}
        if pool_changed:
            self._save_pool(updated_pool)
        try:
            self._save_imports(remaining_catalog)
        except Exception as exc:
            if pool_changed:
                try:
                    self._save_pool(previous_pool)
                except MediaPoolPersistenceError as rollback_exc:
                    raise MediaLibraryPersistenceError(
                        f"{exc}；媒体池回滚失败：{rollback_exc}"
                    ) from exc
            raise
        self._import_catalog.pop(media_id, None)
        self._items.pop(media_id, None)
        return item

    def forget_missing_imports(self) -> int:
        """Synchronize reference availability without deleting the durable catalog."""
        unavailable = [
            item_id
            for item_id, item in self._items.items()
            if item.metadata.get("source") == "local_import" and not Path(item.path).is_file()
        ]
        for item_id in unavailable:
            self._items.pop(item_id, None)
        for item_id, item in self._import_catalog.items():
            if item_id not in self._items and Path(item.path).is_file():
                self._items[item_id] = item
        return len(unavailable)

    def refresh_generated_media(self) -> None:
        generated_sources = _discoverable_media_sources()
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
        # Hide unavailable references without deleting them from the durable catalog. A removable
        # drive can then reconnect and restore the same media id on the next scan.
        self.forget_missing_imports()
        self._tag_cruise_points()
        self.captures.enrich(self._items)
        from automated_video_editing_backend.services.composition_assets import enrich

        enrich(list(self._items.values()))
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
        return self._items.get(media_id) or self._edit_inputs.get(media_id)

    def capture_input_item(self, media_id: str, source_path: str) -> MediaItem | None:
        """Return one internally materialized capture input only for its exact id/path pair.

        Capture selections are intentionally absent from ``list_items``: they are disposable
        render inputs, not independent library assets.  A manual timeline produced from a draft
        still needs to submit that input back to the renderer, so authorize it through the
        process-local record that created it.  Resolving both paths strictly also rejects a
        deleted input and a managed-directory symlink that was replaced to point elsewhere.
        """
        item = self._edit_inputs.get(media_id)
        if (
            item is None
            or item.id != media_id
            or item.kind != "video"
            or item.metadata.get("source") != "capture_input"
            or not item.metadata.get("capture_input")
        ):
            return None
        try:
            claimed = Path(source_path).expanduser().resolve(strict=True)
            recorded = Path(item.path).expanduser().resolve(strict=True)
            managed_root = self.captures.directory.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        if claimed != recorded or managed_root not in claimed.parents:
            return None
        return item

    def is_capture_input_path(self, source_path: str) -> bool:
        """Keep inputs returned by a draft alive until this backend session ends.

        A draft is deliberately not a render job yet, so the ordinary queued/running job guard
        cannot see it.  The process-local edit-input registry is the draft lease: safe cleanup
        may reclaim abandoned inputs after a restart, but never between preview and render.
        """
        return any(
            self.capture_input_item(media_id, source_path) is not None
            for media_id in self._edit_inputs
        )

    def capture_child_item(self, root_media_id: str, child_path: str) -> MediaItem | None:
        """Authorize one generated child through its durable capture root.

        Children stay nested in the media library instead of becoming independent inventory
        rows. Manual fine-tuning nevertheless needs a real path to play and render, so the root
        id may vouch for exactly one ready child recorded in the authoritative manifest. Merely
        placing a file under capture_segments, or pairing an arbitrary path with a root id, is
        never sufficient.
        """
        root = self._items.get(root_media_id)
        public_group = root.metadata.get("capture_group") if root else None
        if root is None or not isinstance(public_group, dict):
            return None
        group = self.captures.groups.get(str(public_group.get("id") or ""))
        if group is None:
            return None
        try:
            requested = Path(child_path).expanduser().resolve(strict=True)
            managed_root = self.captures.directory.resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return None
        if managed_root not in requested.parents or not requested.is_file():
            return None
        for segment in iter_nodes(group.get("segments", [])):
            recorded_path = segment.get("path")
            if not recorded_path or segment.get("status") != "ready":
                continue
            try:
                recorded = Path(recorded_path).expanduser().resolve(strict=True)
            except (OSError, RuntimeError, ValueError):
                continue
            if recorded != requested:
                continue
            child = root.model_copy(deep=True)
            child.id = f"{root.id}:{segment['id']}"
            child.path = str(requested)
            child.metadata.update(
                source="capture_child",
                capture_child=True,
                capture_root_media_id=root.id,
                capture_segment_id=segment["id"],
                capture_segment_kind=segment.get("kind", "unknown"),
                capture_segment_label=segment.get("label", ""),
            )
            return child
        return None

    async def resolve_capture_sources(
        self, media_ids: list[str], selections: list[CaptureSelection]
    ) -> list[str]:
        """One root in, one source out. Never deal children as separate recordings."""
        if not selections:
            return media_ids
        self.list_items()
        choices = {s.capture_id: s.model_dump() for s in selections}
        if len(choices) != len(selections):
            raise ValueError("同一次拍摄只能有一份片段选择")
        result, consumed = [], set()
        for media_id in dict.fromkeys(media_ids):
            item = self.get(media_id)
            group = item.metadata.get("capture_group") if item else None
            key = group["id"] if group else None
            if key in choices:
                resolved = await self.captures.resolve(item, choices[key])
                if resolved is not item:
                    self._edit_inputs[resolved.id] = resolved
                result.append(resolved.id)
                consumed.add(key)
            else:
                result.append(media_id)
        if consumed != set(choices):
            raise ValueError("片段选择必须属于本次选中的拍摄组")
        return result

    def infer_kind(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext in VIDEO_EXTS:
            return "video"
        if ext in AUDIO_EXTS:
            return "audio"
        if ext in IMAGE_EXTS:
            return "image"
        return "unknown"

    def _registered_seedance_image(self, path: Path) -> MediaItem | None:
        """Recover an explicitly recorded Seedance image outside the default scanner root."""
        if self.infer_kind(path) != "image" or path.parent.name != "effects":
            return None
        for metadata_path in path.parent.glob("*.json"):
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
                recorded = payload.get("output_path") or payload.get("video_path")
                recorded_path = Path(str(recorded)).expanduser().resolve()
            except (
                AttributeError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
                json.JSONDecodeError,
            ):
                continue
            if recorded_path != path:
                continue
            metadata = {"source": "data/seedance/effects", "role": "seedance_effect"}
            if payload.get("id") and metadata_path.stem == str(payload["id"]):
                metadata["seedance_asset_id"] = str(payload["id"])
            return self.register_generated_path(path, kind="image", metadata=metadata)
        return None

    def _prepare_import(
        self,
        raw_path: str,
        storage_mode: str,
    ) -> tuple[Path, str, MediaItem | None]:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(raw_path)
        if storage_mode not in {"reference", "copy"}:
            raise ValueError("导入方式无效，请选择复制或仅引用")

        kind = self.infer_kind(path)
        if kind == "unknown":
            raise ValueError(f"不支持此文件格式：{path.suffix or '无扩展名'}")

        if _is_managed_export_path(path):
            # A finished output may be selected later for manual fine tuning, but importing it
            # can never turn it into automatic-mode source footage. Let the generated-media
            # scan create/return the authoritative export entry instead.
            self.refresh_generated_media()
            generated = next(
                (item for item in self._items.values() if item.path == str(path)), None
            )
            if generated is not None:
                return path, kind, generated
            if self._generated_metadata_blocked:
                raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
            raise ValueError("成片只能用于手动微调，不能作为源视频素材导入")

        managed_source = _managed_media_source(path)
        if managed_source is not None:
            # Selecting a file already owned by the app must not mint a conflicting local role.
            self.refresh_generated_media()
            generated = next(
                (item for item in self._items.values() if item.path == str(path)), None
            )
            if generated is not None:
                return path, kind, generated
            seedance_image = self._registered_seedance_image(path)
            if seedance_image is not None:
                return path, kind, seedance_image
            # A copy can safely flatten media from a cache/preview/nested app folder into the
            # scanner-owned inbox. A reference cannot: no scanner would restore it on restart,
            # while treating the app's caches as external input breaks source ownership.
            if storage_mode == "reference":
                raise ValueError("该文件位于不可引用的应用托管目录；请改为复制到应用媒体库")
        if self._imports_blocked:
            raise MediaLibraryPersistenceError(self.media_library_problem)

        if storage_mode == "reference":
            resolved = str(path)
            existing = next(
                (item for item in self._import_catalog.values() if item.path == resolved),
                None,
            )
            if existing is not None:
                # It may have been hidden while its drive was offline. The path exists now, so
                # restore the same durable identity immediately.
                self._items[existing.id] = existing
                return path, kind, existing
        return path, kind, None

    def import_path(self, raw_path: str, storage_mode: str = "reference") -> MediaItem:
        path, kind, existing = self._prepare_import(raw_path, storage_mode)
        if existing is not None:
            return existing
        if storage_mode == "copy":
            return self._copy_import_to_library(path)

        # Importing the same file twice used to mint a second id for one path. The vault
        # keys assets by path so it still showed one row, while Edit Studio lists items by
        # id and showed two — the same clip, selectable twice.
        item = MediaItem(
            path=str(path),
            kind=kind,
            metadata={"source": "local_import", "role": role_for_kind(kind)},
        )
        if (
            item.kind == "video"
            and item.metadata.get("role") == "raw_video"
            and not self.is_automatic_source(item)
        ):
            raise ValueError("应用生成的视频只能用于对应素材类型，不能作为源视频导入")
        updated_catalog = dict(self._import_catalog)
        updated_catalog[item.id] = item
        self._save_imports(updated_catalog)
        self._import_catalog[item.id] = item
        self._items[item.id] = item
        return item

    async def import_path_async(
        self,
        raw_path: str,
        storage_mode: str = "reference",
    ) -> MediaItem:
        """Import without blocking the event loop while a potentially large file is copied."""
        async with self._import_lock:
            path, _kind, existing = self._prepare_import(raw_path, storage_mode)
            if existing is not None:
                return existing
            if storage_mode != "copy":
                # Validation and catalog persistence are small and stay on the owning event loop.
                return self.import_path(str(path), storage_mode)

            copy_task = asyncio.create_task(asyncio.to_thread(self._copy_file_to_library, path))
            try:
                destination = await asyncio.shield(copy_task)
            except asyncio.CancelledError:
                # Cancelling an await cannot stop the worker thread. Hold the import lock until it
                # finishes so another import cannot race for the same readable destination name.
                # A completed but unacknowledged copy would otherwise become `name-2` on retry.
                while not copy_task.done():
                    try:
                        await asyncio.shield(copy_task)
                    except asyncio.CancelledError:
                        # A second cancellation request must not release the name lock while the
                        # worker thread can still publish a file behind it.
                        continue
                    except (OSError, ValueError):
                        break
                if not copy_task.cancelled():
                    try:
                        destination = copy_task.result()
                    except (OSError, ValueError):
                        # _copy_file_to_library already removes staging files on copy failure.
                        LOGGER.debug(
                            "Cancelled media import's worker also failed",
                            exc_info=True,
                        )
                    else:
                        try:
                            destination.unlink(missing_ok=True)
                        except OSError:
                            # The bytes are durable but could not be rolled back. Register them so
                            # the next inventory request cannot discover an unexplained orphan.
                            LOGGER.warning(
                                "Cancelled media import could not remove its completed copy; "
                                "keeping it in the media library",
                                exc_info=True,
                            )
                            self._recognize_copied_import(destination)
                        else:
                            resolved = str(destination.resolve())
                            for item_id, item in list(self._items.items()):
                                if item.path == resolved:
                                    self._items.pop(item_id, None)
                raise
            return self._recognize_copied_import(destination)

    def _copy_file_to_library(self, source: Path) -> Path:
        """Copy bytes atomically without touching the in-memory media inventory."""
        readable_name = _safe_local_import_name(source)
        stem = Path(readable_name).stem
        suffix = Path(readable_name).suffix
        temporary: Path | None = None
        try:
            # Resolve the configured inbox before any mkdir/open. In particular, never write
            # through a downloads symlink or junction which escapes the application root.
            destination_dir = ensure_inside_root(GENERATED_DIRS["data"] / "downloads")
            destination_dir.mkdir(parents=True, exist_ok=True)
            destination_dir = ensure_inside_root(destination_dir)
            destination = destination_dir / readable_name
            counter = 2
            while destination.exists():
                destination = destination_dir / f"{stem}-{counter}{suffix}"
                counter += 1

            destination = ensure_inside_root(destination)
            temporary = ensure_inside_root(
                destination_dir / f".{destination.name}.{uuid4().hex}.part"
            )
            # A managed copy belongs to the day it was imported. Preserving the source mtime
            # makes an old photo disappear into that historical day in the media calendar.
            shutil.copyfile(source, temporary)
            if temporary.stat().st_size != source.stat().st_size:
                raise OSError("复制后的文件大小与原文件不一致")
            temporary.replace(destination)
        except RootPathError as exc:
            LOGGER.warning("Refusing an unsafe managed-downloads path", exc_info=True)
            raise ValueError("应用媒体库目录无效，无法安全复制文件") from exc
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    LOGGER.warning("Could not remove failed import staging file", exc_info=True)
            LOGGER.exception("Could not copy local import into managed downloads")
            raise OSError(f"无法复制 {source.name} 到应用媒体库；请检查磁盘空间和文件权限") from exc
        return destination

    def _recognize_copied_import(self, destination: Path) -> MediaItem:
        """Publish a completed managed copy on the media service's owning thread."""
        resolved_path = ensure_inside_root(destination)
        resolved = str(resolved_path)
        existing = next((item for item in self._items.values() if item.path == resolved), None)
        if existing is not None:
            return existing
        kind = self.infer_kind(resolved_path)
        copied = MediaItem(
            path=resolved,
            kind=kind,
            metadata={"source": "data/downloads", "role": role_for_kind(kind)},
        )
        self._items[copied.id] = copied
        return copied

    def _copy_import_to_library(self, source: Path) -> MediaItem:
        """Synchronous compatibility path for existing service callers and unit tests."""
        return self._recognize_copied_import(self._copy_file_to_library(source))

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
            imports_snapshot = dict(self._import_catalog)
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
        if is_local_import:
            self._import_catalog[item.id] = item
        return item

    def is_known_path(self, path: str) -> bool:
        """Whether this exact file was imported or generated by the app."""
        return any(item.path == path for item in self._items.values())

    async def download_url(
        self,
        url: str,
        metadata: dict | None = None,
        filename_prefix: str = "",
        request_timeout: float | httpx.Timeout = 60,
    ) -> MediaItem:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("Only direct http(s) media URLs are supported")

        target: Path | None = None
        partial: Path | None = None
        downloaded_bytes = 0
        try:
            async with (
                httpx.AsyncClient(
                    follow_redirects=True,
                    timeout=request_timeout,
                ) as client,
                client.stream("GET", url) as response,
            ):
                response.raise_for_status()
                content_type = response.headers.get("content-type")
                if not _is_supported_download(str(response.url), content_type):
                    raise ValueError("Only direct video, audio, or image URLs are supported")
                expected_kind = str((metadata or {}).get("kind_hint") or "")
                if expected_kind and not _content_type_matches_kind(content_type, expected_kind):
                    raise MediaDownloadNotReadyError(f"摄像头返回的文件类型暂时不是{expected_kind}")
                target = generated_path(
                    "data",
                    "downloads",
                    f"{filename_prefix}{_safe_download_name(str(response.url), content_type)}",
                )
                target.parent.mkdir(parents=True, exist_ok=True)
                partial = target.with_name(f".{target.name}.{uuid4().hex}.part")
                with partial.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            downloaded_bytes += len(chunk)
                            handle.write(chunk)

            if not partial.is_file() or partial.stat().st_size == 0:
                raise MediaDownloadNotReadyError("服务器返回的媒体文件暂时为空")
            content_length = response.headers.get("content-length")
            if (
                content_length
                and content_length.isdigit()
                and not response.headers.get("content-encoding")
                and downloaded_bytes != int(content_length)
            ):
                raise MediaDownloadNotReadyError("服务器返回的媒体文件尚未传输完整")
            if expected_kind == "video":
                await asyncio.to_thread(validate_downloaded_video, partial)
            partial.replace(target)
        finally:
            if partial:
                partial.unlink(missing_ok=True)

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
                    raise GeneratedMetadataPersistenceError(self.generated_metadata_problem)
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
