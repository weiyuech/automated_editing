from __future__ import annotations

import asyncio
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
    TimelineDraftRequest,
    JobRecord,
    JobStatus,
    utc_now,
)
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.services.naming import safe_stem
from automated_video_editing_backend.services.analysis import AnalysisService
from automated_video_editing_backend.services.capture import read_sidecar
from automated_video_editing_backend.services.media import MediaService
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
        analysis: AnalysisService,
        planner: EditPlanner,
        renderer: RenderService,
        settings: "SettingsService | None" = None,
    ) -> None:
        self.events = events
        self.media = media
        self.analysis = analysis
        self.planner = planner
        self.renderer = renderer
        # Optional so the planner can be exercised without a settings file; without one there
        # is no day's allowance to spend and only the material limit applies.
        self.settings = settings
        self._jobs: dict[str, JobRecord] = {}
        self._allocated_output_names: set[str] = set()
        self._render_slots = asyncio.Semaphore(1)

    def list_jobs(self) -> list[JobRecord]:
        return list(self._jobs.values())

    def get(self, job_id: str) -> JobRecord | None:
        return self._jobs.get(job_id)

    async def create(self, request: EditJobRequest) -> JobRecord:
        self._resolve_framing(request)
        self._validate_job_request(request)
        self._resolve_policy(request)
        if not request.output_name or request.output_name == "export.mp4":
            request.output_name = self._next_output_name(request.title)
        job = JobRecord(request=request)
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
        count = self._allowed_output_count(request)
        batch_seed = request.seed if request.seed is not None else random.SystemRandom().randrange(MAX_SEED)
        rng = random.Random(batch_seed)
        music_ids = self._deal_pool(request.music_media_ids, count, rng)
        voiceover_ids = self._deal_pool(request.voiceover_media_ids, count, rng)
        self._avoid_repeat_pairs(music_ids, voiceover_ids, rng)
        variant_seeds = self._deal_variant_seeds(request.cut_variation, count, rng)
        signatures = self._deal_signatures(request, count, rng)
        jobs: list[JobRecord] = []
        for index in range(count):
            media_ids = list(request.media_ids)
            rng.shuffle(media_ids)
            job_request = EditJobRequest(
                title=f"{request.title} {index + 1:03d}",
                media_ids=media_ids,
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
                **signatures[index],
            )
            jobs.append(await self.create(job_request))
        if self.settings is not None:
            self.settings.record_outputs(len(jobs))
        return jobs

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

        Editing choices times soundtracks times narrations. The sampling seed is deliberately
        not a factor: it varies which seconds are used, which on footage of one place is a
        difference nobody watching would name.
        """
        labelled, several, musical = self._what_the_footage_supports(request)
        axes = [
            len(request.paces or EDIT_PACE_LEVELS),
            len([
                level for level in (request.contours or EDIT_CONTOUR_LEVELS)
                if musical or level != "follow_energy"
            ]),
            len(request.footage_mixes or FOOTAGE_MIX_LEVELS) if labelled else 1,
            len(request.emphases or EDIT_EMPHASIS_LEVELS) if labelled else 1,
            len(request.point_scopes or EDIT_SCOPE_LEVELS) if labelled else 1,
            len(request.recording_scopes or EDIT_SCOPE_LEVELS) if several else 1,
            request.start_rotations if labelled else 1,
            max(1, len(dict.fromkeys(request.music_media_ids))),
            max(1, len(dict.fromkeys(request.voiceover_media_ids))),
        ]
        capacity = 1
        for size in axes:
            capacity *= max(1, size)
        return capacity

    async def create_from_timeline(self, timeline) -> JobRecord:
        if not timeline.output_path or Path(timeline.output_path).is_dir():
            timeline.output_path = str(GENERATED_DIRS["exports"] / self._next_output_name(timeline.title))
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
            analyses.append(await self.analysis.analyze_video(item, music))
        timeline = self.planner.plan(
            job_request, media_items, analyses, music, voiceover,
            voiceover_duration=self._audio_duration(voiceover),
            source_size=self._source_size(media_items),
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
            if not item or item.kind != "video" or item.metadata.get("role") != "raw_video":
                raise ValueError("Source videos must be imported video media")
        if request.music_media_id:
            item = self.media.get(request.music_media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "music":
                raise ValueError("Music must be music audio media")
        if request.voiceover_media_id:
            item = self.media.get(request.voiceover_media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "tts_voice":
                raise ValueError("Voiceover must be generated TTS media")

    def _validate_batch_request(self, request: EditBatchRequest) -> None:
        for media_id in request.media_ids:
            item = self.media.get(media_id)
            if not item or item.kind != "video" or item.metadata.get("role") != "raw_video":
                raise ValueError("Source videos must be imported video media")
        for media_id in request.music_media_ids:
            item = self.media.get(media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "music":
                raise ValueError("Music pool must contain music audio media")
        for media_id in request.voiceover_media_ids:
            item = self.media.get(media_id)
            if not item or item.kind != "audio" or item.metadata.get("role") != "tts_voice":
                raise ValueError("Voiceover pool must contain generated TTS media")

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
                )
                self._resolve_original_audio(
                    job.request.mute_original_audio, [item.path for item in media_items], timeline,
                )
                # Kept on the record, not just used and dropped. `is_path_in_use` reads it to
                # refuse a rename of anything a running render is reading from, and could not
                # see an auto-planned job at all while this was a local variable.
                job.timeline = timeline

            job.warnings = list(getattr(timeline, "warnings", []) or [])
            job.progress = 0.7
            job.message = "Rendering export"
            await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

            result_path = await self.renderer.render(timeline)
            # One group covers the delivered file and its subtitle-free master, so the library
            # shows a single entry per finished video rather than doubling in length the day
            # subtitles were switched on. Recorded here rather than inferred from the filenames
            # because an operator may rename either file.
            group = f"export:{job.id}"
            subtitled = bool(getattr(timeline, "subtitles", None) and timeline.subtitles.cues)
            # Both halves point at the same cue file. 手动微调 looks it up from whichever one the
            # operator picked, so re-cutting the master can put the words back over the new
            # arrangement — recorded here rather than guessed from the filename, because either
            # file can be renamed.
            sidecar = (
                str(self.renderer.subtitle_sidecar_path(result_path)) if subtitled else ""
            )
            has_voiceover = bool(
                getattr(timeline, "voiceover_path", None)
                or (
                    getattr(timeline, "audio_bed", None)
                    and timeline.audio_bed.has_voiceover
                )
            )
            self.media.register_generated_path(
                Path(result_path),
                kind="video",
                metadata={
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
                    # Re-cutting a file that already has text painted into it drags the old
                    # subtitles along at the wrong times, so the UI has to be able to say so.
                    "has_burned_subtitles": subtitled,
                },
            )
            master_path = await self.renderer.render_master(timeline)
            if master_path:
                self.media.register_generated_path(
                    Path(master_path),
                    kind="video",
                    metadata={
                        "source": "exports", "role": "export", "job_id": job.id,
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
                        "has_burned_subtitles": False,
                    },
                )
            job.status = JobStatus.SUCCEEDED
            job.progress = 1
            job.message = "Export complete"
            job.result_path = result_path
        except Exception as exc:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job.message = "Export failed"
        finally:
            job.updated_at = utc_now()
            await self.events.publish("JOB_UPDATED", job.model_dump(mode="json"))

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

    def is_path_in_use(self, path: str) -> bool:
        """Whether a queued or running render reads from or writes to this file.

        Renaming underneath a running FFmpeg process fails the job, so the rename is
        refused instead.
        """
        for job in self._jobs.values():
            if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
                continue
            timeline = job.timeline
            if timeline is None:
                continue
            candidates = [getattr(timeline, "output_path", None),
                          getattr(timeline, "music_path", None),
                          getattr(timeline, "voiceover_path", None)]
            candidates.extend(clip.source_path for clip in getattr(timeline, "clips", []) or [])
            if any(candidate == path for candidate in candidates):
                return True
        return False

    def _next_output_name(self, title: str = "") -> str:
        """Name the export after what the operator called the edit.

        The title box was previously ignored, so every export was called 导出 regardless of
        what you typed above it.
        """
        stamp = datetime.now().astimezone().strftime("%m-%d %H-%M")
        prefix = safe_stem(title, "导出")
        exports_dir = GENERATED_DIRS["exports"]
        for index in range(0, 10000):
            suffix = "" if index == 0 else f"_{index:02d}"
            name = f"{prefix} {stamp}{suffix}.mp4"
            if name in self._allocated_output_names:
                continue
            if (exports_dir / name).exists():
                continue
            self._allocated_output_names.add(name)
            return name
        raise RuntimeError("Unable to allocate export filename")

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
        labelled, several, musical = self._what_the_footage_supports(request)
        axes: list[tuple[str, list]] = [
            ("pace", list(request.paces or EDIT_PACE_LEVELS)),
            ("contour", [
                level for level in (request.contours or EDIT_CONTOUR_LEVELS)
                if musical or level != "follow_energy"
            ]),
            ("footage_mix", list(request.footage_mixes or FOOTAGE_MIX_LEVELS) if labelled else []),
            ("emphasis", list(request.emphases or EDIT_EMPHASIS_LEVELS) if labelled else []),
            ("point_scope", list(request.point_scopes or EDIT_SCOPE_LEVELS) if labelled else []),
            ("recording_scope", list(request.recording_scopes or EDIT_SCOPE_LEVELS) if several else []),
            ("start_rotation", list(range(request.start_rotations)) if labelled else []),
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
            sidecar = read_sidecar(item.path)
            if sidecar and sidecar.get("segments"):
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
            for offset in range(index + 1, len(voiceover_ids)):
                candidate = (music_ids[index], voiceover_ids[offset])
                if candidate not in seen:
                    voiceover_ids[index], voiceover_ids[offset] = voiceover_ids[offset], voiceover_ids[index]
                    break
            seen.add((music_ids[index], voiceover_ids[index]))
