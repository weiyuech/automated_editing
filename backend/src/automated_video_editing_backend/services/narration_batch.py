"""Sequential, restart-visible batches over the existing one-video/one-voice pipeline."""

import asyncio
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from automated_video_editing_backend.core.store import read_json, write_json
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.services.composition import file_identity
from automated_video_editing_backend.services.narration_context import narration_context


class NarrationBatchService:
    def __init__(self, narration):
        self.narration = narration
        composition_directory = getattr(
            narration.compositions, "directory", GENERATED_DIRS["data"] / "compositions"
        )
        self.directory = Path(composition_directory) / "narration-batches"
        self.records = {}
        self.tasks = {}
        self.owners = {}
        if self.directory.exists():
            for path in self.directory.glob("*.json"):
                record, _ = read_json(path)
                if not isinstance(record, dict) or record.get("id") != path.stem:
                    continue
                if record.get("status") == "running":
                    record["status"] = "interrupted"
                    for item in record.get("items", []):
                        if item.get("status") in {"queued", "running"}:
                            item.update(status="interrupted", error="应用关闭中断了本次生成，可重新选择未完成的组合")
                    self._save(record)
                self.records[record["id"]] = record

    def _save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        if not write_json(self.directory / f"{record['id']}.json", record):
            raise OSError("批量旁白进度保存失败")
        self.records[record["id"]] = record

    def get(self, key):
        if key not in self.records:
            raise ValueError("批量旁白记录不存在")
        return deepcopy(self.records[key])

    def check_owner(self, key, batch_id=None):
        owner = self.owners.get(key)
        if owner is not None and owner != batch_id:
            raise ValueError("这条组合已在批量生成队列中")

    def _has_active_voice(self, item, metadata):
        """Only a ready, matching on-disk binding may consume the skip-existing option."""
        binding_id = metadata.get("narration_binding_id")
        voice_id = item.metadata.get("bound_voice_id")
        if not binding_id or not voice_id:
            return False
        voice = self.narration.compositions.media.get(voice_id)
        if not voice or voice.kind != "audio" or not Path(voice.path).is_file():
            return False
        sidecar, _ = read_json(Path(voice.path).with_suffix(".json"))
        return bool(
            isinstance(sidecar, dict)
            and sidecar.get("binding_id") == binding_id
            and sidecar.get("composition_id") == metadata.get("composition_id")
            and sidecar.get("visual_signature") == metadata.get("visual_signature")
            and sidecar.get("narration_status") == "ready"
        )

    async def start(self, request):
        # Validate the whole selection before reserving anything or spending speech quota.
        keys = [entry.composition_id for entry in request.items]
        if len(keys) != len(set(keys)):
            raise ValueError("同一组合不能在本次生成中重复选择")
        origins = set()
        entries = []
        for entry in request.items:
            key = entry.composition_id
            self.check_owner(key)
            if key in self.narration.tasks:
                raise ValueError("所选组合已有旁白正在合成，请等待完成")
            _, item, metadata = self.narration._material(key)
            context = narration_context(metadata)
            source_ids = context["source_recording_ids"]
            if len(keys) > 1 and (context["mode"] != "single_recording" or len(source_ids) != 1):
                raise ValueError("批量旁白每次只能选择同一次录制下的组合")
            origins.update(source_ids)
            if entry.visual_signature != metadata.get("visual_signature"):
                raise ValueError("组合画面已改变，请刷新并重新审阅文案")
            if not entry.narration.text.strip():
                raise ValueError("请为每条组合填写完整旁白")
            skip = request.skip_existing and self._has_active_voice(item, metadata)
            entries.append({
                "composition_id": key,
                "status": "skipped" if skip else "queued",
                "error": "",
                "video_identity": file_identity(item.path),
            })
        if len(keys) > 1 and len(origins) != 1:
            raise ValueError("批量旁白每次只能选择同一次录制下的组合")
        record = {"id": uuid4().hex, "status": "running", "items": entries}
        self._save(record)
        for entry in entries:
            if entry["status"] != "skipped":
                self.owners[entry["composition_id"]] = record["id"]
        self.tasks[record["id"]] = asyncio.create_task(self._run(record, request))
        return deepcopy(record)

    async def _run(self, record, request):
        try:
            for state, entry in zip(record["items"], request.items):
                if state["status"] == "skipped":
                    continue
                key = entry.composition_id
                try:
                    # A queued source can be deleted externally; check its identity again.
                    _, item, metadata = self.narration._material(key)
                    if (metadata.get("visual_signature") != entry.visual_signature
                            or file_identity(item.path) != state["video_identity"]):
                        raise ValueError("组合画面已改变，请重新审阅文案")
                    if request.skip_existing and self._has_active_voice(item, metadata):
                        state["status"] = "skipped"
                        self._save(record)
                        continue
                    state["status"] = "running"
                    self._save(record)
                    result = await self.narration.start(key, entry.narration, batch_id=record["id"])
                    state["attempt_id"] = result["attempt_id"]
                    self._save(record)
                    task = self.narration.tasks.get(key)
                    if task is not None:
                        await task
                    result = self.narration.compositions.get(key).get("narration", {})
                    state.update(status=result.get("status", "failed"), error=result.get("error", ""))
                except Exception as exc:
                    state.update(status="failed", error=str(exc))
                finally:
                    self.owners.pop(key, None)
                self._save(record)
            succeeded = all(item["status"] in {"ready", "skipped"} for item in record["items"])
            record["status"] = "complete" if succeeded else "needs_attention"
        except asyncio.CancelledError:
            record["status"] = "interrupted"
            for state in record["items"]:
                if state["status"] in {"queued", "running"}:
                    state.update(status="interrupted", error="批量生成已中断，可重新选择未完成的组合")
            raise
        finally:
            for entry in request.items:
                if self.owners.get(entry.composition_id) == record["id"]:
                    self.owners.pop(entry.composition_id, None)
            self.tasks.pop(record["id"], None)
            self._save(record)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Tasks cancelled before their coroutine starts do not execute its finally block.
        for record in self.records.values():
            if record.get("status") != "running":
                continue
            interrupted = False
            for item in record.get("items", []):
                if item.get("status") in {"queued", "running"}:
                    interrupted = True
                    item.update(status="interrupted", error="批量生成已中断，可重新选择未完成的组合")
            if interrupted:
                record["status"] = "interrupted"
            else:
                succeeded = all(item["status"] in {"ready", "skipped"} for item in record["items"])
                record["status"] = "complete" if succeeded else "needs_attention"
            self._save(record)
        self.tasks.clear()
        self.owners.clear()
