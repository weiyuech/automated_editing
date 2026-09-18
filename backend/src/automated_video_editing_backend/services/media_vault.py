from __future__ import annotations

from automated_video_editing_backend.services.recording_segments import iter_nodes

import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from automated_video_editing_backend.core.models import (
    CleanupResult,
    MediaAsset,
    MediaCalendarDay,
    MediaItem,
    StorageBucket,
    StorageReport,
)
from automated_video_editing_backend.core.paths import APP_ROOT, GENERATED_DIRS, ensure_inside_root
from automated_video_editing_backend.services.media import (
    AUDIO_EXTS,
    IMAGE_EXTS,
    VIDEO_EXTS,
    GeneratedMetadataPersistenceError,
    MediaService,
    role_for_kind,
)

MANAGED_AREAS = {
    "downloads": GENERATED_DIRS["data"] / "downloads",
    "tts": GENERATED_DIRS["data"] / "tts",
    "seedance": GENERATED_DIRS["data"] / "seedance" / "effects",
    "seedance_cache": GENERATED_DIRS["data"] / "seedance" / "cache",
    "exports": GENERATED_DIRS["exports"],
    "previews": GENERATED_DIRS["previews"],
    "cache": GENERATED_DIRS["cache"],
}
STORAGE_THRESHOLD_BYTES = 20 * 1024 * 1024 * 1024


def _local_day(moment: datetime) -> str:
    """The calendar day an operator would call this, in their own timezone.

    The UI builds its calendar from local dates, so computing this in UTC filed anything
    timestamped before 08:00 in a +0800 timezone under the previous day.
    """
    return moment.astimezone().date().isoformat()


RENAMEABLE_ROLES = {"raw_video", "music", "tts_voice", "image", "seedance_effect", "export"}


class MediaVaultService:
    def __init__(self, media: MediaService | None = None) -> None:
        self.media = media

    def list_assets(self) -> list[MediaAsset]:
        if self.media and self.media.generated_metadata_problem:
            # Scanning exports without their valid manifest would make every delivery/master
            # pair look like unrelated flat files. Stop and surface the retained manifest error
            # instead of silently changing the meaning of the user's library.
            raise GeneratedMetadataPersistenceError(self.media.generated_metadata_problem)
        by_path: dict[str, MediaAsset] = {}
        for area, folder in MANAGED_AREAS.items():
            for asset in self._scan_area(area, folder):
                by_path[asset.path] = asset
        if self.media:
            for item in self.media.list_items():
                if item.path not in by_path:
                    asset = self._asset_from_media_item(item)
                    if asset:
                        by_path[asset.path] = asset
                        self._attach_group(asset, item)
                else:
                    # A file inside a managed folder is found by scanning, and a scan sees only
                    # the filesystem. Anything the app recorded about it — here, which finished
                    # video it is half of — lives on the media item and has to be carried over,
                    # or the pairing is invisible to the library.
                    self._attach_group(by_path[item.path], item)
        return sorted(by_path.values(), key=lambda item: item.day, reverse=True)

    def _attach_group(self, asset: MediaAsset, item: MediaItem) -> None:
        capture = item.metadata.get("capture_group")
        if capture:
            asset.capture_group = capture
            try:
                asset.day = _local_day(datetime.fromisoformat(capture["started_at"]))
            except (ValueError, TypeError):
                pass
        group = item.metadata.get("export_group")
        if not group:
            return
        asset.export_group = str(group)
        asset.variant = str(item.metadata.get("variant") or "")
        asset.variant_label = str(item.metadata.get("variant_label") or "")

    def calendar(self) -> list[MediaCalendarDay]:
        grouped: dict[str, MediaCalendarDay] = {}
        for asset in self.list_assets():
            if asset.role == "cache":
                continue
            day = grouped.setdefault(asset.day, MediaCalendarDay(day=asset.day))
            day.assets.append(asset)
            day.asset_count += 1
            day.total_bytes += asset.size_bytes
            if asset.capture_group:
                day.total_bytes += sum(s.get("size_bytes", 0) for s in iter_nodes(asset.capture_group["segments"]))
        return list(grouped.values())

    def storage_report(self) -> StorageReport:
        assets = self.list_assets()
        buckets = [self._bucket(area, path) for area, path in MANAGED_AREAS.items()]
        buckets.append(self._bucket("capture_segments", GENERATED_DIRS["data"] / "capture_segments"))
        total = sum(bucket.size_bytes for bucket in buckets)
        cleanup_candidates = [
            asset for asset in assets
            if asset.role in {"preview", "cache"}
        ]
        cleanup_candidates.sort(key=lambda asset: asset.modified_at)
        return StorageReport(
            total_bytes=total,
            threshold_bytes=STORAGE_THRESHOLD_BYTES,
            over_threshold=total >= STORAGE_THRESHOLD_BYTES,
            buckets=buckets,
            cleanup_candidates=cleanup_candidates[:50],
        )

    def safe_cleanup(self) -> CleanupResult:
        deleted_count = 0
        freed_bytes = 0
        skipped_count = 0
        safe_areas = {"previews", "cache", "seedance_cache"}
        for area in safe_areas:
            folder = MANAGED_AREAS[area]
            if not folder.exists():
                continue
            for path in sorted(folder.rglob("*"), reverse=True):
                if not path.exists():
                    continue
                try:
                    resolved = ensure_inside_root(path)
                    if resolved.is_file():
                        size = resolved.stat().st_size
                        resolved.unlink()
                        deleted_count += 1
                        freed_bytes += size
                    elif resolved.is_dir():
                        try:
                            resolved.rmdir()
                        except OSError:
                            pass
                except OSError:
                    skipped_count += 1
        if self.media is not None:
            capture_deleted, capture_freed, capture_skipped = self.media.captures.cleanup_inputs()
            deleted_count += capture_deleted
            freed_bytes += capture_freed
            skipped_count += capture_skipped
        return CleanupResult(deleted_count=deleted_count, freed_bytes=freed_bytes, skipped_count=skipped_count)

    def _scan_area(self, area: str, folder: Path) -> Iterable[MediaAsset]:
        if not folder.exists():
            return []
        assets: list[MediaAsset] = []
        for path in folder.rglob("*"):
            if path.is_file():
                if area in {"tts", "seedance"} and path.suffix.lower() == ".json":
                    continue
                # Capture timing/point notes belong to the downloaded recording. They are moved
                # to the trash as its companion and must not appear as a second, unusable asset.
                if area == "downloads" and path.name.endswith(
                    (".capture.json", ".gimbal.json", ".composition.json")
                ):
                    continue
                # The subtitle layer belongs to its export, not beside it in the library. It is
                # a couple of kilobytes of cue timings, and listing one per finished video would
                # bury the videos themselves.
                if area == "exports" and (
                    path.suffix.lower() == ".ass" or path.name.endswith(".subtitles.json")
                ):
                    continue
                assets.append(self._asset_from_path(area, path))
        return assets

    def _asset_from_path(self, area: str, path: Path) -> MediaAsset:
        resolved = ensure_inside_root(path)
        stat = resolved.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        created = datetime.fromtimestamp(stat.st_ctime, UTC)
        ext = resolved.suffix.lower()
        kind = self._kind_for_extension(ext)
        role = self._role_for(area, kind)
        return MediaAsset(
            id=self._asset_id(resolved),
            name=resolved.name,
            path=str(resolved),
            role=role,
            kind=kind,
            area=area,
            extension=ext,
            size_bytes=stat.st_size,
            created_at=created,
            modified_at=modified,
            day=_local_day(modified),
            managed=APP_ROOT == resolved or APP_ROOT in resolved.parents,
            can_delete=area in {"downloads", "tts", "seedance", "exports", "previews", "cache", "seedance_cache"},
            can_rename=role in RENAMEABLE_ROLES,
            can_preview=kind in {"video", "audio", "image"},
            can_use_as_source=role == "raw_video",
        )

    def _asset_from_media_item(self, item: MediaItem) -> MediaAsset | None:
        path = Path(item.path).expanduser()
        if not path.exists() or not path.is_file():
            capture = item.metadata.get("capture_group")
            if capture:
                moment = item.created_at
                return MediaAsset(id=item.id, name=capture["title"], path=str(path), role="raw_video", kind="video",
                                  created_at=moment, modified_at=moment, day=_local_day(moment),
                                  can_delete=False, can_rename=False, capture_group=capture)
            return None
        resolved = path.resolve()
        stat = resolved.stat()
        modified = datetime.fromtimestamp(stat.st_mtime, UTC)
        created = datetime.fromtimestamp(stat.st_ctime, UTC)
        ext = resolved.suffix.lower()
        kind = self._kind_for_extension(ext)
        role = str(item.metadata.get("role") or role_for_kind(kind))
        # An imported clip keeps the mtime of the day it was filmed, so filing it by that
        # would hide it from the day the operator actually added it.
        imported = item.metadata.get("source") == "local_import"
        day_source = item.created_at if imported else modified
        managed = APP_ROOT == resolved or APP_ROOT in resolved.parents
        return MediaAsset(
            id=item.id,
            name=resolved.name,
            path=str(resolved),
            role=role if role in {"raw_video", "music", "tts_voice", "image", "seedance_effect", "export", "preview", "cache"} else "unknown",
            kind=kind,
            area="external" if not managed else "unknown",
            extension=ext,
            size_bytes=stat.st_size,
            created_at=created,
            modified_at=modified,
            day=_local_day(day_source),
            managed=managed,
            can_delete=managed,
            can_forget=item.metadata.get("source") == "local_import",
            can_rename=role in RENAMEABLE_ROLES,
            can_preview=kind in {"video", "audio", "image"},
            can_use_as_source=role == "raw_video",
        )

    def _bucket(self, area: str, path: Path) -> StorageBucket:
        size = 0
        count = 0
        if path.exists():
            for child in path.rglob("*"):
                if child.is_file():
                    size += child.stat().st_size
                    count += 1
        return StorageBucket(
            key=area,
            label={
                "downloads": "Downloads / Raw Media",
                "tts": "Voiceovers",
                "seedance": "Seedance Effects",
                "seedance_cache": "Seedance Cache",
                "exports": "Exports",
                "previews": "Previews",
                "cache": "Cache",
            }.get(area, area.title()),
            path=str(path),
            size_bytes=size,
            file_count=count,
            deletable=area in {"downloads", "tts", "seedance", "exports", "previews", "cache", "seedance_cache"},
        )

    def _asset_id(self, path: Path) -> str:
        digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()
        return digest[:24]

    def _kind_for_extension(self, ext: str) -> str:
        if ext in VIDEO_EXTS:
            return "video"
        if ext in AUDIO_EXTS:
            return "audio"
        if ext in IMAGE_EXTS:
            return "image"
        return "file"

    def _role_for(self, area: str, kind: str) -> str:
        if area == "exports":
            return "export"
        if area == "previews":
            return "preview"
        if area == "cache":
            return "cache"
        if area == "tts" and kind == "audio":
            return "tts_voice"
        if area == "seedance" and kind in {"video", "image"}:
            # Effects can be Seedance clips or Seedream images; both belong here.
            return "seedance_effect"
        if area == "seedance_cache":
            return "cache"
        if area == "downloads" and kind == "video":
            return "raw_video"
        if area == "downloads" and kind == "audio":
            return "music"
        if area == "downloads" and kind == "image":
            return "image"
        return "unknown"
