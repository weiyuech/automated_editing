from __future__ import annotations

import asyncio
import os
import random
from datetime import datetime
from pathlib import Path

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    EDIT_CONTOUR_LEVELS,
    EDIT_EMPHASIS_LEVELS,
    EDIT_PACE_LEVELS,
    EDIT_SCOPE_LEVELS,
    FOOTAGE_MIX_LEVELS,
    EditBatchRequest,
    EditJobRequest,
    EditTimeline,
    JobRecord,
    JobStatus,
    SubtitleTrack,
    TimelineClip,
    TimelineDraftRequest,
    utc_now,
)
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.core.store import write_json
from automated_video_editing_backend.services.analysis import AnalysisService
from automated_video_editing_backend.services.capture import inspect_sidecar
from automated_video_editing_backend.services.editorial import (
    FAMILY_LABELS,
    TimelineCandidate,
    resolve_family_policy,
    score_timeline,
    select_slot_candidate,
    smart_family,
)
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.naming import safe_stem, validate_filename
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.semantic import SemanticAlignment, SemanticService
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
        analysis: AnalysisService,
        planner: EditPlanner,
        renderer: RenderService,
        settings: SettingsService | None = None,
        semantic: SemanticService | None = None,
    ) -> None:
        self.events = events
        self.media = media
        self.analysis = analysis
        self.planner = planner
        self.renderer = renderer
        # Optional so the planner can be exercised without a settings file; without one there
        # is no day's allowance to spend and only the material limit applies.
        self.settings = settings
        self.semantic = semantic or SemanticService()
        self._jobs: dict[str, JobRecord] = {}
        self._allocated_output_names: set[str] = set()
        self._render_slots = asyncio.Semaphore(1)

    def list_jobs(self) -> list[JobRecord]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    async def editing_capabilities(
        self, media_ids: list[str], music_media_ids: list[str],
    ) -> dict:
        """Cheap, honest recognition status for the one automatic-editing UI."""
        videos = [self.media.get(media_id) for media_id in dict.fromkeys(media_ids)]
        videos = [item for item in videos if item is not None and item.kind == "video"]
        point_items = [inspect_sidecar(item.path) for item in videos]
        usable = [item for item in point_items if item["evidence"] in {"full", "markers_only"}]
        points = sum(item["point_count"] for item in usable)
        successful = sum(item["successful_points"] for item in usable)
        failed = sum(item["failed_points"] for item in usable)
        if not videos:
            point_evidence, point_message = "none", "先选择视频素材"
        elif usable:
            if len(usable) < len(videos):
                point_evidence = "partial"
                suffix = "部分素材带点位信息"
            elif all(item["evidence"] == "full" for item in usable):
                point_evidence = "full"
                suffix = "信息完整"
            else:
                point_evidence = "markers_only"
                suffix = "基础信息"
            outcome = "全部到达" if failed == 0 and successful else f"{successful} 个到达 · {failed} 个未完成"
            point_message = f"已识别 {points} 个点位 · {suffix} · {outcome}"
        elif any(item["evidence"] == "invalid" for item in point_items):
            point_evidence, point_message = "invalid", "发现点位文件，但没有可用的到达信息"
        else:
            point_evidence, point_message = "none", "未识别点位 · 将按画面内容剪辑"

        described = [self.semantic.source_description_count(item) for item in videos]
        description_count = sum(described)
        described_sources = sum(count > 0 for count in described)
        if not videos:
            semantic_evidence, semantic_message = "none", "先选择视频素材"
        elif description_count == 0:
            semantic_evidence = "none"
            semantic_message = "未识别画面匹配备注 · 继续自动剪辑"
        elif described_sources < len(videos):
            semantic_evidence = "partial"
            semantic_message = f"已识别 {description_count} 个点位描述 · 部分素材可参与旁白匹配"
        else:
            semantic_evidence = "full"
            semantic_message = f"已识别 {description_count} 个点位描述 · 有旁白时自动匹配画面"

        music_items = [self.media.get(media_id) for media_id in dict.fromkeys(music_media_ids)]
        music_items = [item for item in music_items if item is not None and item.kind == "audio"]
        async def inspect_music(item):
            warnings: list[str] = []
            result = await asyncio.to_thread(self.analysis.analyze_music, Path(item.path), warnings)
            return result, warnings

        inspected = await asyncio.gather(*(inspect_music(item) for item in music_items))
        music_results = [result for result, _warnings in inspected]
        music_warnings = [warning for _result, warnings in inspected for warning in warnings]
        if not music_items:
            music_evidence, music_message = "none", "未添加音乐 · 将按画面节奏剪辑"
        elif all(item.evidence == "unreadable" for item in music_results):
            music_evidence, music_message = "unreadable", "音乐无法读取 · 将按画面节奏处理"
        elif all(item.evidence == "structured" for item in music_results):
            music_evidence = "structured"
            music_message = f"已分析 {len(music_results)} 首 · 节拍清晰 · 自动选段"
        elif all(item.evidence == "ambient" for item in music_results):
            music_evidence = "ambient"
            music_message = f"已分析 {len(music_results)} 首 · 氛围型 · 自动适配"
        else:
            music_evidence = "mixed"
            music_message = f"已分析 {len(music_results)} 首 · 将按每首结构自动适配"
        return {
            "points": {
                "evidence": point_evidence,
                "count": points,
                "successful": successful,
                "failed": failed,
                "message": point_message,
            },
            "semantic": {
                "evidence": semantic_evidence,
                "count": description_count,
                "source_count": described_sources,
                "message": semantic_message,
            },
            "music": {
                "evidence": music_evidence,
                "count": len(music_results),
                "message": music_message,
                "warnings": music_warnings,
            },
        }

    async def create(self, request: EditJobRequest) -> JobRecord:
        self._resolve_framing(request)
        self._validate_job_request(request)
        self._resolve_policy(request)
        if not request.output_name or request.output_name == "export.mp4":
            request.output_name = self._next_output_name(request.title)
        else:
            request.output_name = self._safe_output_name(request.output_name)
        return await self._announce(request)

    async def _announce(self, request: EditJobRequest, timeline=None) -> JobRecord:
        """Publish and queue one job, including a timeline already selected by a batch."""
        job = JobRecord(request=request, timeline=timeline)
        self._jobs[job.id] = job
        await self.events.publish("JOB_CREATED", job.model_dump(mode="json"))
        asyncio.create_task(self._run(job.id))
        return job

    async def create_batch(self, request: EditBatchRequest) -> list[JobRecord]:
        """Deal one batch of outputs, every choice in it reproducible from a single number.

        A hundred videos a day is the volume at which "regenerate that one from Tuesday" and
        "this batch came out badly, roll it again" both become routine, and neither is
        possible from an unseeded run. The batch seed drives the pools, the ordering and each
        output's own variant seed, so the whole day replays from it; each job also carries its
        variant seed, so one output can be rebuilt without rerunning the other ninety-nine.
        """
        # Freeze once for the whole batch. An operator saving a different preset while jobs are
        # being announced must not split one batch between landscape and portrait.
        self._resolve_framing(request)
        self._validate_batch_request(request)
        if request.editorial_preset is not None:
            return await self._create_automatic_batch(request)
        count = self._allowed_output_count(request)
        batch_seed = request.seed if request.seed is not None else random.SystemRandom().randrange(MAX_SEED)
        rng = random.Random(batch_seed)
        source_ids = self._deal_sources(request.media_ids, count)
        music_ids = self._deal_pool(request.music_media_ids, count, rng)
        voiceover_ids = self._deal_voiceovers(source_ids, request.voiceover_media_ids, rng)
        self._avoid_repeat_pairs(music_ids, voiceover_ids, rng)
        # 专业剪辑 has no quality score, so a scarce ("auto") effect deal is a random subset.
        intro_effect_ids = self._deal_effects(request.intro_effect_media_ids, count, request.effect_scope, rng)
        outro_effect_ids = self._deal_effects(request.outro_effect_media_ids, count, request.effect_scope, rng)
        variant_seeds = self._deal_variant_seeds(request.cut_variation, count, rng)
        signatures = self._deal_source_signatures(request, source_ids, rng)
        jobs: list[JobRecord] = []
        for index in range(count):
            job_request = EditJobRequest(
                title=f"{request.title} {index + 1:03d}",
                # One source recording is one narrative. Mixing five independent recordings
                # inside all ten outputs destroys each recording's chronology; dealing the
                # sources gives the requested two intact outputs per source instead.
                media_ids=[source_ids[index]],
                music_media_id=music_ids[index],
                voiceover_media_id=voiceover_ids[index],
                output_name=self._next_output_name(request.title),
                target_duration_seconds=request.target_duration_seconds,
                mute_original_audio=request.mute_original_audio,
                beat_sync=request.beat_sync,
                output_aspect_ratio=request.output_aspect_ratio,
                output_crop_x=request.output_crop_x,
                output_crop_y=request.output_crop_y,
                variant_seed=variant_seeds[index],
                batch_seed=batch_seed,
                # Carried unchanged to every output. Subtitles are not one of the dimensions a
                # batch spreads across — a day's videos should differ in how they are cut, not
                # in whether they can be watched with the sound off.
                subtitles=request.subtitles,
                subtitle_font=request.subtitle_font,
                subtitle_size=request.subtitle_size,
                intro_effect_media_id=intro_effect_ids[index],
                outro_effect_media_id=outro_effect_ids[index],
                effect_cover_audio=request.effect_cover_audio,
                **signatures[index],
            )
            jobs.append(await self.create(job_request))
        if self.settings is not None:
            self.settings.record_outputs(len(jobs))
        return jobs

    async def _create_automatic_batch(self, request: EditBatchRequest) -> list[JobRecord]:
        """Plan a portfolio first and render only the selected outputs.

        Four candidate timelines per requested delivery is enough to vary source intervals,
        coherent policy choices, and music windows without multiplying the expensive render
        count. Video and music analysis are shared across the whole batch.
        """
        count = request.output_count
        if self.settings is not None:
            count = min(count, self.settings.output_quota().remaining_today)
        if count <= 0:
            raise ValueError("今日自动剪辑产出已达上限，可在设置中调整每日上限")

        batch_seed = request.seed if request.seed is not None else random.SystemRandom().randrange(MAX_SEED)
        rng = random.Random(batch_seed)
        media_items = [self.media.get(media_id) for media_id in request.media_ids]
        media_items = [item for item in media_items if item is not None]
        item_by_id = {item.id: item for item in media_items}
        analyses = [await self.analysis.analyze_video(item) for item in media_items]
        analysis_by_id = {analysis.media_id: analysis for analysis in analyses}
        point_capability = {
            item.id: inspect_sidecar(item.path)["evidence"] in {"full", "markers_only"}
            for item in media_items
        }
        source_ids = self._deal_sources(request.media_ids, count)

        music_ids = self._deal_pool(request.music_media_ids, count, rng)
        voiceover_ids = self._deal_voiceovers(source_ids, request.voiceover_media_ids, rng)
        self._avoid_repeat_pairs(music_ids, voiceover_ids, rng)
        music_analyses = {}
        for media_id in {value for value in music_ids if value}:
            music = self.media.get(media_id)
            if music is not None:
                music_analyses[media_id] = await asyncio.to_thread(
                    self.analysis.analyze_music, Path(music.path), [],
                )

        selected: list[TimelineCandidate] = []
        rejected: list[dict] = []
        for slot in range(count):
            source_id = source_ids[slot]
            source = item_by_id[source_id]
            source_analysis = analysis_by_id[source_id]
            has_points = point_capability[source_id]
            music_id = music_ids[slot]
            voiceover_id = voiceover_ids[slot]
            music = self.media.get(music_id) if music_id else None
            voiceover = self.media.get(voiceover_id) if voiceover_id else None
            voiceover_duration = self._audio_duration(voiceover)
            semantic_alignment = self.semantic.align(source, voiceover, voiceover_duration)
            music_analysis = music_analyses.get(music_id)
            structured_music = bool(music_analysis and music_analysis.evidence == "structured")
            requested = request.editorial_preset or "smart"
            family = (
                smart_family(slot, count, has_points, structured_music)
                if requested == "smart" else requested
            )
            candidates: list[TimelineCandidate] = []
            for attempt in range(4):
                variant_seed = rng.randrange(MAX_SEED)
                candidate_rng = random.Random(variant_seed)
                ordered_media = [source]
                job_request = EditJobRequest(
                    title=f"{request.title} {slot + 1:03d}",
                    media_ids=[source_id],
                    music_media_id=music_id,
                    voiceover_media_id=voiceover_id,
                    output_name=f".candidate-{batch_seed}-{slot}-{attempt}.mp4",
                    target_duration_seconds=request.target_duration_seconds,
                    mute_original_audio=request.mute_original_audio,
                    beat_sync=request.beat_sync,
                    output_aspect_ratio=request.output_aspect_ratio,
                    output_crop_x=request.output_crop_x,
                    output_crop_y=request.output_crop_y,
                    variant_seed=variant_seed,
                    batch_seed=batch_seed,
                    editorial_preset=family,
                    music_window_rank=slot + attempt,
                    subtitles=request.subtitles,
                    subtitle_font=request.subtitle_font,
                    subtitle_size=request.subtitle_size,
                )
                resolve_family_policy(
                    job_request, family, candidate_rng,
                    has_points=has_points, has_music=structured_music,
                )
                timeline = self.planner.plan(
                    job_request,
                    ordered_media,
                    [source_analysis],
                    music,
                    voiceover,
                    voiceover_duration=voiceover_duration,
                    source_size=self._source_size(ordered_media),
                    music_analysis=music_analysis,
                    semantic_alignment=semantic_alignment,
                )
                self._resolve_original_audio(
                    request.mute_original_audio, [item.path for item in ordered_media], timeline,
                )
                score, components = score_timeline(
                    timeline, [source_analysis], family, music_analysis,
                )
                self._assert_batch_timeline_integrity(timeline, source_id)
                candidates.append(TimelineCandidate(
                    slot=slot,
                    request=job_request,
                    timeline=timeline,
                    score=score,
                    components=components,
                    preset=family,
                    music_id=music_id,
                ))
            chosen = select_slot_candidate(candidates, selected)
            selected.append(chosen)
            for candidate in candidates:
                if candidate is not chosen:
                    rejected.append({
                        "slot": slot + 1,
                        "source_media_id": source_id,
                        "preset": candidate.preset,
                        "score": candidate.score,
                        "variant_seed": candidate.request.variant_seed,
                        "reason": "同一输出位中质量或与已选结果的差异度较低",
                    })

        # Effects are scarce, so "auto" decorates the best-scored outputs first; "all" gives
        # every output one, reusing the pool.
        effect_ranking = sorted(range(len(selected)), key=lambda i: selected[i].score, reverse=True)
        intro_effect_ids = self._deal_effects(
            request.intro_effect_media_ids, len(selected), request.effect_scope, rng, ranking=effect_ranking,
        )
        outro_effect_ids = self._deal_effects(
            request.outro_effect_media_ids, len(selected), request.effect_scope, rng, ranking=effect_ranking,
        )

        jobs: list[JobRecord] = []
        for index, candidate in enumerate(selected):
            candidate.request.intro_effect_media_id = intro_effect_ids[index]
            candidate.request.outro_effect_media_id = outro_effect_ids[index]
            candidate.request.effect_cover_audio = request.effect_cover_audio
            candidate.request.output_name = self._next_output_name(request.title)
            candidate.request.title = f"{request.title} {index + 1:03d}"
            candidate.timeline.title = candidate.request.title
            candidate.timeline.output_path = str(
                GENERATED_DIRS["exports"] / candidate.request.output_name
            )
            candidate.timeline.planning_diagnostics.update({
                "batch_seed": batch_seed,
                "portfolio_index": index + 1,
                "portfolio_size": len(selected),
                "preset_label": FAMILY_LABELS[candidate.preset],
                "quality_score": candidate.score,
                "score_components": candidate.components,
                "source_media_id": candidate.request.media_ids[0],
                "source_path": candidate.timeline.clips[0].source_path if candidate.timeline.clips else "",
                "source_allocation": "one_recording_per_output",
            })
            jobs.append(await self._announce(candidate.request, candidate.timeline))

        self._write_batch_manifest(request, batch_seed, selected, rejected)
        if self.settings is not None:
            self.settings.record_outputs(len(jobs))
        return jobs

    def _write_batch_manifest(self, request, batch_seed, selected, rejected) -> None:
        path = GENERATED_DIRS["logs"] / f"batch-plan-{batch_seed}.json"
        write_json(path, {
            "planner_version": 2,
            "batch_seed": batch_seed,
            "requested_preset": request.editorial_preset,
            "requested_count": request.output_count,
            "source_allocation": "one_recording_per_output",
            "source_output_counts": {
                media_id: sum(
                    1 for item in selected if item.request.media_ids == [media_id]
                )
                for media_id in dict.fromkeys(request.media_ids)
            },
            "candidate_count": len(selected) + len(rejected),
            "selected_count": len(selected),
            "selected": [
                {
                    "index": index + 1,
                    "output_name": item.request.output_name,
                    "preset": item.preset,
                    "preset_label": FAMILY_LABELS[item.preset],
                    "score": item.score,
                    "components": item.components,
                    "variant_seed": item.request.variant_seed,
                    "source_media_id": item.request.media_ids[0],
                    "source_path": item.timeline.clips[0].source_path if item.timeline.clips else "",
                    "music_media_id": item.music_id,
                    "music_start_seconds": item.timeline.music_start_seconds,
                    "music_duration_seconds": item.timeline.music_duration_seconds,
                    "clips": [clip.model_dump(mode="json") for clip in item.timeline.clips],
                }
                for index, item in enumerate(selected)
            ],
            "rejected": rejected,
        })

    def _allowed_output_count(self, request: EditBatchRequest) -> int:
        """How many outputs this batch may actually make.

        Two limits, for two different reasons, and the smaller wins.

        The first is what the material can carry. Asking for a hundred videos out of fifteen
        distinct combinations does not produce a hundred videos — it produces fifteen, six
        times each, at six times the render cost, and someone has to watch all of them to find
        that out. Capping at what is genuinely different is not a restriction; it is refusing
        to charge for copies.

        The second is the day's allowance, which is a property of the app rather than of any
        one batch, and is the only one an operator can raise.
        """
        wanted = request.output_count
        allowed = min(wanted, self._distinct_capacity(request))
        if self.settings is not None:
            allowed = min(allowed, self.settings.output_quota().remaining_today)
        if allowed <= 0:
            raise ValueError("今日自动剪辑产出已达上限，可在设置中调整每日上限")
        return allowed

    def _distinct_capacity(self, request: EditBatchRequest) -> int:
        """How many meaningfully different videos this request can yield.

        Capacity is additive across independent source recordings because an output belongs to
        exactly one of them. Within a source it is editing choices times soundtracks times
        narrations. The sampling seed is deliberately not a factor: it varies which seconds
        are used, which on footage of one place is a difference nobody watching would name.
        """
        capacity = 0
        for media_id in dict.fromkeys(request.media_ids):
            scoped = request.model_copy(deep=True)
            scoped.media_ids = [media_id]
            labelled, _several, musical = self._what_the_footage_supports(scoped)
            axes = [
                len(scoped.paces or EDIT_PACE_LEVELS),
                len([
                    level for level in (scoped.contours or EDIT_CONTOUR_LEVELS)
                    if musical or level != "follow_energy"
                ]),
                len(scoped.footage_mixes or FOOTAGE_MIX_LEVELS) if labelled else 1,
                len(scoped.emphases or EDIT_EMPHASIS_LEVELS) if labelled else 1,
                len(scoped.point_scopes or EDIT_SCOPE_LEVELS) if labelled else 1,
                max(1, len(dict.fromkeys(scoped.music_media_ids))),
                max(1, len(dict.fromkeys(scoped.voiceover_media_ids))),
            ]
            source_capacity = 1
            for size in axes:
                source_capacity *= max(1, size)
            capacity += source_capacity
        return capacity

    async def create_from_timeline(self, timeline) -> JobRecord:
        if not str(timeline.output_path or "").strip():
            timeline.output_path = str(GENERATED_DIRS["exports"] / self._next_output_name(timeline.title))
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
                None if timeline.output_fit == "contain"
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

        for clip in timeline.clips:
            source_key = self._path_key(clip.source_path)
            matching = by_path.get(source_key, [])
            # Check burned provenance before a stale-id diagnostic: the physical file is what
            # FFmpeg would open, so a forged id must never turn the safety message into an
            # opportunity to retry the same unsafe pixels through another client.
            if any(bool(item.metadata.get("has_burned_subtitles")) for item in matching):
                raise ValueError(
                    "已选成片的字幕已烧录在画面中，不能用于手动微调；"
                    "请改用同组的母版（无字幕）"
                )
            claimed = self.media.get(clip.media_id)
            if claimed is not None and self._path_key(claimed.path) != source_key:
                raise ValueError("手动微调素材的 media_id 与文件路径不一致，请刷新媒体库后重试")
            if not matching:
                # Stale ids are harmless when the canonical path is still present in the
                # library after a restart. An unknown path is not: it could be an unregistered
                # partial export or a client-selected file outside every persistence boundary.
                raise ValueError("手动微调素材不在媒体库中，请重新选择后再渲染")

        bed = getattr(timeline, "audio_bed", None)
        if bed is None:
            if timeline.subtitles and timeline.subtitles.cues:
                raise ValueError("手动微调字幕没有可验证的原声绑定，请重新选择保留原声")
            timeline.subtitles = None
            return
        bed_items = by_path.get(self._path_key(bed.source_path), [])
        if not bed_items:
            raise ValueError("保留的原声不在媒体库中，请重新选择后再渲染")
        if any(bool(item.metadata.get("has_burned_subtitles")) for item in bed_items):
            raise ValueError(
                "保留的原声来自已烧录字幕的成片，不能用于手动微调；"
                "请改用同组的母版（无字幕）"
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
                "保留的原声记录过字幕，但字幕数据缺失、损坏或不属于该母版；"
                "请重新选择同组的有效母版"
            )
        # read_subtitle_sidecar has already applied strict persistence validation. Reconstructing
        # the model here intentionally discards envelope fields (video/version/has_voiceover) and
        # gives rendering exactly the layer owned by this audio bed, never a client-supplied copy.
        timeline.subtitles = SubtitleTrack.model_validate(authoritative, strict=True)

    async def draft_timeline(self, request: TimelineDraftRequest):
        job_request = EditJobRequest(
            title=request.title,
            media_ids=request.media_ids,
            music_media_id=request.music_media_id,
            voiceover_media_id=request.voiceover_media_id,
            output_name=self._next_output_name(request.title),
            target_duration_seconds=request.target_duration_seconds,
            mute_original_audio=request.mute_original_audio,
            beat_sync=request.beat_sync,
            output_aspect_ratio=request.output_aspect_ratio,
            output_crop_x=request.output_crop_x,
            output_crop_y=request.output_crop_y,
            variant_seed=request.variant_seed,
            editorial_preset=request.editorial_preset,
            music_window_rank=request.music_window_rank,
            footage_mix=request.footage_mix,
            emphasis=request.emphasis,
            pace=request.pace,
            contour=request.contour,
            point_scope=request.point_scope,
            recording_scope=request.recording_scope,
            start_rotation=request.start_rotation,
            # A preview is only worth looking at if it shows what the job will render, and
            # subtitles change how much of the frame is covered.
            subtitles=request.subtitles,
            subtitle_font=request.subtitle_font,
            subtitle_size=request.subtitle_size,
        )
        self._resolve_framing(job_request)
        self._validate_job_request(job_request)
        self._resolve_policy(job_request)
        media_items = [self.media.get(media_id) for media_id in job_request.media_ids]
        media_items = [item for item in media_items if item is not None]
        music = self.media.get(job_request.music_media_id) if job_request.music_media_id else None
        voiceover = self.media.get(job_request.voiceover_media_id) if job_request.voiceover_media_id else None
        analyses = []
        for item in media_items:
            analyses.append(await self.analysis.analyze_video(item))
        music_analysis = None
        if music:
            music_analysis = await asyncio.to_thread(
                self.analysis.analyze_music, Path(music.path), [],
            )
        timeline = self.planner.plan(
            job_request, media_items, analyses, music, voiceover,
            voiceover_duration=self._audio_duration(voiceover),
            source_size=self._source_size(media_items),
            music_analysis=music_analysis,
            semantic_alignment=self._semantic_alignment(media_items, voiceover),
        )
        self._resolve_original_audio(
            job_request.mute_original_audio, [item.path for item in media_items], timeline,
        )
        return timeline

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

    def _resolve_policy(self, request: EditJobRequest) -> None:
        """Choose any editing policy the operator left open, and record what was chosen.

        A policy left unset means "you pick", not "use the middle setting" — one video asked
        for on its own should be as likely to lean on movement as a batch of a hundred is.
        Defaulting quietly to `balanced` and `target` would make every hand-made edit the same
        edit, which is the thing all of this exists to stop.

        Resolved once, here, and written back onto the request rather than rolled again at
        render time, so the job record is an honest account of what it made and resending it
        reproduces that video. A seeded request derives its policy from the seed, so the seed
        alone still reproduces the whole job.
        """
        rng = random.Random(request.variant_seed) if request.variant_seed is not None else random.SystemRandom()
        for field, levels in (
            ("pace", EDIT_PACE_LEVELS),
            ("contour", EDIT_CONTOUR_LEVELS),
            ("footage_mix", FOOTAGE_MIX_LEVELS),
            ("emphasis", EDIT_EMPHASIS_LEVELS),
        ):
            if getattr(request, field) is None:
                setattr(request, field, rng.choice(levels))
        # A single edit uses everything it was given by default. Narrowing is a batch idea —
        # it exists so a hundred outputs differ, and there is nothing to differ from here.
        if request.point_scope is None:
            request.point_scope = "all"
        if request.recording_scope is None:
            request.recording_scope = "all"
        if request.start_rotation is None:
            request.start_rotation = 0

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

    def _validate_batch_request(self, request: EditBatchRequest) -> None:
        for media_id in request.media_ids:
            item = self.media.get(media_id)
            if not item or not MediaService.is_automatic_source(item):
                raise ValueError("Source videos must be imported video media")
        for media_id in request.music_media_ids:
            item = self.media.get(media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "music":
                raise ValueError("Music pool must contain music audio media")
        for media_id in request.voiceover_media_ids:
            item = self.media.get(media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "tts_voice":
                raise ValueError("Voiceover pool must contain generated TTS media")
        for media_id in dict.fromkeys([
            *request.intro_effect_media_ids,
            *request.outro_effect_media_ids,
        ]):
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
            job.message = "Analyzing media"
            job.updated_at = utc_now()
            await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

            if job.timeline:
                timeline = job.timeline
                job.progress = 0.6
                job.message = "Planning timeline"
                await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))
            else:
                media_items = [self.media.get(media_id) for media_id in job.request.media_ids]
                media_items = [item for item in media_items if item is not None]
                music = self.media.get(job.request.music_media_id) if job.request.music_media_id else None
                voiceover = self.media.get(job.request.voiceover_media_id) if job.request.voiceover_media_id else None
                if not media_items:
                    raise ValueError("No valid media items selected")

                analyses = []
                for item in media_items:
                    analyses.append(await self.analysis.analyze_video(item, music))

                job.progress = 0.45
                job.message = "Planning timeline"
                await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

                timeline = self.planner.plan(
                    job.request, media_items, analyses, music, voiceover,
                    voiceover_duration=self._audio_duration(voiceover),
                    source_size=self._source_size(media_items),
                    semantic_alignment=self._semantic_alignment(media_items, voiceover),
                )
                self._resolve_original_audio(
                    job.request.mute_original_audio, [item.path for item in media_items], timeline,
                )
                if job.request.batch_seed is not None and len(job.request.media_ids) == 1:
                    self._assert_batch_timeline_integrity(timeline, job.request.media_ids[0])
                # Kept on the record, not just used and dropped. `is_path_in_use` reads it to
                # refuse a rename of anything a running render is reading from, and could not
                # see an auto-planned job at all while this was a local variable.
                job.timeline = timeline

            # Both paths converge here with a finished timeline; attach any 片头/片尾 effects
            # the batch assigned to this output before it renders.
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
            inherited_burned_subtitles = self._inherits_burned_subtitles(timeline)
            has_voiceover = bool(
                getattr(timeline, "voiceover_path", None)
                or (
                    getattr(timeline, "audio_bed", None)
                    and timeline.audio_bed.has_voiceover
                )
            )
            planned_delivery = Path(timeline.output_path)
            planned_master = (
                self.renderer.master_output_path(timeline) if subtitled else None
            )
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
                sidecar = (
                    str(self.renderer.subtitle_sidecar_path(result_path)) if subtitled else ""
                )
                delivery_metadata = {
                        "source": "exports", "role": "export", "job_id": job.id,
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
                        "has_burned_subtitles": (
                            subtitled or inherited_burned_subtitles
                        ),
                    }
                master_path = await self.renderer.render_master(timeline)
                registrations = [(Path(result_path), "video", delivery_metadata)]
                if subtitled:
                    if not master_path:
                        raise RuntimeError("带字幕成片没有生成对应母版")
                    master_metadata = {
                            "source": "exports", "role": "export", "job_id": job.id,
                            "export_group": group,
                            "variant": "master",
                            "variant_label": "母版（无字幕）",
                            "subtitles_path": str(
                                self.renderer.subtitle_sidecar_path(master_path)
                            ),
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
        if any(
            target != export_root and export_root not in target.parents
            for target in resolved
        ):
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
            source_key = self._path_key(clip.source_path)
            claimed = self.media.get(clip.media_id)
            if claimed is not None and self._path_key(claimed.path) != source_key:
                # The path is what FFmpeg will really read. Never let a client pair it with a
                # different, harmless-looking id to evade persisted provenance checks.
                raise ValueError("手动微调素材的 media_id 与文件路径不一致，请刷新媒体库后重试")
            if any(
                bool(item.metadata.get("has_burned_subtitles"))
                for item in by_path.get(source_key, [])
            ):
                return True
        return False

    def _audio_duration(self, item) -> float | None:
        """Length of a voiceover, preferring the timing the TTS provider already reported."""
        if item is None:
            return None
        duration_ms = item.metadata.get("duration_ms")
        try:
            if duration_ms and float(duration_ms) > 0:
                return float(duration_ms) / 1000.0
        except (TypeError, ValueError):
            pass
        return self.renderer.probe_duration(item.path)

    def _semantic_alignment(
        self, media_items: list, voiceover,
    ) -> SemanticAlignment:
        """Semantic evidence is meaningful only inside one recording's chronology."""
        if len(media_items) != 1:
            return SemanticAlignment(reason="timeline contains multiple recordings")
        return self.semantic.align(
            media_items[0], voiceover, self._audio_duration(voiceover)
        )

    def _source_size(self, items) -> tuple[int, int] | None:
        return self._source_size_from_path(items[0].path) if items else None

    def _source_size_from_path(self, path: str) -> tuple[int, int] | None:
        if self.renderer is None:
            return None
        return self.renderer.probe_frame_size(path)

    def _resolve_original_audio(self, mute_original_audio: bool, paths: list[str], timeline) -> None:
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
            timeline.warnings.append(f"{len(unknown)} 个源视频无法确认音轨（ffprobe 未能读取），已改为静音原视频")
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
            media_id=item.id, source_path=item.path, start=0.0, duration=duration,
            kind="video", include_audio=keep_own_audio, timeline_start=0.0,
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
            candidates = [getattr(timeline, "output_path", None),
                          getattr(timeline, "music_path", None),
                          getattr(timeline, "voiceover_path", None),
                          getattr(job, "result_path", None)]
            audio_bed = getattr(timeline, "audio_bed", None)
            if audio_bed is not None:
                candidates.append(getattr(audio_bed, "source_path", None))
            candidates.extend(clip.source_path for clip in getattr(timeline, "clips", []) or [])
            track = getattr(timeline, "subtitles", None)
            if track is not None and getattr(track, "cues", None):
                candidates.append(str(self.renderer.master_output_path(timeline)))
            if any(
                candidate and self._path_key(candidate) == wanted
                for candidate in candidates
            ):
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

    def _deal_sources(self, media_ids: list[str], count: int) -> list[str]:
        """Balance deliveries across recordings without ever mixing them.

        When there are at least as many outputs as sources this is round-robin, so five inputs
        and ten outputs is exactly two each. When there are fewer outputs, sample the ordered
        source list at evenly spaced midpoints rather than silently taking only its beginning.
        No random draw is involved: source ownership should be transparent and stable even
        when a user changes the batch seed to re-roll the cuts.
        """
        sources = list(dict.fromkeys(media_ids))
        if not sources or count <= 0:
            return []
        if count >= len(sources):
            return [sources[index % len(sources)] for index in range(count)]
        return [
            sources[min(len(sources) - 1, int((index + 0.5) * len(sources) / count))]
            for index in range(count)
        ]

    def _deal_source_signatures(
        self,
        request: EditBatchRequest,
        source_ids: list[str],
        rng: random.Random,
    ) -> list[dict[str, object]]:
        """Deal professional policies against each source's own evidence.

        A batch may contain one cruise sidecar and four ordinary imports. Resolving capability
        once for the whole batch made the plain videos receive point policies that could not do
        anything. Grouping output slots by their assigned source keeps the same public controls
        while every resolved field is truthful for the recording that output actually uses.
        """
        signatures: list[dict[str, object] | None] = [None] * len(source_ids)
        indexes_by_source: dict[str, list[int]] = {}
        for index, media_id in enumerate(source_ids):
            indexes_by_source.setdefault(media_id, []).append(index)
        for media_id, indexes in indexes_by_source.items():
            scoped = request.model_copy(deep=True)
            scoped.media_ids = [media_id]
            scoped.recording_scopes = []
            scoped.start_rotations = 1
            dealt = self._deal_signatures(scoped, len(indexes), rng)
            for index, signature in zip(indexes, dealt):
                signatures[index] = signature
        return [signature or {} for signature in signatures]

    def _assert_batch_timeline_integrity(self, timeline, source_id: str) -> None:
        """Fail before rendering if a batch timeline violates its source contract."""
        if not timeline.clips:
            raise RuntimeError("Batch planner produced an empty timeline")
        if any(clip.media_id != source_id for clip in timeline.clips):
            raise RuntimeError("Batch planner mixed multiple source recordings in one output")
        looping = any("画面循环" in warning for warning in timeline.warnings)
        if not looping and any(
            first.start + first.duration > second.start + 1e-6
            for first, second in zip(timeline.clips, timeline.clips[1:])
        ):
            raise RuntimeError("Batch planner produced a non-chronological source timeline")

    def _deal_signatures(
        self,
        request: EditBatchRequest,
        count: int,
        rng: random.Random,
    ) -> list[dict[str, object]]:
        """Give every output its own point in the space of editing decisions.

        Two properties are wanted and neither implies the other. Every level of every dimension
        should get its fair share of the batch, and no two outputs should make the same set of
        choices while unused combinations remain. Dealing each dimension from its own deck
        gives the first and lets combinations clump; drawing distinct points out of the product
        gives the second and, where the product dwarfs the batch, leaves each dimension's share
        to luck.

        So: deal each dimension balanced, then break repeated combinations by swapping a single
        dimension's value between two outputs. A swap moves a level from one output to another
        without changing how many outputs hold it, so balance survives any number of swaps by
        construction, and the repeats go.

        Only dimensions the operator left open are dealt. A pinned one is a constant, not a
        one-level axis: leaving it in and overriding it afterwards would spend deck positions
        on choices already made.
        """
        labelled, _several, musical = self._what_the_footage_supports(request)
        axes: list[tuple[str, list]] = [
            ("pace", list(request.paces or EDIT_PACE_LEVELS)),
            ("contour", [
                level for level in (request.contours or EDIT_CONTOUR_LEVELS)
                if musical or level != "follow_energy"
            ]),
            ("footage_mix", list(request.footage_mixes or FOOTAGE_MIX_LEVELS) if labelled else []),
            ("emphasis", list(request.emphases or EDIT_EMPHASIS_LEVELS) if labelled else []),
            ("point_scope", list(request.point_scopes or EDIT_SCOPE_LEVELS) if labelled else []),
            # A batch output belongs to one recording and follows it forward. These older API
            # fields remain serialised for compatibility but no longer act as hidden diversity
            # axes that contradict the product's timeline-integrity rule.
            ("recording_scope", []),
            ("start_rotation", []),
        ]
        live = [(name, levels) for name, levels in axes if levels]
        columns = {name: self._deal_pool(levels, count, rng) for name, levels in live}
        dealt = [{name: columns[name][index] for name, _levels in live} for index in range(count)]
        self._separate(dealt, [name for name, _levels in live], rng)

        # A dimension with nothing to act on is recorded at its neutral level rather than left
        # unset. Left unset it would be rolled per job like any unanswered choice, and the
        # record would claim a decision that changed nothing about the video.
        neutral = {
            "footage_mix": "balanced", "emphasis": "target",
            "point_scope": "all", "recording_scope": "all", "start_rotation": 0,
        }
        dropped = {name for name, levels in axes if not levels}
        for signature in dealt:
            signature.update({name: neutral[name] for name in dropped if name in neutral})

        # C1: one output uses everything. Left to the draw, the complete edit — the one most
        # likely to be wanted — can simply fail to be made. Pinned here it costs exactly one
        # output of the spread, which is why full scope is not a dealt level.
        if dealt:
            dealt[0].update({"point_scope": "all", "recording_scope": "all", "start_rotation": 0})
        return dealt

    def _what_the_footage_supports(self, request: EditBatchRequest) -> tuple[bool, bool, bool]:
        """Which dimensions can do anything at all, given what is actually being edited.

        A dimension with nothing to act on has one level, not several. Dealing four rotations
        across footage with no points to rotate does not make four different videos — it makes
        one video four times while spending the batch's variety on a choice that changes
        nothing, and reports a spread that was never there.

        Cheap to establish: whether any source carries cruise spans is a sidecar read, and the
        other two are properties of the request.
        """
        media_ids = list(dict.fromkeys(request.media_ids))
        labelled = False
        for media_id in media_ids:
            item = self.media.get(media_id)
            if item is None:
                continue
            capability = inspect_sidecar(item.path)
            if capability["evidence"] in {"full", "markers_only"}:
                labelled = True
                break
        return labelled, len(media_ids) > 1, bool(request.music_media_ids) and request.beat_sync

    def _separate(
        self,
        signatures: list[dict[str, object]],
        names: list[str],
        rng: random.Random,
        passes: int = 60,
    ) -> None:
        """Break repeated combinations, leaving every dimension's balance untouched.

        Swapping one dimension's value between two outputs moves a level from one to the other
        without changing how many outputs hold it, so the counts dealt above survive intact
        however many swaps this takes. Bounded, because with fewer distinct combinations than
        outputs some repetition is arithmetic rather than a fault.
        """
        for _ in range(passes):
            seen: set[tuple] = set()
            clashes = []
            for index, signature in enumerate(signatures):
                key = tuple(signature[name] for name in names)
                if key in seen:
                    clashes.append(index)
                else:
                    seen.add(key)
            if not clashes:
                return
            for index in clashes:
                name = rng.choice(names)
                other = rng.randrange(len(signatures))
                signatures[index][name], signatures[other][name] = (
                    signatures[other][name], signatures[index][name],
                )

    def _deal_variant_seeds(
        self,
        variation: str,
        output_count: int,
        rng: random.Random,
    ) -> list[int | None]:
        """Decide whether a batch's outputs differ in picture, or only in sound.

        Varied cuts are usually what a hundred-a-day operation wants, but not always. Holding
        the picture still is the only way to judge two soundtracks against each other, and
        an edit that has already been approved should not quietly re-cut itself when it is
        rendered again. Both are the same mechanism as the variety: a seed is a fixed choice,
        so pinning one is not the absence of seeding.
        """
        if variation == "fixed":
            # No seed at all, which is the pick every export used before seeding existed.
            return [None] * output_count
        if variation == "shared":
            return [rng.randrange(MAX_SEED)] * output_count
        return [rng.randrange(MAX_SEED) for _ in range(output_count)]

    def _deal_pool(self, media_ids: list[str], output_count: int, rng: random.Random) -> list[str | None]:
        """Deal one asset to every output, reshuffling the deck when it empties.

        Ticking a pool means "use it", so an empty slot is never dealt while the pool has
        anything in it. The previous version spread each asset over at most one output and
        padded the rest with None: one music, one voiceover and ten outputs produced one
        output with music, one with narration, and eight silent ones.
        """
        deck = list(dict.fromkeys(media_ids))
        if not deck:
            return [None] * output_count
        dealt: list[str | None] = []
        while len(dealt) < output_count:
            shuffled = list(deck)
            rng.shuffle(shuffled)
            dealt.extend(shuffled[:output_count - len(dealt)])
        return dealt

    def _deal_effects(
        self,
        effect_ids: list[str],
        output_count: int,
        scope: str,
        rng: random.Random,
        ranking: list[int] | None = None,
    ) -> list[str | None]:
        """Which output gets which effect from one pool (片头 or 片尾).

        ``scope == "all"`` gives every output a bumper, reusing the pool like _deal_pool.
        ``scope == "auto"`` is scarce: only as many outputs as there are distinct effects are
        decorated, one distinct effect each. ``ranking`` (best output index first, from the
        scored 智能剪辑 portfolio) picks those outputs; without it — 专业剪辑 has no scores — the
        subset is chosen at random from the batch seed.
        """
        deck = list(dict.fromkeys(effect_ids))
        result: list[str | None] = [None] * output_count
        if not deck or output_count <= 0:
            return result
        if scope == "all":
            return self._deal_pool(deck, output_count, rng)
        take = min(len(deck), output_count)
        if ranking is not None:
            chosen = list(ranking)[:take]
        else:
            chosen = rng.sample(range(output_count), take)
        effects = list(deck)
        rng.shuffle(effects)
        for offset, out_index in enumerate(chosen):
            result[out_index] = effects[offset]
        return result

    def _deal_voiceovers(
        self,
        source_ids: list[str],
        media_ids: list[str],
        rng: random.Random,
    ) -> list[str | None]:
        """Keep the balanced voiceover deck, then improve source/content compatibility.

        Swapping positions never changes how many times a selected narration is used. With no
        explicit point descriptions or no TTS text all affinities are neutral and this is
        byte-for-byte the old dealt pool. Where both sides have meaning, model similarity (or
        the local lexical fallback) prevents a warehouse script being assigned to a showroom
        while the matching pair sits in another output slot.
        """
        dealt = self._deal_pool(media_ids, len(source_ids), rng)
        choices = [item for item in dict.fromkeys(dealt) if item]
        if len(source_ids) < 2 or len(choices) < 2:
            return dealt

        scores: dict[tuple[int, str], float] = {}
        for index, source_id in enumerate(source_ids):
            source = self.media.get(source_id)
            for voice_id in choices:
                voice = self.media.get(voice_id)
                affinity = self.semantic.compatibility(source, voice) if source is not None else None
                # Unknown means a generic narration, not a bad one. It should preserve the old
                # deck rather than be pushed below a measured but weak match.
                scores[index, voice_id] = 0.5 if affinity is None else affinity

        for _pass in range(len(dealt)):
            best: tuple[float, int, int] | None = None
            for first in range(len(dealt) - 1):
                a = dealt[first]
                if not a:
                    continue
                for second in range(first + 1, len(dealt)):
                    b = dealt[second]
                    if not b or a == b:
                        continue
                    gain = (
                        scores[first, b] + scores[second, a]
                        - scores[first, a] - scores[second, b]
                    )
                    if gain > 1e-6 and (best is None or gain > best[0]):
                        best = (gain, first, second)
            if best is None:
                break
            _gain, first, second = best
            dealt[first], dealt[second] = dealt[second], dealt[first]
        return dealt

    def _avoid_repeat_pairs(
        self,
        music_ids: list[str | None],
        voiceover_ids: list[str | None],
        rng: random.Random,
    ) -> None:
        """Exhaust the distinct music × voiceover combinations before any pair repeats.

        Dealing the two decks independently can hand out the same pairing twice while other
        combinations go unused. Rotating one side against the other spends the grid first.
        """
        if not music_ids or not voiceover_ids:
            return
        distinct = len({item for item in music_ids if item}) * len({item for item in voiceover_ids if item})
        if distinct <= 1:
            return
        seen: set[tuple[str | None, str | None]] = set()
        for index in range(len(music_ids)):
            pair = (music_ids[index], voiceover_ids[index])
            if pair not in seen or len(seen) >= distinct:
                seen.add(pair)
                continue
            for offset in range(index + 1, len(music_ids)):
                candidate = (music_ids[offset], voiceover_ids[index])
                if candidate not in seen:
                    # Music carries no recording-specific meaning. Swapping it preserves a
                    # semantic source/voiceover pairing established by `_deal_voiceovers`.
                    music_ids[index], music_ids[offset] = music_ids[offset], music_ids[index]
                    break
            seen.add((music_ids[index], voiceover_ids[index]))
