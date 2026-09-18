from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import datetime
from pathlib import Path

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    EditJobRequest,
    EditTimeline,
    JobRecord,
    JobStatus,
    SubtitleTrack,
    TimelineClip,
    utc_now,
)
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.naming import safe_stem, validate_filename
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.timeline import EditPlanner

# Seeds stay inside a signed 32-bit range so they survive JSON, logs, and a copy-paste back
# into the seed box without any of them mangling a long integer.
MAX_SEED = 2**31


class JobService:
    def __init__(
        self,
        events: EventHub,
        media: MediaService,
        analysis,
        planner: EditPlanner,
        renderer: RenderService,
        settings: SettingsService | None = None,
        semantic=None,
        render_slots: asyncio.Semaphore | None = None,
    ) -> None:
        self.events = events
        self.media = media
        self.planner = planner
        self.renderer = renderer
        # Optional in tests; in the app this provides the saved output framing.
        self.settings = settings
        self.compositions = None
        self._jobs: dict[str, JobRecord] = {}
        self._allocated_output_names: set[str] = set()
        self._render_slots = render_slots or asyncio.Semaphore(1)
        # Reuse successful audio probes only while path, size and modification time match.
        self._audio_duration_cache: dict[tuple[str, int, int], float] = {}

    def list_jobs(self) -> list[JobRecord]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    async def editing_capabilities(self, *args, **kwargs) -> dict:
        return {"mode": "controlled_concat", "beat_sync": False, "automatic_selection": False}

    async def create(self, request):
        raise ValueError("请先生成拼接预览，再确认最终组合")

    async def create_batch(self, request):
        raise ValueError("自动批量选片已停用，请先生成拼接预览")

    async def draft_timeline(self, request):
        raise ValueError("请使用拼接预览入口，成片长度由所选画面决定")

    async def _announce(self, request: EditJobRequest, timeline=None) -> JobRecord:
        """Publish and queue a confirmed composition or a manually refined timeline."""
        job = JobRecord(request=request, timeline=timeline)
        self._jobs[job.id] = job
        await self.events.publish("JOB_CREATED", job.model_dump(mode="json"))
        asyncio.create_task(self._run(job.id))
        return job

    async def create_from_timeline(self, timeline) -> JobRecord:
        # Client-supplied manual timelines cannot claim a server-owned confirmed snapshot.
        timeline.planning_diagnostics.pop("composition_id", None)
        if not str(timeline.output_path or "").strip():
            timeline.output_path = str(
                GENERATED_DIRS["exports"] / self._next_output_name(timeline.title)
            )
        else:
            timeline.output_path = str(self._managed_export_path(timeline.output_path))
        self._bind_manual_subtitle_sources(timeline)
        if timeline.output_fit == "contain" and timeline.clips:
            size = self._source_size_from_path(timeline.clips[0].source_path)
            if size:
                timeline.output_width, timeline.output_height = self.planner.source_frame(size)
        self._resolve_clip_audio(timeline)
        self._resolve_original_audio(
            timeline.mute_original_audio,
            [clip.source_path for clip in timeline.clips],
            timeline,
        )
        track = getattr(timeline, "subtitles", None)
        request = EditJobRequest(
            title=timeline.title,
            media_ids=[clip.media_id for clip in timeline.clips],
            output_name=Path(timeline.output_path).name,
            target_duration_seconds=timeline.target_duration_seconds,
            mute_original_audio=timeline.mute_original_audio,
            beat_sync=timeline.beat_sync,
            output_aspect_ratio=(
                None
                if timeline.output_fit == "contain"
                else ("9:16" if timeline.output_height > timeline.output_width else "16:9")
            ),
            output_crop_x=timeline.output_crop_x,
            output_crop_y=timeline.output_crop_y,
            editorial_preset=timeline.editorial_preset,
            # Read back off the timeline the client sent rather than defaulted to False. The
            # timeline is what gets rendered here, so a record saying otherwise would describe
            # an export that never happened.
            subtitles=bool(track and track.cues),
            subtitle_font=track.font if track else "noto_sans_sc",
        )
        job = JobRecord(request=request, timeline=timeline)
        self._jobs[job.id] = job
        await self.events.publish("JOB_CREATED", job.model_dump(mode="json"))
        asyncio.create_task(self._run(job.id))
        return job

    def _bind_manual_subtitle_sources(self, timeline: EditTimeline) -> None:
        """Bind manual renders to the soundtrack's authoritative persisted subtitle layer.

        The UI performs the same checks for immediate feedback, but this endpoint also serves
        older clients and is the final authority. A client copy can be missing, stale or taken
        from another export, so a valid sidecar replaces it rather than merely proving it exists.
        Selecting an export for inspection remains allowed; only submitting an unsafe re-cut is
        rejected.
        """
        items = self.media.list_items()
        by_path: dict[str, list] = {}
        for item in items:
            by_path.setdefault(self._path_key(item.path), []).append(item)

        authorized_clip_items: dict[str, list] = {}
        for clip in timeline.clips:
            source_key = self._path_key(clip.source_path)
            # Check burned provenance before a stale-id diagnostic: the physical file is what
            # FFmpeg would open, so a forged id must never turn the safety message into an
            # opportunity to retry the same unsafe pixels through another client.
            if any(
                bool(item.metadata.get("has_burned_subtitles"))
                for item in by_path.get(source_key, [])
            ):
                raise ValueError(
                    "已选成片的字幕已烧录在画面中，不能用于手动微调；请改用同组的母版（无字幕）"
                )
            matching = self._manual_clip_items(clip, by_path)
            authorized_clip_items[source_key] = matching
            if any(bool(item.metadata.get("has_burned_subtitles")) for item in matching):
                raise ValueError(
                    "已选成片的字幕已烧录在画面中，不能用于手动微调；请改用同组的母版（无字幕）"
                )
        bed = getattr(timeline, "audio_bed", None)
        if bed is None:
            if timeline.subtitles and timeline.subtitles.cues:
                raise ValueError("手动微调字幕没有可验证的原声绑定，请重新选择保留原声")
            timeline.subtitles = None
            return
        bed_key = self._path_key(bed.source_path)
        bed_items = by_path.get(bed_key, []) or authorized_clip_items.get(bed_key, [])
        if not bed_items:
            raise ValueError("保留的原声不在媒体库中，请重新选择后再渲染")
        if any(bool(item.metadata.get("has_burned_subtitles")) for item in bed_items):
            raise ValueError(
                "保留的原声来自已烧录字幕的成片，不能用于手动微调；请改用同组的母版（无字幕）"
            )

        bed_item = bed_items[0]

        recorded_sidecar = bed_item.metadata.get("subtitles_path")
        canonical_sidecar = self.renderer.subtitle_sidecar_path(bed_item.path)
        if not recorded_sidecar and not canonical_sidecar.exists():
            if timeline.subtitles and timeline.subtitles.cues:
                raise ValueError("保留的原声没有可验证的字幕数据，请重新选择素材")
            timeline.subtitles = None
            return

        authoritative = self.renderer.read_subtitle_sidecar(
            recorded_sidecar or bed_item.path,
            expected_video_path=bed_item.path,
        )
        if authoritative is None:
            raise ValueError(
                "保留的原声记录过字幕，但字幕数据缺失、损坏或不属于该母版；请重新选择同组的有效母版"
            )
        # read_subtitle_sidecar has already applied strict persistence validation. Reconstructing
        # the model here intentionally discards envelope fields (video/version/has_voiceover) and
        # gives rendering exactly the layer owned by this audio bed, never a client-supplied copy.
        timeline.subtitles = SubtitleTrack.model_validate(authoritative, strict=True)

    def _manual_clip_items(self, clip, by_path: dict[str, list]) -> list:
        """Validate the media id/path pair a manual timeline asks FFmpeg to read."""
        source_key = self._path_key(clip.source_path)
        matching = list(by_path.get(source_key, []))
        claimed = self.media.get(clip.media_id)
        capture_input = self.media.capture_input_item(clip.media_id, clip.source_path)
        if claimed is not None and claimed.metadata.get("capture_input"):
            if capture_input is None:
                if self._path_key(claimed.path) != source_key:
                    raise ValueError("手动微调素材的 media_id 与文件路径不一致，请刷新媒体库后重试")
                raise ValueError("手动微调素材不在媒体库中，请重新选择后再渲染")
            matching.append(capture_input)
        elif claimed is not None and self._path_key(claimed.path) != source_key:
            child = self.media.capture_child_item(clip.media_id, clip.source_path)
            if child is None:
                raise ValueError("手动微调素材的 media_id 与文件路径不一致，请刷新媒体库后重试")
            matching.append(child)
        if not matching:
            # Stale ids are harmless when the canonical path is still present in the library
            # after a restart. An unknown path could be an unregistered partial export or a
            # client-selected file outside every persistence boundary.
            raise ValueError("手动微调素材不在媒体库中，请重新选择后再渲染")
        return matching

    def _resolve_framing(self, request) -> None:
        """Freeze the current global canvas and crop onto a request before it is queued."""
        if request.output_aspect_ratio is not None:
            request.output_crop_x = (
                request.output_crop_x if request.output_crop_x is not None else 0.5
            )
            request.output_crop_y = (
                request.output_crop_y if request.output_crop_y is not None else 0.5
            )
            return

        framing = self.settings.output_quota() if self.settings is not None else None
        if framing is None or not framing.framing_configured:
            # Unset is an explicit original-frame choice, resolved from the source later.
            request.output_aspect_ratio = None
            request.output_crop_x = None
            request.output_crop_y = None
            return

        request.output_aspect_ratio = framing.output_aspect_ratio
        request.output_crop_x = framing.framing_crop_x
        request.output_crop_y = framing.framing_crop_y

    def _validate_job_request(self, request: EditJobRequest) -> None:
        if not request.media_ids:
            raise ValueError("No source videos selected")
        for media_id in request.media_ids:
            item = self.media.get(media_id)
            if not item or not MediaService.is_automatic_source(item):
                raise ValueError("Source videos must be imported video media")
        if request.music_media_id:
            item = self.media.get(request.music_media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "music":
                raise ValueError("Music must be music audio media")
        if request.voiceover_media_id:
            item = self.media.get(request.voiceover_media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "tts_voice":
                raise ValueError("Voiceover must be generated TTS media")
        for media_id in (request.intro_effect_media_id, request.outro_effect_media_id):
            if not media_id:
                continue
            item = self.media.get(media_id)
            if not item or item.kind != "video" or item.metadata.get("role") != "seedance_effect":
                raise ValueError("Effect pool must contain generated video effect media")

    async def _run(self, job_id: str) -> None:
        async with self._render_slots:
            await self._run_job(job_id)

    async def _run_job(self, job_id: str) -> None:
        job = self._jobs[job_id]
        try:
            job.status = JobStatus.RUNNING
            job.progress = 0.1
            job.message = "准备已确认的拼接"
            job.updated_at = utc_now()
            await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

            if job.timeline is None:
                raise ValueError("请先预览并确认拼接结果")
            timeline = job.timeline
            composition_id = timeline.planning_diagnostics.get("composition_id")
            if composition_id:
                self.compositions.validate_files(self.compositions.get(composition_id))
            # Controlled compositions already include effects; manual timelines can attach them here.
            self._decorate_timeline(timeline, job.request)
            # Defense in depth for restored/internal jobs: rendering must never be able to place
            # an export in data/downloads, where a restart would rediscover it as raw footage.
            timeline.output_path = str(self._managed_export_path(timeline.output_path))
            job.warnings = list(getattr(timeline, "warnings", []) or [])
            job.progress = 0.7
            job.message = "Rendering export"
            await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

            # One group covers the delivered file and its subtitle-free master, so the library
            # shows a single entry per finished video rather than doubling in length the day
            # subtitles were switched on. Recorded here rather than inferred from the filenames
            # because an operator may rename either file.
            group = f"export:{job.id}"
            # A carried track is not necessarily visible in this cut: manual fine-tuning may
            # start the retained soundtrack after every old cue. Use the renderer's one
            # output-clock projection for rendering, metadata and master creation alike.
            subtitled = bool(self.renderer.output_subtitle_cues(timeline))
            inherited_burned_subtitles = (
                False if composition_id else self._inherits_burned_subtitles(timeline)
            )
            has_voiceover = bool(
                getattr(timeline, "voiceover_path", None)
                or (getattr(timeline, "audio_bed", None) and timeline.audio_bed.has_voiceover)
            )
            planned_delivery = Path(timeline.output_path)
            planned_master = self.renderer.master_output_path(timeline) if subtitled else None
            publish_family = self._export_publish_family(
                planned_delivery,
                planned_master,
            )
            existing = [path.name for path in publish_family if path.exists()]
            if existing:
                # Prove ownership before creating either half. Cleanup after a later failure can
                # then remove the entire family without ever touching an older user file.
                raise RuntimeError(f"成片组目标文件已存在：{', '.join(existing)}")

            try:
                result_path = await self.renderer.render(timeline)
                # Each half receives its own canonical cue sidecar. 手动微调 can therefore load
                # the exact words and style from whichever variant the operator picked, while
                # renaming or deleting one variant never makes the other depend on its filename.
                sidecar = str(self.renderer.subtitle_sidecar_path(result_path)) if subtitled else ""
                delivery_metadata = {
                    "source": "exports",
                    "role": "export",
                    "job_id": job.id,
                    "export_group": group,
                    "variant": "subtitled" if subtitled else "single",
                    "variant_label": "成片（带字幕）" if subtitled else "成片",
                    "subtitles_path": sidecar,
                    "has_voiceover": has_voiceover,
                    "output_width": timeline.output_width,
                    "output_height": timeline.output_height,
                    "output_aspect_ratio": job.request.output_aspect_ratio,
                    "output_crop_x": timeline.output_crop_x,
                    "output_crop_y": timeline.output_crop_y,
                    "editorial_preset": timeline.editorial_preset,
                    "planning_diagnostics": timeline.planning_diagnostics,
                    # Re-cutting a file that already has text painted into it drags the old
                    # subtitles along at the wrong times, so the UI has to be able to say so.
                    "has_burned_subtitles": (subtitled or inherited_burned_subtitles),
                }
                master_path = await self.renderer.render_master(timeline)
                registrations = [(Path(result_path), "video", delivery_metadata)]
                if subtitled:
                    if not master_path:
                        raise RuntimeError("带字幕成片没有生成对应母版")
                    master_metadata = {
                        "source": "exports",
                        "role": "export",
                        "job_id": job.id,
                        "export_group": group,
                        "variant": "master",
                        "variant_label": "母版（无字幕）",
                        "subtitles_path": str(self.renderer.subtitle_sidecar_path(master_path)),
                        "has_voiceover": has_voiceover,
                        "output_width": timeline.output_width,
                        "output_height": timeline.output_height,
                        "output_aspect_ratio": job.request.output_aspect_ratio,
                        "output_crop_x": timeline.output_crop_x,
                        "output_crop_y": timeline.output_crop_y,
                        "editorial_preset": timeline.editorial_preset,
                        "planning_diagnostics": timeline.planning_diagnostics,
                        # The master removes only the subtitle layer added by this render.
                        # Text already baked into a source export remains pixels and must
                        # keep warning the next manual fine-tune generation.
                        "has_burned_subtitles": inherited_burned_subtitles,
                    }
                    registrations.append((Path(master_path), "video", master_metadata))
                # Delivery + master become visible together, after both files and both subtitle
                # sidecars exist. One manifest replacement and one in-memory commit publish them.
                if composition_id:
                    self.compositions.validate_files(self.compositions.get(composition_id))
                self.media.register_generated_paths(registrations)
            except BaseException as publish_exc:  # noqa: BLE001 - cancellation also needs cleanup
                self._discard_unregistered_export_group(publish_family, publish_exc)
            job.status = JobStatus.SUCCEEDED
            job.progress = 1
            job.message = "Export complete"
            job.result_path = result_path
        except Exception as exc:  # noqa: BLE001 - job failures are recorded and surfaced
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.message = "Export failed"
        finally:
            job.updated_at = utc_now()
            await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

    @staticmethod
    def _export_publish_family(
        delivery: Path,
        master: Path | None,
    ) -> tuple[Path, ...]:
        paths = [
            delivery,
            delivery.with_suffix(".ass"),
            delivery.with_suffix(".subtitles.json"),
        ]
        if master is not None:
            paths.extend(
                [
                    master,
                    master.with_suffix(".ass"),
                    master.with_suffix(".subtitles.json"),
                ]
            )
        return tuple(dict.fromkeys(paths))

    @staticmethod
    def _discard_unregistered_export_group(
        paths: tuple[Path, ...],
        registration_exc: BaseException,
    ) -> None:
        """Remove a newly-created group that could not acquire its durable identity.

        The caller first proved that every member was absent, and this remains deliberately
        limited to the managed exports directory. It never repairs or guesses about older files;
        it only prevents the current failed job from publishing a partial group.
        """
        export_root = GENERATED_DIRS["exports"].resolve()
        resolved = [path.resolve() for path in paths]
        if any(target != export_root and export_root not in target.parents for target in resolved):
            raise registration_exc
        failures = []
        for member in resolved:
            try:
                member.unlink(missing_ok=True)
            except OSError as exc:
                failures.append(f"{member.name}: {exc}")
        if failures:
            raise RuntimeError(
                f"{registration_exc}；未登记成片清理失败：{' | '.join(failures)}"
            ) from registration_exc
        raise registration_exc

    def _inherits_burned_subtitles(self, timeline: EditTimeline) -> bool:
        """Whether any source clip already carries subtitle pixels that encoding cannot remove."""
        items = self.media.list_items()
        by_path: dict[str, list] = {}
        for item in items:
            by_path.setdefault(self._path_key(item.path), []).append(item)
        for clip in getattr(timeline, "clips", []) or []:
            if any(
                bool(item.metadata.get("has_burned_subtitles"))
                for item in self._manual_clip_items(clip, by_path)
            ):
                return True
        return False

    def _audio_duration(self, item) -> float | None:
        """Length of a voiceover, measuring estimates against the actual audio when possible."""
        if item is None:
            return None

        metadata_path = item.metadata.get("metadata_path")
        if not metadata_path:
            metadata_path = str(Path(item.path).with_suffix(".json"))
        try:
            sidecar = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            sidecar = {}
        if not isinstance(sidecar, dict):
            sidecar = {}

        # The output file is the clock the renderer will really play. Measuring it also catches
        # an otherwise-valid provider response that stopped reporting words before the audio did.
        # Direct JSON reading above is intentionally non-mutating: planning must never quarantine
        # or rewrite a malformed narration sidecar merely because the operator selected it.
        if self.renderer is not None:
            cache_key = None
            try:
                audio_path = Path(item.path).resolve()
                stat = audio_path.stat()
                cache_key = (str(audio_path), stat.st_size, stat.st_mtime_ns)
            except OSError:
                pass
            measured = self._audio_duration_cache.get(cache_key) if cache_key else None
            if measured is None:
                measured = self.renderer.probe_duration(item.path)
                if (
                    measured is not None
                    and math.isfinite(measured)
                    and measured > 0
                    and cache_key is not None
                ):
                    self._audio_duration_cache[cache_key] = measured
            if measured is not None and math.isfinite(measured) and measured > 0:
                return measured

        for duration_ms in (item.metadata.get("duration_ms"), sidecar.get("duration_ms")):
            if isinstance(duration_ms, bool):
                continue
            try:
                duration = float(duration_ms)
            except (TypeError, ValueError):
                continue
            if math.isfinite(duration) and duration > 0:
                return duration / 1000.0
        return None

    def _source_size(self, items) -> tuple[int, int] | None:
        return self._source_size_from_path(items[0].path) if items else None

    def _source_size_from_path(self, path: str) -> tuple[int, int] | None:
        if self.renderer is None:
            return None
        return self.renderer.probe_frame_size(path)

    def _resolve_original_audio(
        self, mute_original_audio: bool, paths: list[str], timeline
    ) -> None:
        """Honour 静音原视频噪声 only when every source can actually supply audio.

        Recomputed here rather than trusted from the timeline, because /timeline/render accepts
        a client-built one and a source without an audio stream fails the whole render.
        """
        timeline.include_original_audio = False
        if mute_original_audio:
            return
        if any(getattr(clip, "kind", "video") == "image" for clip in timeline.clips):
            # A still has no audio stream, so asking ffmpeg for one fails the whole render.
            timeline.warnings.append("时间线里有图片，已改为静音原视频")
            return
        answers = {path: self.renderer.has_audio_stream(path) for path in dict.fromkeys(paths)}
        unknown = [path for path, answer in answers.items() if answer is None]
        silent = [path for path, answer in answers.items() if answer is False]
        # Said apart, because they call for different words. Telling an operator their footage
        # has no audio when the truth is that we could not look is a claim about their footage
        # made from a fact about the tool.
        if unknown:
            timeline.warnings.append(
                f"{len(unknown)} 个源视频无法确认音轨（ffprobe 未能读取），已改为静音原视频"
            )
            return
        if silent:
            timeline.warnings.append(f"{len(silent)} 个源视频没有声音轨，已改为静音原视频")
            return
        timeline.include_original_audio = True

    def _resolve_clip_audio(self, timeline) -> None:
        """Keep only explicitly selected clip-audio streams that FFmpeg can actually read."""
        selected = [clip for clip in timeline.clips if getattr(clip, "include_audio", False)]
        if not selected:
            return
        answers = {
            path: self.renderer.has_audio_stream(path)
            for path in dict.fromkeys(clip.source_path for clip in selected)
        }
        for clip in selected:
            if clip.kind != "video" or answers.get(clip.source_path) is not True:
                clip.include_audio = False
                timeline.warnings.append(
                    f"特效「{Path(clip.source_path).name}」没有可用声音，已按静音特效处理"
                )

    def _decorate_timeline(self, timeline, request) -> None:
        """Attach the resolved 片头/片尾 effect bumpers to a finished timeline.

        Effects sit on top of the planned picture: an intro is prepended and (cover off) the
        bed pushed past it; an outro is appended. With cover off the effect keeps its own audio
        and the narration/music sit around it; with cover on it is an ordinary clip the bed
        plays over. Runs once, after either batch path, so nothing upstream needs to know
        effects exist.
        """
        if not isinstance(getattr(timeline, "planning_diagnostics", None), dict):
            return
        if timeline.planning_diagnostics.get("effects_applied"):
            return
        intro_id = getattr(request, "intro_effect_media_id", None)
        outro_id = getattr(request, "outro_effect_media_id", None)
        intro = self.media.get(intro_id) if intro_id else None
        outro = self.media.get(outro_id) if outro_id else None
        # Automatic edits only accept video effects; an image effect (allowed in 手动微调) has no
        # motion to open or close on, so it is dropped with a note rather than frozen on screen.
        if intro is not None and getattr(intro, "kind", None) != "video":
            timeline.warnings.append(f"片头特效「{Path(intro.path).name}」不是视频，自动剪辑已跳过")
            intro = None
        if outro is not None and getattr(outro, "kind", None) != "video":
            timeline.warnings.append(f"片尾特效「{Path(outro.path).name}」不是视频，自动剪辑已跳过")
            outro = None
        if intro is None and outro is None:
            return
        cover = bool(getattr(request, "effect_cover_audio", False))
        # Cover off + original audio muted is the only case the effect needs its own audio
        # layer; when the original bed is kept, the effect's sound rides it in sequence and a
        # second copy would double it, and when covered the effect is just an ordinary clip.
        keep_own_audio = (not cover) and not getattr(timeline, "include_original_audio", False)
        if intro is not None:
            self._attach_effect(timeline, intro, "intro", cover, keep_own_audio)
        if outro is not None:
            self._attach_effect(timeline, outro, "outro", cover, keep_own_audio)
        self._resolve_clip_audio(timeline)
        timeline.planning_diagnostics["effects_applied"] = True

    def _attach_effect(self, timeline, item, where: str, cover: bool, keep_own_audio: bool) -> None:
        duration = self.renderer.probe_duration(item.path) or 0.0
        if duration <= 0:
            label = "片头特效" if where == "intro" else "片尾特效"
            timeline.warnings.append(f"{label}「{Path(item.path).name}」时长无法读取，已跳过")
            return
        clip = TimelineClip(
            media_id=item.id,
            source_path=item.path,
            start=0.0,
            duration=duration,
            kind="video",
            include_audio=keep_own_audio,
            timeline_start=0.0,
        )
        if where == "intro":
            for existing in timeline.clips:
                existing.timeline_start += duration
            timeline.clips.insert(0, clip)
            if not cover:
                # Push the narration (and its subtitles) and the music past the intro.
                if timeline.voiceover_path:
                    timeline.voiceover_start_seconds += duration
                if timeline.music_path:
                    timeline.music_delay_seconds += duration
        else:
            clip.timeline_start = max(
                (existing.timeline_start + existing.duration for existing in timeline.clips),
                default=0.0,
            )
            timeline.clips.append(clip)

    def is_path_in_use(self, path: str) -> bool:
        """Whether a queued or running render reads from or writes to this file.

        Renaming or deleting underneath a running job can either fail that job or publish an
        incomplete export, so every filesystem mutation asks this one authoritative inventory.
        It includes request ids because an automatic job has no timeline while it is analysing,
        and includes both export variants because the delivery remains live while its clean
        master is still being rendered.
        """
        if self.compositions and self.compositions.is_path_in_use(path):
            return True
        if self.media.captures.is_path_in_use(path):
            return True
        # Timeline drafts are not jobs yet. Their internally generated capture input remains a
        # valid manual-render dependency for this process lifetime and must survive safe cleanup
        # during the user's preview/review interval.
        if self.media.is_capture_input_path(path):
            return True
        wanted = self._path_key(path)
        for job in self._jobs.values():
            if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                continue
            # Automatic jobs do not have a timeline until analysis finishes. Resolve their
            # accepted media ids as well, otherwise a rename during OpenCV/music analysis can
            # move a file out from under the reader before `job.timeline` exists.
            request = job.request
            dependency_ids = [
                *request.media_ids,
                request.music_media_id,
                request.voiceover_media_id,
                request.intro_effect_media_id,
                request.outro_effect_media_id,
            ]
            request_paths = [
                item.path
                for media_id in dependency_ids
                if media_id and (item := self.media.get(media_id)) is not None
            ]
            if any(self._path_key(candidate) == wanted for candidate in request_paths):
                return True
            timeline = job.timeline
            if timeline is None:
                continue
            candidates = [
                getattr(timeline, "output_path", None),
                getattr(timeline, "music_path", None),
                getattr(timeline, "voiceover_path", None),
                getattr(job, "result_path", None),
            ]
            audio_bed = getattr(timeline, "audio_bed", None)
            if audio_bed is not None:
                candidates.append(getattr(audio_bed, "source_path", None))
            candidates.extend(clip.source_path for clip in getattr(timeline, "clips", []) or [])
            track = getattr(timeline, "subtitles", None)
            if track is not None and getattr(track, "cues", None):
                candidates.append(str(self.renderer.master_output_path(timeline)))
            if any(candidate and self._path_key(candidate) == wanted for candidate in candidates):
                return True
        return False

    @staticmethod
    def _path_key(path: str) -> str:
        """Canonical comparison key for existing inputs and not-yet-created outputs."""
        try:
            resolved = Path(path).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, ValueError):
            return os.path.normcase(str(path))
        return os.path.normcase(str(resolved))

    @staticmethod
    def _safe_output_name(value: str) -> str:
        """A portable MP4 basename, never a relative or absolute path."""
        name = validate_filename(value, ".mp4")
        if Path(name).suffix.lower() != ".mp4":
            raise ValueError("Export filename must end in .mp4")
        return name

    @classmethod
    def _managed_export_path(cls, value: str | Path) -> Path:
        """Resolve one flat output below the managed exports directory, or fail closed."""
        candidate = Path(value).expanduser()
        try:
            resolved = candidate.resolve(strict=False)
            export_root = GENERATED_DIRS["exports"].resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError("Export path is invalid") from exc
        if resolved.parent != export_root:
            raise ValueError(
                "Export output must be written directly inside the managed exports folder"
            )
        if cls._safe_output_name(resolved.name) != resolved.name:
            raise ValueError("Export filename must be a safe .mp4 basename")
        return resolved

    def _next_output_name(self, title: str = "") -> str:
        """Name the export after what the operator called the edit.

        The title box was previously ignored, so every export was called 导出 regardless of
        what you typed above it.
        """
        stamp = datetime.now().astimezone().strftime("%m-%d %H-%M")
        prefix = safe_stem(title, "导出")
        exports_dir = GENERATED_DIRS["exports"]
        for index in range(10000):
            suffix = "" if index == 0 else f"_{index:02d}"
            name = f"{prefix} {stamp}{suffix}.mp4"
            if name in self._allocated_output_names:
                continue
            delivery = exports_dir / name
            master = delivery.with_name(f"{delivery.stem} 母版{delivery.suffix}")
            # A render owns this whole filename family. Reusing a delivery name merely because
            # its video is gone could overwrite a surviving subtitle layer or clean master and
            # attach yesterday's words to today's picture.
            reserved_paths = (
                delivery,
                master,
                delivery.with_suffix(".ass"),
                delivery.with_suffix(".subtitles.json"),
                master.with_suffix(".ass"),
                master.with_suffix(".subtitles.json"),
            )
            if any(path.exists() for path in reserved_paths):
                continue
            self._allocated_output_names.add(name)
            return name
        raise RuntimeError("Unable to allocate export filename")
