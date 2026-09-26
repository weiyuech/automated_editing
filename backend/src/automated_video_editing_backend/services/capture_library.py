"""Durable, nested capture assets and one-input-per-recording materialization.

Child files are deliberately never registered as independent MediaItems. The manifest
owns their identity, lifecycle and source intervals; edit inputs are disposable derived
files with the same capture identity. All publication is atomic and restartable.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import shutil
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from pathlib import Path

from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services.capture import (
    gimbal_sidecar_path,
    read_sidecar,
    sidecar_path,
)
from automated_video_editing_backend.services.recording_segments import (
    MOTION_TRIM_VERSION,
    apply_motion_trim,
    build_timeline,
    iter_nodes,
    selected_nodes,
    validate_tree,
    rebase_timeline,
    selected_ranges,
)
from automated_video_editing_backend.services.render import RenderService

LOGGER = logging.getLogger(__name__)


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


class CaptureLibrary:
    def __init__(self, path: Path, directory: Path) -> None:
        self.path = path
        self.directory = directory.resolve()
        raw, problem = read_json(path)
        self.problem = problem or ""
        self.groups: dict[str, dict] = {}
        self._problem_reported = False
        if raw is not None:
            try:
                if not isinstance(raw, dict) or raw.get("version") != 1:
                    raise ValueError("拍摄分组清单格式无效")
                groups = raw.get("groups")
                if not isinstance(groups, dict):
                    raise TypeError("拍摄分组清单缺少分组数据")
                for key, group in groups.items():
                    # A process may have stopped after the timeline was committed but before
                    # its first child entered the encoder. Early development manifests did not
                    # mark those untouched entries explicitly; treat them as resumable pending
                    # work rather than disabling the entire optional grouping layer.
                    if isinstance(group, dict) and isinstance(group.get("segments"), list):
                        for child in iter_nodes(group["segments"]):
                            if isinstance(child, dict):
                                child.setdefault("status", "pending")
                                child.setdefault("error", "")
                    self._validate_group(key, group)
                    for child in iter_nodes(group["segments"]):
                        if (
                            child.get("path")
                            and self.directory not in Path(child["path"]).resolve().parents
                        ):
                            raise ValueError("拍摄子视频路径无效")
                    if group.get("status") == "generating":
                        group["status"] = "pending"
                    self.groups[key] = group
            except (
                AttributeError,
                KeyError,
                OSError,
                OverflowError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                self.problem = str(exc)
                self.groups = {}
        self.renderer = RenderService()
        self.slots = asyncio.Semaphore(1)
        self._locks: dict[str, asyncio.Lock] = {}
        self._active: set[str] = set()
        self._worker: asyncio.Task | None = None
        self.external_path_in_use: Callable[[str], bool] = lambda _path: False

    @staticmethod
    def _validate_group(key: object, group: object) -> None:
        """Reject a corrupt derived record before it can break ordinary media listing."""
        if not isinstance(key, str) or not isinstance(group, dict) or key != group.get("id"):
            raise ValueError("拍摄分组结构无效")
        required_strings = ("title", "master_path", "fingerprint", "status")
        if any(
            not isinstance(group.get(field), str) or not group[field] for field in required_strings
        ):
            raise ValueError("拍摄分组字段无效")
        if group["status"] not in {"pending", "generating", "ready", "failed"}:
            raise ValueError("拍摄分组状态无效")
        if not isinstance(group.get("evidence"), dict) or not isinstance(
            group.get("segments"), list
        ):
            raise TypeError("拍摄分组证据或分段无效")
        for field in ("duration", "offset_seconds"):
            value = group.get(field)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("拍摄分组时间字段无效")
        if group["duration"] < 0:
            raise ValueError("拍摄分组时长无效")
        if group["segments"] and (
            not isinstance(group.get("timeline_version"), str)
            or not group["timeline_version"]
            or group["duration"] <= 0
        ):
            raise ValueError("拍摄分组时间轴版本无效")
        seen_ids: set[str] = set()
        validate_tree(group["segments"], 0, group["duration"])
        for child in iter_nodes(group["segments"]):
            if not isinstance(child, dict):
                raise TypeError("拍摄子视频结构无效")
            if (
                not isinstance(child.get("id"), str)
                or not child["id"]
                or child["id"] in seen_ids
                or not isinstance(child.get("order"), int)
                or isinstance(child["order"], bool)
                or child["order"] < 0
                or child.get("status") not in {"pending", "virtual", "ready", "failed"}
                or not isinstance(child.get("kind"), str)
                or not isinstance(child.get("label"), str)
                or not isinstance(child.get("boundary_source"), str)
                or not isinstance(child.get("error"), str)
                or (child.get("path") is not None and not isinstance(child.get("path"), str))
            ):
                raise TypeError("拍摄子视频字段无效")
            if child["status"] == "ready" and not child.get("path"):
                raise ValueError("已生成的拍摄子视频缺少路径")
            start, end = child.get("start"), child.get("end")
            if (
                any(
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    for value in (start, end)
                )
                or end <= start
                or start < 0
                or end > group["duration"] + 1e-6
            ):
                raise ValueError("拍摄子视频时间范围无效")
            seen_ids.add(child["id"])

    def _commit(self, group: dict) -> None:
        if self.problem:
            raise ValueError(self.problem)
        groups = {**self.groups, group["id"]: group}
        if not write_json(self.path, {"version": 1, "groups": groups}):
            raise OSError("拍摄分组信息保存失败，完整录制仍保留")
        self.groups = groups

    def _remove_group(self, key: str) -> None:
        if self.problem:
            raise ValueError(self.problem)
        groups = {group_id: group for group_id, group in self.groups.items() if group_id != key}
        if not write_json(self.path, {"version": 1, "groups": groups}):
            raise OSError("拍摄分组信息保存失败，完整录制仍保留")
        self.groups = groups

    def enrich(self, items: dict[str, MediaItem]) -> None:
        if self.problem:
            # This manifest describes optional derived children. A bad auxiliary file must
            # never make intact originals disappear from the base media library.
            if not self._problem_reported:
                LOGGER.error(
                    "Capture grouping disabled; base media remains available: %s", self.problem
                )
                self._problem_reported = True
            return
        for item in list(items.values()):
            if (
                item.kind != "video"
                or item.metadata.get("role") != "raw_video"
                or item.metadata.get("capture_input")
            ):
                continue
            payload = read_sidecar(item.path)
            if (
                not payload
                or not payload.get("capture_session_id")
                or not payload.get("segments")
                # Releases before recording-event capture already wrote legacy cruise spans.
                # Automatically transcoding every historical recording on upgrade would be a
                # surprising migration and could monopolise the encoder for hours. Only footage
                # recorded with the new event clock is enrolled automatically.
                or not payload.get("recording_events")
            ):
                continue
            source = Path(item.path)
            if not source.is_file():
                continue
            stat = source.stat()
            key = str(payload["capture_session_id"])
            fingerprint = digest(
                [
                    stat.st_size,
                    stat.st_mtime_ns,
                    payload.get("segments"),
                    payload.get("recording_events"),
                    payload.get("recording_clock"),
                ]
            )
            existing = self.groups.get(key)
            if existing is None or existing["fingerprint"] != fingerprint:
                obsolete = (
                    []
                    if existing is None
                    else [
                        *(existing.get("obsolete_paths") or []),
                        *(
                            segment.get("path")
                            for segment in iter_nodes(existing["segments"])
                            if segment.get("path")
                        ),
                    ]
                )
                group = {
                    "id": key,
                    "title": payload.get("title") or source.stem,
                    "started_at": payload.get("started_at"),
                    "master_path": str(source),
                    "fingerprint": fingerprint,
                    "status": "pending",
                    "error": "",
                    "duration": 0.0,
                    "offset_seconds": 0.0,
                    "segments": [],
                    "evidence": payload,
                    "timing_note": "边界来自机器人反馈与应用计时；相机起始时间为估计值",
                }
                if obsolete:
                    group["obsolete_paths"] = list(dict.fromkeys(obsolete))
                self._commit(group)
            elif existing["master_path"] != str(source):
                self._commit({**existing, "master_path": str(source)})
        by_path = {item.path: item for item in items.values()}
        for key in list(self.groups):
            group = self.groups[key]
            if self._needs_timeline_upgrade(group) and not self._timeline_in_use(group):
                if group["status"] == "ready":
                    group = {**group, "status": "pending"}
                    self._commit(group)
            if group.get("status") == "ready" and group.get("obsolete_paths"):
                updated = deepcopy(group)
                remaining = self._cleanup_obsolete(updated)
                if remaining:
                    updated["obsolete_paths"] = remaining
                else:
                    updated.pop("obsolete_paths", None)
                if updated != group:
                    self._commit(updated)
                    group = updated
            item = by_path.get(group["master_path"])
            master_exists = Path(group["master_path"]).is_file()
            children_exist = any(
                s.get("path") and Path(s["path"]).is_file() for s in iter_nodes(group["segments"])
            )
            if item is None and not master_exists and not children_exist:
                # Once every user-facing member has gone, remove disposable selection inputs
                # and their derived manifest record. A running render keeps the group until a
                # later inventory refresh, so deleting a library row cannot pull its input away.
                if self._purge_empty_group(key):
                    self._remove_group(key)
                continue
            if item is None:
                item = MediaItem(
                    id="capture-" + digest(group["id"]),
                    path=group["master_path"],
                    kind="video",
                    metadata={"role": "raw_video", "source": "data/downloads"},
                )
                items[item.id] = item
            public = {
                k: deepcopy(v) for k, v in group.items()
                if k not in {"evidence", "obsolete_paths", "previous_timeline"}
            }
            public["master_available"] = master_exists
            public["size_bytes"] = Path(item.path).stat().st_size if master_exists else 0
            for segment in iter_nodes(public["segments"]):
                path = Path(segment["path"]) if segment.get("path") else None
                available = bool(path and path.is_file())
                segment["available"] = available
                segment["size_bytes"] = path.stat().st_size if available else 0
                if segment.get("status") == "ready" and not available:
                    segment["status"] = "missing"
            item.metadata["capture_group"] = public

    def _group_directory(self, key: str) -> Path:
        return self.directory / digest(key)

    def _directory_in_use(self, directory: Path) -> bool:
        resolved = directory.resolve()
        return any(
            Path(active).resolve() == resolved or resolved in Path(active).resolve().parents
            for active in self._active
        )

    def _purge_empty_group(self, key: str) -> bool:
        """Delete only app-derived files after every visible member has disappeared."""
        folder = self._group_directory(key)
        if not folder.exists():
            return True
        if self._directory_in_use(folder):
            return False
        media_paths = [path for path in folder.rglob("*.mp4") if path.is_file()]
        if any(self.external_path_in_use(str(path)) for path in media_paths):
            return False
        try:
            shutil.rmtree(folder)
        except OSError:
            LOGGER.exception("Could not remove empty capture group: %s", key)
            return False
        return True

    def cleanup_inputs(self) -> tuple[int, int, int]:
        """Remove disposable selection composites, never published capture children."""
        deleted_count = 0
        freed_bytes = 0
        skipped_count = 0
        for key in list(self.groups):
            inputs = self._group_directory(key) / "inputs"
            if not inputs.exists():
                continue
            if self._directory_in_use(inputs):
                skipped_count += 1
                continue
            videos = [path for path in inputs.glob("*.mp4") if path.is_file()]
            busy = {path for path in videos if self.external_path_in_use(str(path))}
            for video in videos:
                if video in busy:
                    skipped_count += 1
                    continue
                for path in (video, sidecar_path(video), gimbal_sidecar_path(video)):
                    try:
                        if path.is_file():
                            freed_bytes += path.stat().st_size
                            path.unlink()
                            deleted_count += 1
                    except OSError:
                        skipped_count += 1
            # Staging/filter files are safe only when no generator owns this directory.
            for pattern in ("*.part.mp4", "*.filters.txt"):
                for path in inputs.glob(pattern):
                    try:
                        if path.is_file():
                            freed_bytes += path.stat().st_size
                            path.unlink()
                            deleted_count += 1
                    except OSError:
                        skipped_count += 1
            try:
                inputs.rmdir()
            except OSError:
                pass
        return deleted_count, freed_bytes, skipped_count

    async def start(self, inventory) -> None:
        async def work():
            while True:
                try:
                    inventory()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Capture inventory refresh failed")
                for key in list(self.groups):
                    if self.groups[key]["status"] != "pending":
                        continue
                    try:
                        if self.groups[key].get("materialize_requested"):
                            await self.prepare(key)
                        else:
                            await self.ensure_timeline(key)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        # One missing/corrupt recording must not starve every later capture in
                        # the durable queue. prepare() records its own visible failed state.
                        LOGGER.exception("Capture preparation failed: %s", key)
                await asyncio.sleep(2)

        self._worker = asyncio.create_task(work())

    async def close(self) -> None:
        if self._worker:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    def is_path_in_use(self, path: str) -> bool:
        return str(Path(path).resolve()) in self._active

    def retry(self, key: str, offset: float | None = None) -> None:
        group = deepcopy(self.groups[key])
        protected_paths = [
            group["master_path"],
            *(
                segment.get("path")
                for segment in iter_nodes(group["segments"])
                if segment.get("path")
            ),
        ]
        if any(
            self.is_path_in_use(path) or self.external_path_in_use(path) for path in protected_paths
        ):
            raise ValueError("这次拍摄正在生成视频，请稍后再试")
        if offset is not None:
            if not math.isfinite(offset) or abs(offset) > 120:
                raise ValueError("校准偏移须在 -120 到 120 秒之间")
            group["obsolete_paths"] = list(
                dict.fromkeys(
                    [
                        *(group.get("obsolete_paths") or []),
                        *(
                            segment.get("path")
                            for segment in iter_nodes(group["segments"])
                            if segment.get("path")
                        ),
                    ]
                )
            )
            group["offset_seconds"] = offset
            group["segments"] = []
            group.pop("timeline_version", None)
        # Missing ready children were explicitly removed or moved. Only this explicit
        # action re-creates them; ordinary scans and restarts never do so.
        for segment in iter_nodes(group["segments"]):
            if not segment.get("path") or not Path(segment["path"]).is_file():
                segment["status"] = "pending"
        group.update(status="pending", error="", materialize_requested=offset is None)
        self._commit(group)

    @asynccontextmanager
    async def _group_lock(self, key, progress=None):
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked() and progress:
            progress({"stage": "waiting", "message": "等待此录制的处理完成"})
        async with lock:
            yield

    @staticmethod
    def _needs_timeline_upgrade(group: dict) -> bool:
        return (
            group.get("motion_trim_version") != MOTION_TRIM_VERSION
            and bool(group.get("timeline_version"))
            and any(isinstance(visit, dict) and visit.get("shots")
                    for visit in group["evidence"].get("segments", []))
            and Path(group["master_path"]).is_file()
        )

    def _timeline_in_use(self, group: dict) -> bool:
        return any(
            self.is_path_in_use(path) or self.external_path_in_use(path)
            for path in [group["master_path"], *(
                node["path"] for node in iter_nodes(group["segments"]) if node.get("path")
            )]
        )

    async def ensure_timeline(self, key: str, progress=None) -> dict:
        """Read source timing once. Tree nodes are intervals, not pre-encoded files."""
        current = self.groups[key]
        if (current.get("timeline_version") and current["duration"] > 0
                and not self._needs_timeline_upgrade(current)):
            if current["status"] == "pending" and not current.get("materialize_requested"):
                current = deepcopy(current)
                current.update(status="ready", error="")
                for segment in iter_nodes(current["segments"]):
                    if segment.get("status") == "pending" and not segment.get("path"):
                        segment["status"] = "virtual"
                self._commit(current)
            return deepcopy(current)
        async with self._group_lock(key, progress):
            group = deepcopy(self.groups[key])
            upgrading = self._needs_timeline_upgrade(group)
            if group.get("timeline_version") and group["duration"] > 0 and not upgrading:
                return group
            if upgrading:
                if self._timeline_in_use(group):
                    raise ValueError("这次拍摄正在使用，请稍后再更新镜头区间")
                # Publish the new tree atomically. Preserve the previous tree and
                # materialized files; saved compositions and originals are untouched.
                group["previous_timeline"] = {
                    "timeline_version": group["timeline_version"],
                    "offset_seconds": group["offset_seconds"],
                    "segments": deepcopy(group["segments"]),
                }
            master = Path(group["master_path"])
            self._active.add(str(master.resolve()))
            try:
                if progress:
                    progress({"stage": "preparing", "message": "读取录制时长与片段区间"})
                if not master.is_file():
                    raise ValueError("完整录制文件不存在，无法建立分段时间轴")
                duration = await asyncio.to_thread(self.renderer.probe_duration, str(master))
                if not duration or not math.isfinite(duration):
                    raise ValueError("无法读取完整录制的有效时长")
                group["duration"] = duration
                group["segments"] = build_timeline(
                    group["evidence"], duration, group["offset_seconds"]
                )
                samples: list[tuple[float, float, float, float]] = []
                track, _ = read_json(gimbal_sidecar_path(group["master_path"]))
                if isinstance(track, dict):
                    for sample in track.get("samples") or []:
                        if isinstance(sample, list) and len(sample) >= 3:
                            try:
                                zoom = float(sample[3]) if len(sample) >= 4 else 0.0
                                values = (float(sample[0]), float(sample[1]), float(sample[2]), zoom)
                            except (TypeError, ValueError, OverflowError):
                                continue
                            samples.append(
                                values
                            )
                group["segments"] = apply_motion_trim(
                    group["segments"], samples, group["offset_seconds"]
                )
                for segment in iter_nodes(group["segments"]):
                    segment.update(status="virtual", error="")
                group["timeline_version"] = digest(
                    [MOTION_TRIM_VERSION, group["fingerprint"], group["offset_seconds"], group["segments"]]
                )
                group["motion_trim_version"] = MOTION_TRIM_VERSION
                group["timing_note"] = (
                    "准备仅用于下一镜头的起点就位；镜头前后等待另行标注。"
                    "反馈不足时保留完整镜头，可能包含等待；相机起始时间仍为估计值。"
                )
                group.update(status="ready", error="")
                self._commit(group)
                return deepcopy(group)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                group.update(status="failed", error=str(exc))
                self._commit(group)
                raise
            finally:
                self._active.discard(str(master.resolve()))

    async def prepare(self, key: str) -> dict:
        """Explicitly materialize children only when the operator requests it."""
        await self.ensure_timeline(key)
        async with self._group_lock(key):
            group = deepcopy(self.groups[key])
            master = Path(group["master_path"])
            self._active.add(str(master.resolve()))
            try:
                if not master.is_file():
                    raise ValueError("完整录制文件不存在；已生成的子视频仍可使用")
                group.update(status="generating", error="")
                self._commit(deepcopy(group))
                # A published child is immutable. Calibration creates a new timeline version so
                # a queued render can never observe a file changing underneath it.
                folder = self.directory / digest(key) / group["timeline_version"]
                folder.mkdir(parents=True, exist_ok=True)
                for segment in iter_nodes(group["segments"]):
                    # Never regenerate an already-published child just because it was trashed.
                    if segment.get("status") == "ready":
                        continue
                    target = folder / f"{digest(segment['id'])}.mp4"
                    self._active.add(str(target))
                    try:
                        await self._encode(master, [(segment["start"], segment["end"])], target)
                        self._write_evidence(group, target, [(segment["start"], segment["end"])])
                        segment.update(path=str(target), status="ready", error="")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - record one child failure and continue
                        segment.update(status="failed", error=str(exc))
                    finally:
                        self._active.discard(str(target))
                    self._commit(deepcopy(group))
                group["status"] = (
                    "failed"
                    if any(s.get("status") == "failed" for s in iter_nodes(group["segments"]))
                    else "ready"
                )
                group["error"] = (
                    "部分子视频生成失败，可重试；完整录制可继续使用"
                    if group["status"] == "failed"
                    else ""
                )
                # Analysis reads the exact same segmentation as the UI and file generator.
                payload = read_sidecar(master) or group["evidence"]
                if not write_json(
                    sidecar_path(master), {**payload, "recording_timeline": group["segments"]}
                ):
                    raise OSError("录制时间轴保存失败")
                if group["status"] == "ready":
                    remaining = self._cleanup_obsolete(group)
                    if remaining:
                        group["obsolete_paths"] = remaining
                    else:
                        group.pop("obsolete_paths", None)
                group["materialize_requested"] = False
                self._commit(group)
                return group
            except asyncio.CancelledError:
                group["status"] = "pending"
                self._commit(group)
                raise
            except Exception as exc:
                group.update(status="failed", error=str(exc))
                self._commit(group)
                raise
            finally:
                self._active.discard(str(master.resolve()))

    def _cleanup_obsolete(self, group: dict) -> list[str]:
        """Remove superseded derived children only after their replacement is complete."""
        current = {
            str(Path(s["path"]).resolve()) for s in iter_nodes(group["segments"]) if s.get("path")
        }
        remaining: list[str] = []
        for raw_path in group.get("obsolete_paths") or []:
            try:
                path = Path(raw_path).resolve()
            except (OSError, RuntimeError, ValueError):
                continue
            if (
                str(path) in current
                or self.directory not in path.parents
                or self.is_path_in_use(str(path))
                or self.external_path_in_use(str(path))
            ):
                remaining.append(raw_path)
                continue
            try:
                path.unlink(missing_ok=True)
                sidecar_path(path).unlink(missing_ok=True)
                gimbal_sidecar_path(path).unlink(missing_ok=True)
            except OSError:
                remaining.append(raw_path)
        return list(dict.fromkeys(remaining))

    def validate_selection(self, selection: dict) -> None:
        group = self.groups.get(selection["capture_id"])
        if group is None:
            raise ValueError("拍摄分组不存在，请刷新媒体库")
        if not group["segments"]:
            if not selection.get("include_full"):
                raise ValueError("分段时间轴正在准备，请稍后选择子片段")
            return
        selected_ranges(group, selection)

    async def resolve(self, item: MediaItem, selection: dict, progress=None) -> MediaItem:
        self.validate_selection(selection)
        key = selection["capture_id"]
        group = self.groups[key]
        if not group["segments"]:
            if selection.get("include_full") and Path(item.path).is_file():
                return item
            group = await self.ensure_timeline(key, progress)
        ranges = selected_ranges(group, selection)
        selected_ids = set(selection.get("segment_ids") or [])
        selected_children = selected_nodes(group["segments"], selected_ids)
        if ranges == [(0.0, group["duration"])]:
            if Path(item.path).is_file():
                return item
            if selection.get("include_full"):
                raise ValueError("完整录制已移走或删除，请选择仍可用的子片段")
        async with self._group_lock(key, progress):
            signature = digest([group["fingerprint"], group["timeline_version"], ranges])
            target = self.directory / digest(key) / "inputs" / f"{signature}.mp4"
            master = Path(group["master_path"])
            child_paths_in_use: set[str] = set()
            self._active.update([str(master.resolve()), str(target)])
            try:
                if not target.is_file() or not sidecar_path(target).is_file():
                    if master.is_file():
                        await self._encode(
                            master, ranges, target, **({"progress": progress} if progress else {})
                        )
                        self._write_evidence(group, target, ranges)
                    else:
                        if not selected_children:
                            raise ValueError("完整录制已移走或删除，请选择仍可用的子片段")
                        sources = [Path(segment.get("path") or "") for segment in selected_children]
                        if any(not source.is_file() for source in sources):
                            raise ValueError("所选子片段有文件缺失，请恢复原片或重新选择")
                        child_paths_in_use = {str(source.resolve()) for source in sources}
                        self._active.update(child_paths_in_use)
                        if len(sources) == 1:
                            return self._resolved_item(
                                item,
                                selection,
                                key,
                                signature,
                                sources[0],
                                ranges,
                                group,
                            )
                        await self._concat_children(
                            sources, target, **({"progress": progress} if progress else {})
                        )
                        self._write_evidence_from_children(
                            group, target, ranges, selected_children, sources
                        )
            finally:
                self._active.difference_update([str(master.resolve()), str(target)])
                self._active.difference_update(child_paths_in_use)
            return self._resolved_item(item, selection, key, signature, target, ranges, group)

    @staticmethod
    def _resolved_item(
        item: MediaItem,
        selection: dict,
        key: str,
        signature: str,
        path: Path,
        ranges: list[tuple[float, float]],
        group: dict,
    ) -> MediaItem:
        return MediaItem(
            id="capture-input-" + signature,
            path=str(path),
            kind="video",
            metadata={
                "role": "raw_video",
                "source": "capture_input",
                "capture_input": True,
                "capture_id": key,
                "parent_path": item.path,
                "selection": selection,
                "source_ranges": ranges,
                "capture_title": group["title"],
            },
        )

    def _write_evidence(self, group: dict, target: Path, ranges: list[tuple[float, float]]) -> None:
        track, _ = read_json(gimbal_sidecar_path(group["master_path"]))
        target_gimbal = gimbal_sidecar_path(target)
        if isinstance(track, dict) and track.get("samples"):
            samples, cursor = [], 0.0
            for start, end in ranges:
                for sample in track["samples"]:
                    if not isinstance(sample, list) or len(sample) < 3:
                        continue
                    t = float(sample[0])
                    if start <= t < end:
                        zoom = float(sample[3]) if len(sample) >= 4 else 0.0
                        samples.append(
                            [cursor + t - start, float(sample[1]), float(sample[2]), zoom]
                        )
                cursor += end - start
            if not write_json(target_gimbal, {"samples": samples}):
                raise OSError("子视频云台轨迹保存失败")
        else:
            target_gimbal.unlink(missing_ok=True)
        # The capture sidecar is the enrollment marker, so publish it last.
        self._write_capture_evidence(group, target, ranges)

    def _write_evidence_from_children(
        self,
        group: dict,
        target: Path,
        ranges: list[tuple[float, float]],
        segments: list[dict],
        sources: list[Path],
    ) -> None:
        samples: list[list[float]] = []
        cursor = 0.0
        for segment, source in zip(segments, sources, strict=True):
            track, _ = read_json(gimbal_sidecar_path(source))
            if isinstance(track, dict):
                for sample in track.get("samples") or []:
                    if isinstance(sample, list) and len(sample) >= 3:
                        zoom = float(sample[3]) if len(sample) >= 4 else 0.0
                        samples.append(
                            [cursor + float(sample[0]), float(sample[1]), float(sample[2]), zoom]
                        )
            cursor += segment["end"] - segment["start"]
        target_gimbal = gimbal_sidecar_path(target)
        if samples:
            if not write_json(target_gimbal, {"samples": samples}):
                raise OSError("组合视频云台轨迹保存失败")
        else:
            target_gimbal.unlink(missing_ok=True)
        # The capture sidecar is the enrollment marker, so publish it last.
        self._write_capture_evidence(group, target, ranges)

    def _write_capture_evidence(
        self, group: dict, target: Path, ranges: list[tuple[float, float]]
    ) -> None:
        payload = {
            "capture_session_id": group["id"],
            "title": group["title"],
            "started_at": group["started_at"],
            "parent_path": group["master_path"],
            "source_ranges": ranges,
            "recording_timeline": rebase_timeline(group["segments"], ranges),
            "markers": [],
            "segments": [],
            "notes": group["evidence"].get("notes", []),
        }
        if not write_json(sidecar_path(target), payload):
            raise OSError("子视频来源信息保存失败")

    async def _concat_children(self, sources: list[Path], target: Path, progress=None) -> None:
        """Join already-published children when the complete recording is offline."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part.mp4")
        filters_path = target.with_suffix(".filters.txt")
        if progress:
            progress({"stage": "waiting", "message": "等待片段处理资源"})
        async with self.slots:
            audio_flags = [
                await asyncio.to_thread(self.renderer.has_audio_stream, str(source))
                for source in sources
            ]
            if any(flag is None for flag in audio_flags):
                raise ValueError("无法确认所选子片段的声音轨，未发布组合视频")
            if len(set(audio_flags)) != 1:
                raise ValueError("所选子片段的声音轨不一致，无法安全组合")
            has_audio = audio_flags[0]
            inputs = "".join(
                f"[{index}:v:0]" + (f"[{index}:a:0]" if has_audio else "")
                for index in range(len(sources))
            )
            suffix = "[v][a]" if has_audio else "[v]"
            filters_path.write_text(
                f"{inputs}concat=n={len(sources)}:v=1:a={int(has_audio)}{suffix}",
                encoding="utf-8",
            )
            args = [self.renderer.ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y"]
            for source in sources:
                args += ["-i", str(source)]
            # FFmpeg 9 removed the deprecated -filter_complex_script spelling. The slash form
            # asks FFmpeg 8+ (including our pinned Windows build) to read the option value from
            # a file, preserving Windows command-line headroom for a long capture timeline.
            args += ["-/filter_complex", str(filters_path), "-map", "[v]"]
            if has_audio:
                args += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
            args += [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-threads",
                "2",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
            try:
                source_durations = [
                    await asyncio.to_thread(self.renderer.probe_duration, str(source))
                    for source in sources
                ]
                if any(
                    duration is None or not math.isfinite(duration) or duration <= 0
                    for duration in source_durations
                ):
                    raise ValueError("无法确认所选子片段的时长，未发布组合视频")
                expected = sum(source_durations)
                temporary.unlink(missing_ok=True)  # This call owns the unpublished staging path.
                await self.renderer.encode(args, duration=expected, progress=progress)
                duration = await asyncio.to_thread(self.renderer.probe_duration, str(temporary))
                if not duration or not expected or abs(duration - expected) > 0.15:
                    raise ValueError("组合视频时长校验失败，未发布不完整文件")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
                filters_path.unlink(missing_ok=True)

    async def _encode(
        self, source: Path, ranges: list[tuple[float, float]], target: Path, progress=None
    ) -> None:
        """Decode exact intervals; concat picture and sound on the same zero-based clock."""
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".part.mp4")
        filters_path = target.with_suffix(".filters.txt")
        if progress:
            progress({"stage": "waiting", "message": "等待片段处理资源"})
        async with self.slots:
            has_audio = await asyncio.to_thread(self.renderer.has_audio_stream, str(source))
            if has_audio is None:
                raise ValueError("无法确认源视频的声音轨，未发布子视频")
            filters, inputs = [], []
            for i, (start, end) in enumerate(ranges):
                filters.append(
                    f"[0:v:0]setpts=PTS-STARTPTS,trim=start={start:.9f}:end={end:.9f},setpts=PTS-STARTPTS[v{i}]"
                )
                inputs.append(f"[v{i}]")
                if has_audio:
                    filters.append(
                        f"[0:a:0]aresample=async=1:first_pts=0,atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS,apad=whole_dur={end - start:.9f},atrim=duration={end - start:.9f}[a{i}]"
                    )
                    inputs.append(f"[a{i}]")
            filters.append(
                f"{''.join(inputs)}concat=n={len(ranges)}:v=1:a={int(has_audio)}[v]"
                + ("[a]" if has_audio else "")
            )
            filters_path.write_text(";\n".join(filters), encoding="utf-8")
            args = [
                self.renderer.ffmpeg_binary(),
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-/filter_complex",
                str(filters_path),
                "-map",
                "[v]",
            ]
            if has_audio:
                args += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
            args += [
                "-c:v",
                "libx264",
                "-preset",
                "fast",
                "-crf",
                "18",
                "-threads",
                "2",
                "-movflags",
                "+faststart",
                str(temporary),
            ]
            try:
                temporary.unlink(missing_ok=True)  # This call owns the unpublished staging path.
                await self.renderer.encode(
                    args, duration=sum(end - start for start, end in ranges), progress=progress
                )
                duration = await asyncio.to_thread(self.renderer.probe_duration, str(temporary))
                expected = sum(end - start for start, end in ranges)
                if not duration or abs(duration - expected) > 0.15:
                    raise ValueError("子视频时长校验失败，未发布不完整文件")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
                filters_path.unlink(missing_ok=True)
