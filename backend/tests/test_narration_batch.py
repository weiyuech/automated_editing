import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from automated_video_editing_backend.core.composition import MappedNarrationRequest
from automated_video_editing_backend.core.paths import GENERATED_DIRS
from automated_video_editing_backend.core.narration_batch import BatchNarrationRequest
from automated_video_editing_backend.core.store import write_json
from automated_video_editing_backend.services.composition_assets import read_manifest
from automated_video_editing_backend.services.mapped_narration import MappedNarrationService
from automated_video_editing_backend.services.narration_batch import NarrationBatchService
from test_controlled_composition import FakeLLM, FakeTTS, saved_material
from test_controlled_composition import studio as studio


def request(keys=("one", "two"), skip_existing=True):
    return BatchNarrationRequest(items=[{
        "composition_id": key, "visual_signature": f"signature-{key}",
        "narration": {"text": "完整旁白", "direct_narration": True},
    } for key in keys], skip_existing=skip_existing)


class FakeNarration:
    def __init__(self, tmp_path):
        self.tasks, self.records, self.items, self.metadata, self.voices = {}, {}, {}, {}, {}
        self.calls = []
        self.failures = set()
        self.release = None
        self.started = asyncio.Event()
        for key in ("one", "two"):
            path = tmp_path / f"{key}.mp4"
            path.write_bytes(b"video")
            self.items[key] = SimpleNamespace(path=str(path), metadata={})
            self.records[key] = {"id": key}
            self.metadata[key] = {
                "composition_id": key, "visual_signature": f"signature-{key}",
                "duration_seconds": 10, "source_recording_ids": ["capture:original"],
                "composition_tree": [],
            }
        self.compositions = SimpleNamespace(
            directory=tmp_path / "compositions", get=lambda key: self.records[key],
            media=SimpleNamespace(get=lambda key: self.voices.get(key)),
        )
        self.batches = NarrationBatchService(self)

    def _material(self, key):
        return self.records[key], self.items[key], deepcopy(self.metadata[key])

    async def start(self, key, narration, *, batch_id=None):
        self.batches.check_owner(key, batch_id)
        assert key not in self.tasks
        self.calls.append(key)
        state = {"attempt_id": f"attempt-{key}", "status": "running"}
        self.records[key]["narration"] = state

        async def generate():
            try:
                self.started.set()
                if self.release is not None:
                    await self.release.wait()
                state.update(
                    status="failed" if key in self.failures else "ready",
                    error="provider rejected" if key in self.failures else "",
                )
            finally:
                self.tasks.pop(key, None)

        self.tasks[key] = asyncio.create_task(generate())
        return state

    def bind_voice(self, key, *, status="ready", signature=None):
        voice_path = Path(self.items[key].path).with_suffix(".wav")
        voice_path.write_bytes(b"voice")
        sidecar = {
            "binding_id": f"binding-{key}", "composition_id": key,
            "visual_signature": signature or f"signature-{key}", "narration_status": status,
        }
        write_json(voice_path.with_suffix(".json"), sidecar)
        self.metadata[key]["narration_binding_id"] = sidecar["binding_id"]
        self.items[key].metadata["bound_voice_id"] = f"voice-{key}"
        self.voices[f"voice-{key}"] = SimpleNamespace(kind="audio", path=str(voice_path))


async def run_batch(service, payload):
    created = await service.start(payload)
    await asyncio.wait_for(service.tasks[created["id"]], 5)
    return service.get(created["id"])


@pytest.mark.asyncio
async def test_batch_rejects_duplicates_and_different_original_recordings_before_spending(tmp_path):
    narration = FakeNarration(tmp_path)
    with pytest.raises(ValueError, match="重复"):
        await narration.batches.start(request(("one", "one")))
    narration.metadata["two"]["source_recording_ids"] = ["capture:another"]
    with pytest.raises(ValueError, match="同一次录制"):
        await narration.batches.start(request())
    assert narration.calls == []
    assert not narration.batches.owners


@pytest.mark.asyncio
async def test_source_identity_falls_back_to_tree_and_single_cross_recording_is_allowed(tmp_path):
    narration = FakeNarration(tmp_path)
    for metadata in narration.metadata.values():
        metadata.pop("source_recording_ids")
        metadata["composition_tree"] = [{
            "id": "root", "kind": "recording", "label": "原始录制", "start": 0,
            "end": 10, "duration": 10, "source_recording_ids": ["capture:original"],
        }]
    result = await run_batch(narration.batches, request())
    assert result["status"] == "complete"
    narration.metadata["one"]["source_recording_ids"] = ["capture:a", "capture:b"]
    result = await run_batch(narration.batches, request(("one",)))
    assert result["status"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, "missing", "signature", "pending"])
async def test_skip_checks_active_on_disk_binding_not_only_cached_voice_id(tmp_path, invalid):
    narration = FakeNarration(tmp_path)
    narration.bind_voice("one", status="generating" if invalid == "pending" else "ready",
                         signature="old" if invalid == "signature" else None)
    if invalid == "missing":
        Path(narration.voices["voice-one"].path).unlink()
    result = await run_batch(narration.batches, request())
    assert result["items"][0]["status"] == ("skipped" if invalid is None else "ready")
    assert narration.calls == (["two"] if invalid is None else ["one", "two"])


@pytest.mark.asyncio
async def test_changed_queued_file_fails_without_spending_and_later_item_continues(tmp_path):
    narration = FakeNarration(tmp_path)
    batch = await narration.batches.start(request())
    Path(narration.items["one"].path).write_bytes(b"video replaced externally")
    await narration.batches.tasks[batch["id"]]
    result = narration.batches.get(batch["id"])
    assert result["status"] == "needs_attention"
    assert result["items"][0]["status"] == "failed"
    assert result["items"][1]["status"] == "ready"
    assert narration.calls == ["two"]


@pytest.mark.asyncio
async def test_reserved_combinations_cannot_run_twice_and_per_item_failure_continues(tmp_path):
    narration = FakeNarration(tmp_path)
    narration.release = asyncio.Event()
    narration.failures.add("one")
    batch = await narration.batches.start(request())
    await narration.started.wait()
    assert narration.calls == ["one"]
    with pytest.raises(ValueError, match="队列"):
        await narration.batches.start(request(("two",)))
    with pytest.raises(ValueError, match="队列"):
        await narration.start("two", MappedNarrationRequest(text="另一个请求"))
    narration.release.set()
    await narration.batches.tasks[batch["id"]]
    result = narration.batches.get(batch["id"])
    assert [item["status"] for item in result["items"]] == ["failed", "ready"]
    assert not narration.batches.owners


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_for_start", [False, True])
async def test_close_releases_reservations_even_before_coroutine_starts(tmp_path, wait_for_start):
    narration = FakeNarration(tmp_path)
    narration.release = asyncio.Event()
    batch = await narration.batches.start(request())
    if wait_for_start:
        await narration.started.wait()
    await narration.batches.close()
    assert not narration.batches.owners
    assert not narration.batches.tasks
    assert not narration.tasks
    result = narration.batches.get(batch["id"])
    assert result["status"] == "interrupted"
    assert all(item["status"] == "interrupted" for item in result["items"])
    restored = NarrationBatchService(narration)
    assert restored.get(batch["id"]) == result


def test_restart_marks_old_running_batch_interrupted_and_preserves_completed_items(tmp_path):
    narration = FakeNarration(tmp_path)
    record = {"id": "old", "status": "running", "items": [
        {"composition_id": "one", "status": "ready"},
        {"composition_id": "two", "status": "queued"},
    ]}
    narration.batches._save(record)
    restored = NarrationBatchService(narration)
    assert restored.get("old")["status"] == "interrupted"
    assert [item["status"] for item in restored.get("old")["items"]] == ["ready", "interrupted"]
    assert not restored.owners


def test_prompt_only_service_fixture_does_not_require_composition_directory(tmp_path, monkeypatch):
    monkeypatch.setitem(GENERATED_DIRS, "data", tmp_path)
    narration = MappedNarrationService(SimpleNamespace(), None, None, embedder=object())
    assert narration.batches.directory == tmp_path / "compositions/narration-batches"
    assert not narration.batches.directory.exists()


@pytest.mark.asyncio
async def test_real_batch_failed_replacement_keeps_previous_binding_and_valid_skip(studio, tmp_path):
    record, material = await saved_material(studio, tmp_path)
    directory = tmp_path / "data/tts"
    directory.mkdir(exist_ok=True)
    tts = FakeTTS(directory, studio.media, [0.4])
    narration = MappedNarrationService(studio, FakeLLM(), tts)
    payload = BatchNarrationRequest(items=[{
        "composition_id": record["id"], "visual_signature": record["visual_signature"],
        "narration": {"text": "一条完整旁白", "direct_narration": True},
    }])
    result = await run_batch(narration.batches, payload)
    assert result["status"] == "complete", result
    original_binding = read_manifest(material.path)["narration_binding_id"]
    original_voice = studio.material(record["id"])[1].metadata["bound_voice_id"]
    result = await run_batch(narration.batches, payload)
    assert result["items"][0]["status"] == "skipped"
    assert len(tts.calls) == 1

    async def failed_provider(*args):
        raise ValueError("service unavailable")

    tts.synthesize = failed_provider
    result = await run_batch(narration.batches, payload.model_copy(update={"skip_existing": False}))
    assert result["items"][0]["status"] == "failed"
    assert read_manifest(material.path)["narration_binding_id"] == original_binding
    assert studio.material(record["id"])[1].metadata["bound_voice_id"] == original_voice
    assert Path(studio.media.get(original_voice).path).is_file()
    persisted = json.loads((narration.batches.directory / f"{result['id']}.json").read_text())
    assert persisted["status"] == "needs_attention"
