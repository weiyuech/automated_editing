"""One reviewed script and one complete TTS request per composition narration."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import shutil
from pathlib import Path
from uuid import uuid4

from automated_video_editing_backend.core.composition import NarrationBinding
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.services.narration_alignment import (
    map_script,
    reliable_words,
    playback_plan,
    retime_words,
)
from automated_video_editing_backend.services.narration_audio import render_track
from automated_video_editing_backend.services.semantic import BgeOnnxEmbedder
from automated_video_editing_backend.services.narration_prompts import composition_prompt
from automated_video_editing_backend.services.narration_context import (
    narration_context,
    narration_windows as narration_windows,
)
from automated_video_editing_backend.core.models import TTSGenerateRequest
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services import subtitles
from automated_video_editing_backend.services.composition_assets import read_manifest, save_manifest
from automated_video_editing_backend.services.composition import file_identity


def _speech_chars(text):
    return "".join(char.lower() for char in text if char.isalnum())


def check_alignment(windows, sections, words):
    """Locate reviewed paragraphs on provider word clocks; never infer clocks from text length."""
    characters, clocks = [], []
    for text, start, end in subtitles.normalise_words(words):
        for char in _speech_chars(text):
            characters.append(char)
            clocks.append((start, end))
    spoken = "".join(characters)
    by_id = {node["id"]: node for node in windows}
    checks, cursor = [], 0
    for section in sections:
        node = by_id.get(section.node_id)
        if not node:
            raise ValueError("文案对应的画面已改变，请重新润色")
        text = _speech_chars(section.text)
        index = spoken.find(text, cursor) if text else -1
        check = {
            "node_id": node["id"],
            "label": node["label"],
            "planned_start": node["start"],
            "planned_end": node["end"],
            "text": section.text,
            "status": "unverified",
        }
        if index >= 0:
            start, end = clocks[index][0], clocks[index + len(text) - 1][1]
            cursor = index + len(text)
            check.update(
                actual_start=start,
                actual_end=end,
                start_difference=round(start - node["start"], 3),
                overflow_seconds=max(0, end - node["end"]),
                status="aligned"
                if start >= node["start"] - 0.5 and end <= node["end"] + 0.5
                else "review",
            )
        checks.append(check)
    return checks


class MappedNarrationService:
    def __init__(self, compositions, llm, tts, embedder=None):
        self.compositions, self.llm, self.tts = compositions, llm, tts
        self.tasks = {}
        self.embedder = embedder or BgeOnnxEmbedder()

    def _material(self, key):
        record, item = self.compositions.material(key)
        metadata = read_manifest(item.path)
        if not metadata or not Path(item.path).is_file():
            raise ValueError("组合文件或来源记录已失效，请重新保存组合")
        return record, item, metadata

    def prompt(self, key, request):
        """Read-only inspection; does not call an LLM or spend TTS quota."""
        record, item, metadata = self._material(key)
        context = narration_context(metadata)
        feedback = (
            {
                field: record.get("narration", {}).get(field)
                for field in (
                    "text",
                    "source_seconds",
                    "actual_seconds",
                    "available_seconds",
                    "checks",
                    "mappings",
                    "warnings",
                    "error",
                    "playback_blocks",
                )
            }
            if request.measured_feedback
            else None
        )
        return {
            **composition_prompt(context, request, feedback=feedback),
            "title": metadata["title"],
        }

    async def allocate(self, key, request):
        prompt = self.prompt(key, request)
        windows = prompt["windows"]
        if (
            prompt["mode"] == "single_recording"
            and not prompt["capture_notes"]
            and not request.instructions.strip()
        ):
            raise ValueError("这份组合没有拍摄备注，请填写本次补充要求后再润色")
        cfg = self.llm.settings.llm_config()
        if not self.llm._configured(cfg) or not cfg.get("enabled"):
            raise ValueError("请先配置并启用大模型，也可关闭润色直接填写整篇文案")
        raw = await self.llm._chat(
            cfg,
            system=prompt["system"],
            user=prompt["user"],
            max_tokens=3500,
            temperature=0.2,
        )
        if prompt["mode"] != "single_recording":
            text = raw.strip()
            if not text or len(text) > 4000:
                raise ValueError("润色结果为空或过长，请修改文案后重试")
            return {
                "text": text,
                "sections": [],
                "windows": [],
                "prompt": prompt,
                "message": "请审阅整篇文案；合成后按组合总时长检查",
                "mode": prompt["mode"],
            }
        try:
            from automated_video_editing_backend.core.composition import NarrationBinding

            payload = json.loads(raw)
            sections = [
                NarrationBinding.model_validate(entry)
                for entry in payload["sections"]
                if entry.get("text", "").strip()
            ]
            order = {window["id"]: i for i, window in enumerate(windows)}
            if (
                not sections
                or len({s.node_id for s in sections}) != len(sections)
                or any(s.node_id not in order for s in sections)
            ):
                raise ValueError("画面节点不正确")
            sections.sort(key=lambda section: order[section.node_id])
            assignments = self._validate_assignments(payload.get("note_assignments", []), windows)
            general_notes = payload.get("general_notes", [])
            if (
                not isinstance(general_notes, list)
                or len(general_notes) > 100
                or any(not isinstance(n, str) for n in general_notes)
            ):
                raise ValueError("整体备注格式错误")
            text = "\n\n".join(s.text.strip() for s in sections)
            if len(text) > 4000:
                raise ValueError("文案过长")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("润色结果格式无效，请重试或直接编辑完整文案") from exc
        return {
            "text": text,
            "sections": [s.model_dump() for s in sections],
            "note_assignments": assignments,
            "general_notes": general_notes,
            "windows": windows,
            "message": "请审阅完整文案；段落时间将在整条语音合成后检查",
            "prompt": prompt,
        }

    @staticmethod
    def _validate_assignments(entries, windows):
        entries = [NarrationBinding.model_validate(entry).model_dump() for entry in entries]
        ids = {node["id"] for node in windows}
        if (
            len(entries) > 100
            or len({e["node_id"] for e in entries}) != len(entries)
            or any(e["node_id"] not in ids for e in entries)
        ):
            raise ValueError("备注对应的画面节点无效")
        return entries

    async def start(self, key, request):
        record, item, metadata = self._material(key)
        if key in self.tasks:
            raise ValueError("这条组合的旁白正在合成")
        if not request.text.strip():
            raise ValueError("请输入完整旁白文案")
        context = narration_context(metadata)
        windows = context["windows"]
        order = {window["id"]: index for index, window in enumerate(windows)}
        requested_sections = request.sections if context["mode"] == "single_recording" else []
        if any(section.node_id not in order for section in requested_sections) or len(
            {s.node_id for s in requested_sections}
        ) != len(requested_sections):
            raise ValueError("文案与组合画面不匹配，请重新润色")
        sections = sorted(requested_sections, key=lambda section: order[section.node_id])
        assignments = (
            self._validate_assignments(request.note_assignments, windows)
            if context["mode"] == "single_recording" and not request.direct_narration
            else []
        )
        state = {
            "direct_narration": request.direct_narration,
            "note_assignments": assignments,
            "general_notes": request.general_notes
            if context["mode"] == "single_recording" and not request.direct_narration
            else [],
            "expected_binding_id": metadata.get("narration_binding_id"),
            "mode": context["mode"],
            "source_count": context["source_count"],
            "attempt_id": uuid4().hex,
            "status": "running",
            "progress": 0,
            "error": "",
            "message": "正在完整合成一条旁白",
            "text": request.text,
            "checks": [],
            "available_seconds": metadata["duration_seconds"],
        }
        record["narration"] = state
        self.compositions.save(record)
        self.tasks[key] = asyncio.create_task(
            self._run(record, item, metadata, request, sections, windows, state)
        )
        return state

    async def _run(self, record, item, metadata, request, sections, windows, state):
        try:
            state["video_identity"] = file_identity(item.path)
            state["visual_signature"] = metadata["visual_signature"]
            state["sections"] = [s.model_dump() for s in sections]
            state["windows"] = windows
            # Exactly one paid request. Audio-only adjustments reuse the private source copy.
            result = await self.tts.synthesize(
                TTSGenerateRequest(title=f"{metadata['title']} 旁白", text=request.text),
                request.text,
            )
            source = Path(result.media_item.path)
            folder = GENERATED_DIRS["cache"] / "narration" / state["attempt_id"]
            folder.mkdir(parents=True, exist_ok=True)
            raw = folder / ("source" + source.suffix)
            shutil.copy2(source, raw)
            voice_meta, _ = read_json(source.with_suffix(".json"))
            voice_meta = voice_meta if isinstance(voice_meta, dict) else {}
            voice_meta.update(
                binding_id=state["attempt_id"],
                composition_id=record["id"],
                visual_signature=metadata["visual_signature"],
                narration_status="generating",
            )
            # Mark as unbound before any async processing: it must not enter a pool as ordinary TTS.
            if not write_json(source.with_suffix(".json"), voice_meta):
                raise OSError("候选旁白信息无法保存")
            duration = await asyncio.to_thread(self.compositions.renderer.probe_duration, str(raw))
            if duration is None or duration <= 0:
                raise ValueError("无法测量整条语音时长")
            state.update(
                source_audio_path=str(raw),
                source_identity=file_identity(str(raw)),
                source_words=result.words,
                source_timing_quality=result.asset.timing_quality,
                source_seconds=duration,
                voice_metadata=voice_meta,
                candidate_path=str(source.with_suffix(".wav")),
                original_asset_path=str(source),
                audio_path=str(source),
                actual_seconds=duration,
            )
            await self._prepare(
                record, item, metadata, state, request.playback_rate, request.auto_tempo
            )
        except Exception as exc:
            if state.get("status") == "ready":
                state.setdefault("warnings", []).append(str(exc))
            else:
                state.update(status="failed", error=str(exc))
        except asyncio.CancelledError:
            state.update(status="failed", error="整条语音合成被中断，请检查后重试")
            raise
        finally:
            try:
                self.compositions.save(record)
            finally:
                self.tasks.pop(record["id"], None)

    async def _prepare(self, record, item, metadata, state, rate, auto_tempo):
        state.update(
            status="running",
            error="",
            preview_path=None,
            review_id=None,
            playback_blocks=[],
            progress=0.65,
            playback_rate=rate,
            auto_tempo=auto_tempo,
            message="正在检查时长并调整语音",
        )
        source = state["source_audio_path"]
        if not Path(source).is_file():
            raise ValueError("原始语音缓存已清理，请重新合成")
        if state.get("source_identity") and file_identity(source) != state["source_identity"]:
            raise ValueError("原始语音缓存已改变，请重新合成")
        if (
            file_identity(item.path) != state["video_identity"]
            or metadata["visual_signature"] != state["visual_signature"]
        ):
            raise ValueError("组合视频已改变，请重新检查")
        context = narration_context(metadata)
        single = context["mode"] == "single_recording" and not state.get("direct_narration")
        state.update(
            mode=context["mode"], source_count=context["source_count"], windows=context["windows"]
        )
        text, duration, windows = state["text"], state["source_seconds"], context["windows"]
        if single:
            note_map = {
                entry["node_id"]: entry["text"] for entry in state.get("note_assignments", [])
            }
            windows = [
                {**window, "notes": [note_map[window["id"]]] if window["id"] in note_map else []}
                for window in windows
            ]
            state["windows"] = windows
            mappings, evidence = await asyncio.to_thread(
                map_script, text, state["sections"], windows, self.embedder
            )
        else:
            # Cross-recording mode must not invoke BAAI, even with stale/custom draft sections.
            mappings, evidence = [], "not_applicable"
            state["sections"] = []
        reliable = reliable_words(
            text, state["source_words"], state["source_timing_quality"], duration
        )
        references = [
            NarrationBinding(node_id=m["node_id"], text=m["text"]) for m in mappings if m["node_id"]
        ]
        checks = check_alignment(windows, references, state["source_words"] if reliable else [])
        complete = (
            reliable
            and bool(mappings)
            and all(m["node_id"] and not m.get("warning") for m in mappings)
        )
        warnings = []
        if evidence == "unavailable":
            warnings.append(
                "本地 BAAI 暂不可用，已保留明确的段落绑定；修改或未匹配内容请试听核对。"
            )
        if not reliable:
            warnings.append(
                "未取得可靠逐词时间，已按整篇时长处理，无法保证逐点精确对应。"
                if single
                else "未取得可靠逐词时间，字幕仅按整段时长处理。"
            )
        if any(not m["node_id"] or m.get("warning") for m in mappings):
            warnings.append("部分文案对应关系不确定，本次保留连续语音顺序，请核对画面。")
        state.update(
            mappings=mappings, semantic_evidence=evidence, checks=checks, warnings=warnings
        )
        try:
            blocks, local = playback_plan(
                duration,
                metadata["duration_seconds"],
                checks,
                complete=complete,
                rate=rate,
                auto_tempo=auto_tempo,
            )
        except ValueError as exc:
            state.update(
                status="needs_revision",
                progress=1,
                actual_seconds=duration,
                audio_path=source,
                message="语音已保留，尚未应用",
                error=str(exc),
            )
            return
        words = retime_words(state["source_words"], blocks) if reliable else []
        folder = Path(source).parent
        stage = folder / "adjusted.wav"
        renderer = self.compositions.renderer
        await render_track(renderer, source, stage, blocks)
        adjusted = await asyncio.to_thread(renderer.probe_duration, str(stage))
        if adjusted is None or adjusted > metadata["duration_seconds"] + 0.02:
            raise ValueError("调整后的实测语音仍超出画面，请精简文案")
        checks = check_alignment(windows, references, words)
        if any(c["status"] != "aligned" for c in checks):
            warnings.append("部分段落未精确对齐，可在剪辑台预览中核对。")
        cues = (
            subtitles.cues_from_words(words, subtitles.SubtitleStyle(), 1280, 720)
            if reliable
            else []
        )
        voice_meta = dict(state["voice_metadata"])
        voice_meta.update(
            text=text,
            binding_id=state["attempt_id"],
            narration_status="ready",
            note_assignments=state.get("note_assignments", []),
            general_notes=state.get("general_notes", []),
            duration_ms=round(adjusted * 1000),
            words=words,
            phonemes=[],
            whole_audio=True,
            timing_quality="exact" if reliable else "estimated",
            playback_blocks=blocks,
            mapping_checks=checks,
            semantic_mappings=mappings,
            mapped_cues=[
                {"text": c.text, "start": c.start, "end": min(c.end, adjusted)} for c in cues
            ]
            or [{"text": text, "start": 0, "end": adjusted}],
        )
        if file_identity(item.path) != state["video_identity"]:
            raise ValueError("生成期间组合视频已改变，请重新生成")
        candidate = Path(state["candidate_path"])
        if not write_json(candidate.with_suffix(".json"), voice_meta):
            raise OSError("旁白时间信息无法保存")
        stage.replace(candidate)
        original = Path(state["original_asset_path"])
        if original != candidate:
            original.unlink(missing_ok=True)
        media = self.compositions.media.register_generated_path(
            candidate, kind="audio", metadata={"source": "data/tts", "role": "tts_voice"}
        )
        state.update(
            progress=1,
            audio_path=str(candidate),
            actual_seconds=adjusted,
            media_id=media.id,
            audio_identity=file_identity(str(candidate)),
            metadata_identity=file_identity(str(candidate.with_suffix(".json"))),
            checks=checks,
            playback_blocks=blocks,
            local_alignment=local,
        )
        self._publish(record, item, state)

    def _publish(self, record, item, state):
        """Publish only a complete immutable audio+clock pair; the video manifest commits it."""
        metadata = read_manifest(item.path)
        if (
            file_identity(item.path) != state["video_identity"]
            or metadata.get("visual_signature") != state["visual_signature"]
            or metadata.get("narration_binding_id") != state.get("expected_binding_id")
            or file_identity(state["audio_path"]) != state["audio_identity"]
            or file_identity(str(Path(state["audio_path"]).with_suffix(".json")))
            != state["metadata_identity"]
        ):
            raise ValueError("组合或旁白内容已改变，未替换原绑定，请重新生成")
        metadata["narration_binding_id"] = state["attempt_id"]
        save_manifest(item.path, metadata)
        state.update(status="ready", message="已生成并绑定此组合")
        # A cache refresh failure cannot roll back or misreport a successful manifest commit.
        try:
            self.compositions.save(record)
            self.compositions.media.list_items()
            self.compositions.media.media_pool()
        except (OSError, ValueError, RuntimeError) as exc:
            state.setdefault("warnings", []).append(f"旁白已绑定，列表暂未刷新：{exc}")

    async def adjust(self, key, request):
        record, item, metadata = self._material(key)
        state = self._pending(record, request.attempt_id)
        if state["status"] not in {
            "ready",
            "pending_review",
            "needs_revision",
            "failed",
        } or not state.get("source_audio_path"):
            raise ValueError("没有可复用的原始语音，请先生成旁白")
        # Never overwrite the audio/sidecar currently used by editing or an existing export.
        state = deepcopy(state)
        state["attempt_id"] = uuid4().hex
        candidate = Path(state["candidate_path"]).with_name(f"narration-{state['attempt_id']}.wav")
        state.update(
            candidate_path=str(candidate),
            original_asset_path=str(candidate),
            expected_binding_id=metadata.get("narration_binding_id"),
            status="running",
            progress=0.1,
            error="",
            message="正在复用现有语音调整节奏",
        )
        record["narration"] = state
        self.compositions.save(record)
        self.tasks[key] = asyncio.create_task(
            self._adjust_run(record, item, metadata, state, request)
        )
        return state

    async def _adjust_run(self, record, item, metadata, state, request):
        try:
            await self._prepare(
                record, item, metadata, state, request.playback_rate, request.auto_tempo
            )
        except Exception as exc:
            if state.get("status") == "ready":
                state.setdefault("warnings", []).append(str(exc))
            else:
                state.update(status="failed", error=str(exc))
        except asyncio.CancelledError:
            state.update(status="failed", error="语音调整被中断，可复用原音频重试")
            raise
        finally:
            try:
                self.compositions.save(record)
            finally:
                self.tasks.pop(record["id"], None)

    def _pending(self, record, attempt_id):
        state = record.get("narration") or {}
        if record["id"] in self.tasks or state.get("attempt_id") != attempt_id:
            raise ValueError("候选旁白已更新或仍在处理，请刷新后再操作")
        return state

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
