"""One reviewed script and one complete TTS request per composition narration."""

from __future__ import annotations

import asyncio
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
from automated_video_editing_backend.services.narration_audio import render_track, render_review
from automated_video_editing_backend.services.semantic import BgeOnnxEmbedder
from automated_video_editing_backend.core.models import TTSGenerateRequest
from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.services import subtitles
from automated_video_editing_backend.services.composition_assets import read_manifest, save_manifest
from automated_video_editing_backend.services.composition import file_identity


def narration_windows(tree: list[dict]) -> list[dict]:
    """Point and transit windows remain in tree order; a tiny shot need not start a new sentence."""

    def visit(node, notes):
        notes = node.get("notes") or notes
        if node.get("kind") in {"dwell", "transit"} or not node.get("children"):
            return [
                {
                    **{key: node[key] for key in ("id", "label", "start", "end", "duration")},
                    "kind": node.get("kind"),
                    "notes": notes,
                }
            ]
        return [window for child in node["children"] for window in visit(child, notes)]

    return [window for root in tree for window in visit(root, [])]


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

    async def allocate(self, key, request):
        record, item, metadata = self._material(key)
        windows = narration_windows(metadata["composition_tree"])
        cfg = self.llm.settings.llm_config()
        if not self.llm._configured(cfg) or not cfg.get("enabled"):
            raise ValueError("请先配置并启用大模型，也可关闭润色直接填写整篇文案")
        raw = await self.llm._chat(
            cfg,
            system=(
                "为整条组合视频写一篇连贯自然的中文旁白，不是独立小段录音。用户文案是唯一事实来源；拍摄备注只解释画面，不得将拍摄指令、猜测或未提供的信息当作口播事实。"
                "严格依照画面顺序安排内容。短画面少讲，重要点位多讲；行进画面可作为自然过渡或留白。不要每切一个小镜头就重新开头。不得为了凑时长扩写。"
                "总时长只是上限，允许留白；时间安排供用户审阅，不能保证语音合成精确到秒。若有实测反馈，针对提前讲到下一点或超时的问题精简、调整整篇衔接。"
                '仅返回 JSON {"sections":[{"node_id":"画面id","text":"本段完整口播"}]}。每个id最多一次，可略过不需要讲解的画面，所有段落连接后应是一篇通顺完整文案。'
            ),
            user=json.dumps(
                {
                    "文案": request.text,
                    "组合时长": metadata["duration_seconds"],
                    "画面与备注": windows,
                    "上一版实测": {
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
                    else None,
                },
                ensure_ascii=False,
            ),
            max_tokens=3500,
            temperature=0.2,
        )
        try:
            from automated_video_editing_backend.core.composition import NarrationBinding

            sections = [
                NarrationBinding.model_validate(entry)
                for entry in json.loads(raw)["sections"]
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
            text = "\n\n".join(s.text.strip() for s in sections)
            if len(text) > 4000:
                raise ValueError("文案过长")
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            raise ValueError("润色结果格式无效，请重试或直接编辑完整文案") from exc
        return {
            "text": text,
            "sections": [s.model_dump() for s in sections],
            "windows": windows,
            "message": "请审阅完整文案；段落时间将在整条语音合成后检查",
        }

    async def start(self, key, request):
        record, item, metadata = self._material(key)
        if key in self.tasks:
            raise ValueError("这条组合的旁白正在合成")
        if not request.text.strip():
            raise ValueError("请输入完整旁白文案")
        windows = narration_windows(metadata["composition_tree"])
        order = {window["id"]: index for index, window in enumerate(windows)}
        if any(section.node_id not in order for section in request.sections) or len(
            {s.node_id for s in request.sections}
        ) != len(request.sections):
            raise ValueError("文案与组合画面不匹配，请重新润色")
        sections = sorted(request.sections, key=lambda section: order[section.node_id])
        state = {
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
                narration_status="pending_review",
            )
            # Mark as unbound before any async processing: it must not enter a pool as ordinary TTS.
            if not write_json(source.with_suffix(".json"), voice_meta):
                raise OSError("候选旁白信息无法保存")
            duration = await asyncio.to_thread(self.compositions.renderer.probe_duration, str(raw))
            if duration is None or duration <= 0:
                raise ValueError("无法测量整条语音时长")
            state.update(
                source_audio_path=str(raw),
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
            message="正在匹配内容并生成同步试听",
        )
        source = state["source_audio_path"]
        if not Path(source).is_file():
            raise ValueError("原始语音缓存已清理，请重新合成")
        if (
            file_identity(item.path) != state["video_identity"]
            or metadata["visual_signature"] != state["visual_signature"]
        ):
            raise ValueError("组合视频已改变，请重新检查")
        text, duration, windows = state["text"], state["source_seconds"], state["windows"]
        mappings, evidence = await asyncio.to_thread(
            map_script, text, state["sections"], windows, self.embedder
        )
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
            warnings.append("未取得完整可靠的逐词时间，无法精确调整局部对应；请同步试听确认。")
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
            warnings.append("部分段落尚未对齐，请在同步试听后决定是否采用，或调整文案。")
        cues = (
            subtitles.cues_from_words(words, subtitles.SubtitleStyle(), 1280, 720)
            if reliable
            else []
        )
        voice_meta = dict(state["voice_metadata"])
        voice_meta.update(
            text=text,
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
        review_id = uuid4().hex
        preview = GENERATED_DIRS["previews"] / f"narration-{review_id}.mp4"
        state["building_preview_path"] = str(preview)
        await render_review(renderer, item.path, stage, preview, metadata["duration_seconds"])
        if file_identity(item.path) != state["video_identity"]:
            raise ValueError("试听生成期间组合视频已改变，请重新检查")
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
            status="pending_review",
            progress=1,
            audio_path=str(candidate),
            actual_seconds=adjusted,
            media_id=media.id,
            preview_path=str(preview),
            review_id=review_id,
            audio_identity=file_identity(str(candidate)),
            preview_identity=file_identity(str(preview)),
            metadata_identity=file_identity(str(candidate.with_suffix(".json"))),
            checks=checks,
            playback_blocks=blocks,
            local_alignment=local,
            message="同步试听已就绪，确认后才替换组合旁白",
        )

    async def adjust(self, key, request):
        record, item, metadata = self._material(key)
        state = self._pending(record, request.attempt_id)
        if state["status"] not in {"pending_review", "needs_revision", "failed"} or not state.get(
            "source_audio_path"
        ):
            raise ValueError("只有未确认的候选旁白可重新调整")
        state.update(status="running", progress=0.1, error="", message="正在复用现有语音调整节奏")
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

    def confirm(self, key, attempt_id, review_id):
        record, item, metadata = self._material(key)
        state = self._pending(record, attempt_id)
        if state.get("status") == "ready" and metadata.get("narration_binding_id") == attempt_id:
            return state
        if state.get("status") != "pending_review" or state.get("review_id") != review_id:
            raise ValueError("请先试听当前版本，再确认应用")
        try:
            unchanged = (
                file_identity(item.path) == state["video_identity"]
                and metadata["visual_signature"] == state["visual_signature"]
                and file_identity(state["audio_path"]) == state["audio_identity"]
                and file_identity(state["preview_path"]) == state["preview_identity"]
                and file_identity(str(Path(state["audio_path"]).with_suffix(".json")))
                == state["metadata_identity"]
            )
        except OSError:
            unchanged = False
        if not unchanged:
            raise ValueError("试听内容已改变或缺失，请重新生成预览")
        # The composition manifest is the sole commit point. No earlier attempt replaces it.
        metadata["narration_binding_id"] = attempt_id
        save_manifest(item.path, metadata)
        state.update(status="ready", message="已确认应用；组合与完整旁白同步入池和选用")
        self.compositions.save(record)
        try:
            self.compositions.media.list_items()
            self.compositions.media.media_pool()
        except (OSError, ValueError, RuntimeError) as exc:
            state.setdefault("warnings", []).append(f"旁白已绑定，媒体池暂未刷新：{exc}")
            self.compositions.save(record)
        return state

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
