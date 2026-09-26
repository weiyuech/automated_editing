"""Reviewable, immutable concatenation snapshots with nested source provenance."""

from __future__ import annotations

import asyncio
import shutil
import time
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from automated_video_editing_backend.core.composition import CompositionRequest
from automated_video_editing_backend.services.composition_assets import (
    manifest_path,
    read_manifest,
    save_manifest,
)
from automated_video_editing_backend.core.models import (
    EditJobRequest,
    EditTimeline,
    TimelineClip,
)
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services.capture import read_sidecar
from automated_video_editing_backend.services.capture_library import digest
from automated_video_editing_backend.services.naming import safe_stem
from automated_video_editing_backend.services.narration_context import original_recording_ids
from automated_video_editing_backend.services.recording_segments import (
    rebase_timeline,
    selected_ranges,
)


def file_identity(path: str) -> list:
    source = Path(path).resolve(strict=True)
    stat = source.stat()
    return [str(source), stat.st_size, stat.st_mtime_ns]


def compact_tree(nodes: list[dict], prefix: str, offset: float) -> list[dict]:
    """Rejoin split parents after removing unselected intervals; keep children nested."""
    grouped = {}
    for node in nodes:
        key = node["id"]
        if key not in grouped:
            grouped[key] = {**node, "children": list(node.get("children", []))}
        else:
            grouped[key]["end"] = node["end"]
            grouped[key]["children"].extend(node.get("children", []))
    return [
        {
            "id": prefix + "/" + key,
            "label": node["label"],
            "kind": node["kind"],
            "start": node["start"] + offset,
            "end": node["end"] + offset,
            "duration": node["end"] - node["start"],
            "children": compact_tree(node.get("children", []), prefix, offset),
            "notes": node.get("notes", []),
            **(
                {"source_recording_ids": node["source_recording_ids"]}
                if node.get("source_recording_ids")
                else {}
            ),
        }
        for key, node in grouped.items()
    ]


class CompositionService:
    def __init__(self, jobs, directory: Path | None = None):
        self.jobs, self.media, self.renderer = jobs, jobs.media, jobs.renderer
        self.directory = directory or GENERATED_DIRS["data"] / "compositions"
        self.records = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._confirm_lock = asyncio.Lock()
        # Automatic batches share one preparation lane, including across API requests.
        self._automatic_build_slot = asyncio.Semaphore(1)
        if self.directory.exists():
            for path in self.directory.glob("*.json"):
                raw, _ = read_json(path)
                if not isinstance(raw, dict) or raw.get("id") != path.stem:
                    continue
                if raw.get("status") in {"queued", "building"}:
                    raw.update(status="failed", error="预览生成被中断，请重新生成")
                if raw.get("narration", {}).get("status") == "running":
                    raw["narration"].update(
                        status="failed", error="旁白合成被中断，请检查后重新完整合成"
                    )
                self.records[raw["id"]] = raw

    def save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        if not write_json(self.directory / f"{record['id']}.json", record):
            raise OSError("剪辑预览状态保存失败")
        self.records[record["id"]] = record

    def list(self):
        return [self.public(r) for r in reversed(list(self.records.values()))]

    @staticmethod
    def public(record):
        elapsed = max(
            0, (record.get("finished_at") or time.time()) - record.get("started_at", time.time())
        )
        return {
            "elapsed_seconds": round(elapsed, 1),
            **{
                key: deepcopy(value)
                for key, value in record.items()
                if key not in {"timeline", "files", "groups"}
            },
        }

    def get(self, key):
        if key not in self.records:
            raise ValueError("剪辑预览不存在")
        return self.records[key]

    async def create(self, request: CompositionRequest, *, automatic_snapshot: dict | None = None):
        self.media.list_items()
        if request.purpose == "library":
            # Selection order is the displayed source/tree order, never click order.
            by_id = {item.id: item for item in self.media.list_items()}
            if any(key not in by_id for key in request.media_ids):
                raise ValueError("素材已改变，请刷新媒体库")
            request = request.model_copy(
                update={
                    "media_ids": sorted(
                        request.media_ids,
                        key=lambda key: (by_id[key].metadata["composition_order"], by_id[key].path),
                    ),
                    "music_media_id": None,
                    "voiceover_media_id": None,
                    "intro_effect_media_id": None,
                    "outro_effect_media_id": None,
                    "subtitles": False,
                    "mute_original_audio": False,
                    "output_aspect_ratio": None,
                    "output_crop_x": None,
                    "output_crop_y": None,
                }
            )
        if len(set(request.media_ids)) != len(request.media_ids):
            raise ValueError("同一录制不能重复作为输入")
        if len({s.capture_id for s in request.capture_selections}) != len(
            request.capture_selections
        ):
            raise ValueError("同一录制不能提供两份选择")
        record = {
            "id": str(uuid4()),
            "title": request.title,
            "request": request.model_dump(),
            "status": "queued",
            "progress": 0,
            "message": "等待准备预览",
            "stage": "preparing",
            "started_at": time.time(),
            "tree": [],
            "error": "",
        }
        if automatic_snapshot is not None:
            record.update(
                automatic=deepcopy(automatic_snapshot["metadata"]),
                files=deepcopy(automatic_snapshot["files"]),
                groups=deepcopy(automatic_snapshot["groups"]),
                source_recording_ids=deepcopy(automatic_snapshot["source_recording_ids"]),
            )
        self.save(record)
        build = (
            self._build_automatic(record, request, automatic_snapshot)
            if automatic_snapshot is not None else self._build(record, request)
        )
        self.tasks[record["id"]] = asyncio.create_task(build)
        return self.public(record)

    async def _build_automatic(self, record, request, snapshot):
        """Keep queued snapshots leased and validate them before any expensive preparation."""
        try:
            async with self._automatic_build_slot:
                self.validate_files(record)
                await self._build(record, request, expected_groups=snapshot["groups"])
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            record.update(
                status="cancelled" if cancelled else "failed",
                stage="cancelled" if cancelled else "failed",
                message="预览已取消" if cancelled else "剪辑预览未完成",
                error="" if cancelled else str(exc),
                finished_at=time.time(),
            )
            self.save(record)
            if cancelled:
                raise
        finally:
            self.tasks.pop(record["id"], None)

    def _progress(self, record, detail):
        # Updates stay in memory while running; stage boundaries and terminal states persist.
        # A slow disk must not turn every FFmpeg progress frame into another bottleneck.
        stage = detail.get("stage", "encoding")
        defaults = {
            "waiting": "等待可用编码资源",
            "encoding": "正在生成预览",
            "verifying": "校验预览时长",
        }
        record.update({key: value for key, value in detail.items() if key != "fraction"})
        record["message"] = detail.get("message", defaults.get(stage, "准备预览"))
        if "fraction" in detail:
            record["progress"] = 0.45 + 0.5 * max(0, min(1, detail["fraction"]))

    async def cancel(self, key):
        record = self.get(key)
        if (
            record.get("job_id")
            or record.get("material_path")
            or record["status"] not in {"queued", "building"}
        ):
            return self.public(record)
        task = self.tasks.get(key)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            self.tasks.pop(key, None)
        record.update(
            status="cancelled",
            stage="cancelled",
            message="已取消预览生成",
            error="",
            finished_at=time.time(),
        )
        self.save(record)
        return self.public(record)

    async def _build(self, record, request, *, expected_groups=None):
        preview = GENERATED_DIRS["previews"] / f"composition-{record['id']}.mp4"
        try:
            record.update(status="building", progress=0.05, message="读取选择与时长")
            self.save(record)
            job_request = EditJobRequest(**request.model_dump(), beat_sync=False)
            if request.purpose != "library":
                self.jobs._resolve_framing(job_request)
            choices = {s.capture_id: s.model_dump() for s in request.capture_selections}
            tree, clips, files, groups = [], [], {}, {}
            record["files"] = files  # Lease originals as soon as asynchronous preparation begins.
            source_paths = []
            cursor = 0.0
            ids = [request.intro_effect_media_id] if request.intro_effect_media_id else []
            ids += request.media_ids
            ids += [request.outro_effect_media_id] if request.outro_effect_media_id else []
            for index, media_id in enumerate(ids):
                item = self.media.get(media_id)
                if item is None or item.kind != "video":
                    raise ValueError("剪辑素材必须是媒体库中的有效视频")
                is_effect = (index == 0 and request.intro_effect_media_id is not None) or (
                    index == len(ids) - 1 and request.outro_effect_media_id is not None
                )
                if not is_effect and item.metadata.get("role") in {
                    "preview",
                    "cache",
                    "seedance_effect",
                }:
                    raise ValueError("请选择原始录制或无字幕母版；预览和特效不能作为录制来源")
                if is_effect and item.metadata.get("role") != "seedance_effect":
                    raise ValueError("片头和片尾必须选择特效视频")
                if item.metadata.get("has_burned_subtitles"):
                    raise ValueError("请选择无字幕母版，避免字幕错位")
                if Path(item.path).is_file():
                    files[item.path] = file_identity(item.path)
                group = item.metadata.get("capture_group") if not is_effect else None
                children = []
                clip_ranges = None
                notes = (read_sidecar(item.path) or {}).get("notes", [])
                original = item
                origins = (
                    [f"capture:{group['id']}"] if group else [f"file:{Path(item.path).resolve()}"]
                )
                if not is_effect:
                    source_paths.append(item.path)
                if item.metadata.get("composition_id"):
                    files[str(manifest_path(item.path))] = file_identity(
                        str(manifest_path(item.path))
                    )
                    notes = item.metadata.get("notes", [])
                    origins = item.metadata.get("source_recording_ids") or original_recording_ids(
                        item.metadata.get("composition_tree", [])
                    )
                if group:
                    stored = await self.media.captures.ensure_timeline(
                        group["id"], lambda detail: self._progress(record, detail)
                    )
                    if expected_groups is not None and expected_groups.get(group["id"]) != [
                        stored["fingerprint"], stored["timeline_version"]
                    ]:
                        raise ValueError("拍摄时间轴已改变，请重新规划自动组合")
                    choice = choices.pop(
                        group["id"], {"capture_id": group["id"], "include_full": True}
                    )
                    ranges = selected_ranges(stored, choice)
                    groups[group["id"]] = [stored["fingerprint"], stored["timeline_version"]]
                    duration = sum(end - start for start, end in ranges)
                    children = compact_tree(
                        rebase_timeline(stored["segments"], ranges), original.id, cursor
                    )
                    if Path(original.path).is_file():
                        # Multiple render intervals still belong to ONE recording tree/input.
                        # Trim directly in the final filtergraph instead of encoding a temporary
                        # selection video and then decoding/encoding it again for the preview.
                        clip_ranges = ranges
                    else:
                        resolved = await self.media.captures.resolve(
                            original, choice, lambda detail: self._progress(record, detail)
                        )
                        item = resolved
                        files[item.path] = file_identity(item.path)
                else:
                    duration = await asyncio.to_thread(self.renderer.probe_duration, item.path)
                    ranges = [(0, duration)]
                    children = compact_tree(
                        item.metadata.get("composition_tree", []), original.id, cursor
                    )
                if not duration or duration < 1 / 30:
                    raise ValueError("所选画面不足一帧或无法读取时长")
                tree.append(
                    {
                        "id": f"{index}:{original.id}",
                        "label": group["title"] if group else Path(item.path).stem,
                        "kind": "effect" if is_effect else "recording",
                        "start": cursor,
                        "end": cursor + duration,
                        "duration": duration,
                        "children": children,
                        "notes": notes,
                        "media_id": original.id,
                        "source_recording_ids": origins,
                        "source_ranges": ranges,
                    }
                )
                part_cursor = cursor
                for range_start, range_end in clip_ranges or [(0, duration)]:
                    part_duration = range_end - range_start
                    clips.append(
                        TimelineClip(
                            media_id=item.id,
                            source_path=item.path,
                            start=range_start,
                            duration=part_duration,
                            timeline_start=part_cursor,
                            label=tree[-1]["label"],
                            include_audio=is_effect and not request.effect_cover_audio,
                        )
                    )
                    part_cursor += part_duration
                cursor += duration
                record.update(progress=0.1 + 0.3 * (index + 1) / len(ids), message="组合选中的画面")
                self.save(record)
            if choices:
                raise ValueError("分段选择与当前录制列表不一致，请重新选择")
            # Original-frame output follows the recording, never an intro effect canvas.
            first_recording = clips[1 if request.intro_effect_media_id else 0]
            size = await asyncio.to_thread(
                self.jobs._source_size_from_path, first_recording.source_path
            )
            width, height = (
                ((720, 1280) if job_request.output_aspect_ratio == "9:16" else (1280, 720))
                if job_request.output_aspect_ratio
                else self.jobs.planner.source_frame(size)
            )
            visual_signature = digest(
                [
                    tree,
                    files,
                    groups,
                    width,
                    height,
                    job_request.output_crop_x,
                    job_request.output_crop_y,
                ]
            )
            timeline = EditTimeline(
                title=request.title,
                clips=clips,
                output_path=str(preview),
                target_duration_seconds=cursor,
                beat_sync=False,
                mute_original_audio=request.mute_original_audio,
                output_width=width,
                output_height=height,
                output_fit="cover" if job_request.output_aspect_ratio else "contain",
                output_crop_x=job_request.output_crop_x
                if job_request.output_crop_x is not None
                else 0.5,
                output_crop_y=job_request.output_crop_y
                if job_request.output_crop_y is not None
                else 0.5,
                planning_diagnostics={
                    "mode": "controlled_concat",
                    "composition_id": record["id"],
                    "effects_applied": True,
                    "visual_signature": visual_signature,
                },
            )
            def music_boundaries(nodes):
                return [value for node in nodes for value in [node["start"], *music_boundaries(node.get("children", []))]]

            timeline.planning_diagnostics["music_cut_times"] = music_boundaries(tree)
            if request.music_media_id:
                music = self.media.get(request.music_media_id)
                if not music or music.kind != "audio":
                    raise ValueError("请选择有效音乐")
                timeline.music_path = music.path
                content = [node for node in tree if node["kind"] != "effect"]
                timeline.music_delay_seconds = (
                    0 if request.effect_cover_audio else content[0]["start"]
                )
                timeline.music_duration_seconds = (
                    cursor
                    if request.effect_cover_audio
                    else sum(node["duration"] for node in content)
                )
                files[music.path] = file_identity(music.path)
            if request.voiceover_media_id:
                voice = self.media.get(request.voiceover_media_id)
                if not voice or voice.kind != "audio":
                    raise ValueError("请选择有效旁白")
                metadata, _ = read_json(
                    Path(
                        voice.metadata.get("metadata_path") or Path(voice.path).with_suffix(".json")
                    )
                )
                content = [node for node in tree if node["kind"] != "effect"]
                source = (
                    self.media.get(request.media_ids[0]) if len(request.media_ids) == 1 else None
                )
                if metadata.get("whole_audio"):
                    if (
                        not source
                        or source.metadata.get("composition_id") != metadata.get("composition_id")
                        or source.metadata.get("narration_binding_id") != metadata.get("binding_id")
                    ):
                        raise ValueError("旁白不属于此组合的当前版本，请重新选择")
                elif metadata.get("visual_signature"):
                    if metadata["visual_signature"] != visual_signature:
                        raise ValueError("旁白尚未绑定这个组合，请重新制作")
                elif source and source.metadata.get("composition_id"):
                    raise ValueError("组合视频请使用与它绑定的旁白")
                voice_duration = await asyncio.to_thread(self.renderer.probe_duration, voice.path)
                available = sum(node["duration"] for node in content)
                if voice_duration is None or voice_duration > available + 0.02:
                    raise ValueError("旁白长于所选画面，请先调整文案；不会截断语音或拉长视频")
                timeline.voiceover_path = voice.path
                timeline.voiceover_start_seconds = content[0]["start"]
                timeline.subtitles = self.jobs.planner._subtitles(
                    job_request, voice, width, height, timeline.warnings, voice_duration
                )
                files[voice.path] = file_identity(voice.path)
                files[str(Path(voice.path).with_suffix(".json"))] = file_identity(
                    str(Path(voice.path).with_suffix(".json"))
                )
            await asyncio.to_thread(self.jobs._resolve_clip_audio, timeline)
            await asyncio.to_thread(
                self.jobs._resolve_original_audio,
                request.mute_original_audio,
                [c.source_path for c in clips],
                timeline,
            )
            if timeline.include_original_audio:
                for clip in timeline.clips:
                    clip.include_audio = False  # original audio already includes the effects
            record.update(
                tree=tree,
                source_paths=source_paths,
                duration=cursor,
                visual_signature=visual_signature,
                files=files,
                groups=groups,
                timeline=timeline.model_dump(mode="json"),
                signature=digest(
                    [request.model_dump(), files, groups, timeline.model_dump(mode="json")]
                ),
                progress=0.5,
                stage="waiting",
                message="等待可用编码资源",
            )
            self.save(record)
            async with self.jobs._render_slots:
                self.validate_files(record)
                await self.renderer.render(
                    timeline, progress=lambda detail: self._progress(record, detail)
                )
            self._progress(record, {"stage": "verifying"})
            self.validate_files(record)
            actual = await asyncio.to_thread(self.renderer.probe_duration, str(preview))
            if actual is None or abs(actual - cursor) > max(0.08, len(clips) / 30):
                raise ValueError("预览实际时长与组合不一致，请检查源视频时间戳")
            self.media.register_generated_path(
                preview, kind="video", metadata={"role": "preview", "composition_id": record["id"]}
            )
            record.update(
                status="ready",
                stage="ready",
                finished_at=time.time(),
                progress=1,
                message="请查看预览并确认最终组合",
                preview_path=str(preview),
                measured_duration=actual,
                timeline=timeline.model_dump(mode="json"),
                warnings=timeline.warnings,
            )
            self.save(record)
        except BaseException as exc:
            record.update(
                status="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                stage="cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                error="" if isinstance(exc, asyncio.CancelledError) else str(exc),
                message="预览已取消"
                if isinstance(exc, asyncio.CancelledError)
                else "剪辑预览未完成",
                finished_at=time.time(),
            )
            self.save(record)
            preview.unlink(missing_ok=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            self.tasks.pop(record["id"], None)

    def validate_files(self, record):
        for path, identity in record.get("files", {}).items():
            if file_identity(path) != identity:
                raise ValueError("预览中的素材已改变，请重新生成预览")
        for key, identity in record.get("groups", {}).items():
            group = self.media.captures.groups.get(key)
            if not group or [group["fingerprint"], group["timeline_version"]] != identity:
                raise ValueError("拍摄时间轴已改变，请重新生成预览")

    async def confirm(self, key: str, signature: str):
        async with self._confirm_lock:
            record = self.get(key)
            if record["request"].get("purpose") == "library":
                raise ValueError("媒体库组合请先保存为素材，再到工作台制作成片")
            if record.get("signature") != signature or record["status"] != "ready":
                raise ValueError("只有已完成且未改变的预览才能确认")
            self.validate_files(record)
            if record.get("job_id"):
                job = self.jobs.get(record["job_id"])
                if job:
                    return job
                raise ValueError("这个组合已经创建过成片，请在媒体库查看")
            request = EditJobRequest(**record["request"], beat_sync=False)
            request.output_name = self.jobs._next_output_name(request.title)
            timeline = EditTimeline.model_validate(record["timeline"])
            timeline.output_path = str(GENERATED_DIRS["exports"] / request.output_name)
            request.target_duration_seconds = timeline.target_duration_seconds
            job = await self.jobs._announce(request, timeline)
            record["job_id"] = job.id
            self.save(record)
            return job

    def material(self, key):
        record = self.get(key)
        item = next(
            (
                item
                for item in self.media.list_items()
                if item.kind == "video"
                and item.metadata.get("role") == "raw_video"
                and read_manifest(item.path).get("composition_id") == key
            ),
            None,
        )
        if not item:
            raise ValueError("请先在媒体库确认并保存组合视频")
        record["material_path"] = item.path
        record["preview_path"] = item.path
        return record, item

    async def save_material(self, key, signature):
        async with self._confirm_lock:
            record = self.get(key)
            if (
                record.get("signature") != signature
                or record["status"] != "ready"
                or record["request"].get("purpose") != "library"
            ):
                raise ValueError("请先生成媒体库组合预览，再确认保存")
            if record.get("material_path"):
                _, item = self.material(key)
                return item
            self.validate_files(record)
            self.media.ensure_media_pool_initialized()
            directory = GENERATED_DIRS["data"] / "downloads"
            directory.mkdir(parents=True, exist_ok=True)
            # The title is a label the operator and the app write for people, so it may
            # hold characters a filename cannot (a recording stamped 17:20 is the common
            # case). Keep the label readable and only clean the derived filename, instead
            # of refusing to save the composition at all.
            stem = safe_stem(record["title"] or "组合", "组合")
            target = directory / f"{stem} 组合-{key[:8]}.mp4"
            stage = target.with_suffix(".part")
            metadata = {
                "composition_id": key,
                "title": record["title"],
                "composition_tree": record["tree"],
                "source_recording_ids": original_recording_ids(record["tree"]),
                "visual_signature": record["visual_signature"],
                "duration_seconds": record["duration"],
                "notes": [note for root in record["tree"] for note in root.get("notes", [])],
                "narration_binding_id": None,
                **({"automatic": deepcopy(record["automatic"])} if record.get("automatic") else {}),
            }
            try:
                await asyncio.to_thread(shutil.copyfile, record["preview_path"], stage)
                self.validate_files(record)
                save_manifest(target, metadata)
                stage.replace(target)
                item = self.media.register_generated_path(
                    target,
                    kind="video",
                    metadata={"source": "data/downloads", "role": "raw_video", **metadata},
                )
                record["material_path"] = str(target)
                record["preview_path"] = str(target)
                self.save(record)
                return item
            finally:
                stage.unlink(missing_ok=True)
                if not target.is_file():
                    manifest_path(target).unlink(missing_ok=True)

    async def studio_previews(self, request):
        items = {item.id: item for item in self.media.list_items()}
        sources = [items.get(key) for key in dict.fromkeys(request.media_ids)]
        if any(
            not item or not self.media._matches_pool(item, "source_media_ids") for item in sources
        ):
            raise ValueError("请先在媒体库保存组合；工作台只接收完整视频输入")
        ordinary = [items.get(key) for key in request.voiceover_media_ids]
        if any(
            not item or item.kind != "audio" or item.metadata.get("role") != "tts_voice"
            for item in ordinary
        ):
            raise ValueError("旁白选择已失效")
        ordinary = [item for item in ordinary if not item.metadata.get("binding_id")]
        if len(ordinary) > 1:
            raise ValueError("普通视频每次请选择一条旁白")
        plans = []
        for source in sources:
            if source.metadata.get("bound_voice_missing"):
                raise ValueError(f"{Path(source.path).name} 的绑定旁白文件缺失，请重新制作")
            voice_id = source.metadata.get("bound_voice_id")
            if not source.metadata.get("composition_id") and ordinary:
                voice_id = ordinary[0].id
            values = request.model_dump(exclude={"media_ids", "voiceover_media_ids"})
            source_title = source.metadata.get("title") or Path(source.path).stem
            values["title"] = f"{request.title} · {source_title}" if request.title else source_title
            plans.append(
                CompositionRequest(**values, media_ids=[source.id], voiceover_media_id=voice_id)
            )
        return [await self.create(plan) for plan in plans]

    def discard(self, key):
        record = self.get(key)
        if key in self.tasks or record.get("narration", {}).get("status") == "running":
            raise ValueError("组合仍在生成，请完成后再移除")
        job = self.jobs.get(record.get("job_id")) if record.get("job_id") else None
        if job and job.status.value in {"queued", "running"}:
            raise ValueError("成片正在导出，请完成后再移除")
        # Removing the review record releases source leases. Media/export deletion stays
        # in the existing library cleanup workflow and never touches original recordings.
        if record.get("material_path"):
            raise ValueError("已保存组合保留来源记录；可在媒体库管理视频")
        (self.directory / f"{key}.json").unlink(missing_ok=True)
        del self.records[key]

    def is_path_in_use(self, path):
        target = str(Path(path).resolve())
        for record in self.records.values():
            narration = record.get("narration") or {}
            if (
                narration.get("status") == "ready"
                and narration.get("source_audio_path")
                and str(Path(narration["source_audio_path"]).resolve()) == target
            ):
                return (
                    True  # Keep the raw take for later speed adjustments without another TTS call.
                )
            if narration.get("status") in {"running", "pending_review", "needs_revision", "failed"}:
                held = [
                    record.get("material_path"),
                    *(
                        narration.get(key)
                        for key in (
                            "source_audio_path",
                            "audio_path",
                            "candidate_path",
                            "preview_path",
                            "building_preview_path",
                        )
                    ),
                ]
                if any(value and str(Path(value).resolve()) == target for value in held):
                    return True
        return any(
            (
                record.get("narration", {}).get("status") == "running"
                and record.get("material_path") == target
            )
            or (
                not record.get("material_path")
                and (target in record.get("files", {}) or record.get("preview_path") == target)
            )
            for record in self.records.values()
            if record["status"] in {"queued", "building", "ready"}
            and not record.get("job_id")  # Jobs own their leases after confirmation.
        )

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
