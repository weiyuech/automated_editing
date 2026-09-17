from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.core.store import write_json
from automated_video_editing_backend.services.capture import gimbal_sidecar_path, sidecar_path
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.naming import validate_filename
from automated_video_editing_backend.services.seedance import SeedanceService


class MediaRenameService:
    """Renames a generated file and everything that points at it.

    The rename itself is trivial; the care is in the references. A file left renamed with a
    stale pointer somewhere shows up later as a mysteriously missing clip.
    """

    def __init__(self, media: MediaService, jobs: JobService, seedance: SeedanceService) -> None:
        self.media = media
        self.jobs = jobs
        self.seedance = seedance

    def rename(self, media_id: str, new_name: str) -> MediaItem:
        item = self.media.get(media_id)
        if item is None:
            raise ValueError("That media item no longer exists")

        source = Path(item.path)
        if not source.is_file():
            raise ValueError("That file no longer exists on disk")

        filename = validate_filename(new_name, source.suffix)
        if Path(filename).suffix.lower() != source.suffix.lower():
            raise ValueError(f"文件类型必须保持为 {source.suffix}")
        target = source.with_name(filename)
        if target == source:
            return item
        if target.exists():
            raise ValueError(f"'{filename}' already exists here")
        if self.jobs.is_path_in_use(str(source)) or self.jobs.is_path_in_use(str(target)):
            raise ValueError("That file is being used by a render right now")

        seedance_change = self._prepare_seedance_metadata(item, str(source), target)

        # The TTS sidecar is paired by filename, so it has to move in step.
        tts_metadata = source.with_suffix(".json")
        tts_target = target.with_suffix(".json")
        is_tts = item.metadata.get("role") == "tts_voice"
        if is_tts and tts_target.exists():
            raise ValueError(f"'{tts_target.name}' already exists here")
        move_tts_metadata = is_tts and tts_metadata.is_file()

        # A recording's notes and markers live in "<full filename>.capture.json", so they
        # are orphaned unless the sidecar moves with the video.
        capture_sidecar = sidecar_path(source)
        capture_target = sidecar_path(target)
        move_capture_sidecar = capture_sidecar.is_file()
        if item.kind == "video" and capture_target.exists():
            raise ValueError(f"'{capture_target.name}' already exists here")
        gimbal_sidecar = gimbal_sidecar_path(source)
        gimbal_target = gimbal_sidecar_path(target)
        move_gimbal_sidecar = gimbal_sidecar.is_file()
        if item.kind == "video" and gimbal_target.exists():
            raise ValueError(f"'{gimbal_target.name}' already exists here")

        # Export subtitle layers are paired by stem as well. Keeping them beside both the
        # delivery and its clean master is what lets 手动微调 rediscover the timing after a
        # restart or a rename, rather than depending on transient in-memory metadata.
        export_sidecars: list[tuple[Path, Path]] = []
        rewritten_subtitles: tuple[Path, dict] | None = None
        original_subtitles: tuple[Path, dict] | None = None
        if item.metadata.get("role") == "export":
            recorded_subtitles = item.metadata.get("subtitles_path")
            if recorded_subtitles is not None and not isinstance(recorded_subtitles, str):
                raise ValueError("成片的字幕记录无效，不能重命名")
            canonical_subtitles = source.with_suffix(".subtitles.json")
            if recorded_subtitles:
                try:
                    recorded_path = Path(recorded_subtitles).expanduser().resolve()
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ValueError("成片的字幕记录无效，不能重命名") from exc
                if (
                    str(recorded_path) != recorded_subtitles
                    or recorded_path != canonical_subtitles.resolve()
                ):
                    raise ValueError("成片的字幕记录不属于该视频，不能重命名")
                if not canonical_subtitles.is_file():
                    # Persisting the renamed video with the old, now-noncanonical path would
                    # make the whole generated manifest fail closed on the next restart.
                    raise ValueError("成片的字幕文件缺失，请恢复后再重命名")
            for suffix in (".subtitles.json", ".ass"):
                paired = source.with_suffix(suffix)
                paired_target = target.with_suffix(suffix)
                # A pre-existing companion belongs to some other file. Renaming over only the
                # video would make the new video silently adopt those unrelated words/styles.
                if paired_target.exists():
                    raise ValueError(f"'{paired_target.name}' already exists here")
                if paired.is_file():
                    if suffix == ".subtitles.json":
                        try:
                            payload = json.loads(paired.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError) as exc:
                            raise ValueError(f"{paired.name} 无法更新新的成片文件名：{exc}") from exc
                        if not isinstance(payload, dict):
                            raise ValueError(f"{paired.name} 格式不正确，无法更新成片文件名")
                        if "video" in payload:
                            binding = payload.get("video")
                            if not isinstance(binding, str) or not binding:
                                raise ValueError(f"{paired.name} 的 video 绑定无效")
                        else:
                            binding = None
                        if binding is not None and binding != source.name:
                            raise ValueError(
                                f"{paired.name} 属于另一个成片，不能随 {source.name} 重命名"
                            )
                        original_subtitles = (paired, payload)
                        rewritten_subtitles = (paired_target, {**payload, "video": target.name})
                    export_sidecars.append((paired, paired_target))

        moves = [(source, target)]
        if move_tts_metadata:
            moves.append((tts_metadata, tts_target))
        if move_capture_sidecar:
            moves.append((capture_sidecar, capture_target))
        if move_gimbal_sidecar:
            moves.append((gimbal_sidecar, gimbal_target))
        moves.extend(export_sidecars)
        self._rename_files(moves, rewritten_subtitles)

        original_metadata = deepcopy(item.metadata)
        seedance_committed = False
        try:
            if move_tts_metadata:
                item.metadata["metadata_path"] = str(tts_target)
            for paired, paired_target in export_sidecars:
                if item.metadata.get("subtitles_path") == str(paired):
                    item.metadata["subtitles_path"] = str(paired_target)
            if seedance_change is not None:
                metadata_path, _original_seedance, updated_seedance = seedance_change
                if not write_json(metadata_path, updated_seedance):
                    raise ValueError(f"{metadata_path.name} 无法更新特效文件名")
                seedance_committed = True
            self.media.repoint(media_id, str(target))
        except Exception as exc:
            # The file family moved before the path-keyed manifests were committed. Restore
            # both halves: leaving only one half rolled back makes the failure look harmless
            # until a restart drops the asset or attaches the wrong subtitle/marker document.
            item.path = str(source)
            item.metadata = original_metadata
            reverse_moves = [(new_path, old_path) for old_path, new_path in moves]
            rollback_errors: list[str] = []
            if seedance_committed and seedance_change is not None:
                metadata_path, original_seedance, _updated_seedance = seedance_change
                if not write_json(metadata_path, original_seedance):
                    rollback_errors.append(f"{metadata_path.name} 特效记录无法回滚")
            try:
                self._rename_files(reverse_moves, original_subtitles)
            except ValueError as rollback_exc:
                rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise RuntimeError(
                    f"媒体库更新失败，回滚也失败：{' | '.join(rollback_errors)}"
                ) from exc
            raise

        self._update_finished_jobs(str(source), target)
        return self.media.get(media_id) or item

    @staticmethod
    def _rename_files(
        moves: list[tuple[Path, Path]],
        rewritten_subtitles: tuple[Path, dict] | None,
    ) -> None:
        """Move one filename family without exposing a half-renamed export.

        The rewritten subtitle document is prepared first and then swapped in atomically only
        after the original video and companions have moved. If any filesystem step fails, every
        completed move is reversed before the error reaches the caller, so library and render-
        queue pointers (updated by the caller afterwards) still describe the files on disk.
        """
        staging: Path | None = None
        if rewritten_subtitles is not None:
            subtitle_target, payload = rewritten_subtitles
            staging = subtitle_target.with_name(
                f".{subtitle_target.name}.{uuid4().hex}.rename-stage"
            )
            if not write_json(staging, payload):
                raise ValueError(f"{subtitle_target.name} 无法更新新的成片文件名")

        completed: list[tuple[Path, Path]] = []
        try:
            for old_path, new_path in moves:
                old_path.rename(new_path)
                completed.append((old_path, new_path))
            if staging is not None and rewritten_subtitles is not None:
                staging.replace(rewritten_subtitles[0])
                staging = None
        except OSError as exc:
            rollback_errors: list[str] = []
            for old_path, new_path in reversed(completed):
                try:
                    new_path.rename(old_path)
                except OSError as rollback_exc:
                    rollback_errors.append(f"{new_path.name}: {rollback_exc}")
            detail = f"；回滚失败：{' | '.join(rollback_errors)}" if rollback_errors else ""
            raise ValueError(f"重命名失败，原文件已保留{detail}") from exc
        finally:
            if staging is not None:
                staging.unlink(missing_ok=True)

    def _update_finished_jobs(self, old_path: str, target: Path) -> None:
        """Keep 渲染队列 pointing at the export after it is renamed."""
        for job in self.jobs.list_jobs():
            if job.result_path == old_path:
                job.result_path = str(target)
            timeline = job.timeline
            if timeline is not None and getattr(timeline, "output_path", None) == old_path:
                timeline.output_path = str(target)

    def _prepare_seedance_metadata(
        self, item: MediaItem, old_path: str, target: Path
    ) -> tuple[Path, dict, dict] | None:
        """Prepare the required metadata update for a generated effect rename."""
        candidates: list[Path] = []
        asset_id = item.metadata.get("seedance_asset_id")
        if asset_id:
            candidates.append(self.seedance.effects_dir / f"{asset_id}.json")
        candidates.extend(
            path
            for path in self.seedance.effects_dir.glob("*.json")
            if path not in candidates
        )
        for metadata_path in candidates:
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                if asset_id and metadata_path.name == f"{asset_id}.json":
                    raise ValueError(f"{metadata_path.name} 无法读取，不能安全重命名特效") from exc
                continue
            if data.get("output_path") != old_path and data.get("video_path") != old_path:
                continue
            updated = deepcopy(data)
            updated["output_path"] = str(target)
            updated.pop("video_path", None)
            updated["name"] = target.name
            return metadata_path, data, updated
        return None
