"""Exercise actual concatenation clocks and source identity, without a robot or paid provider."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from automated_video_editing_backend.core import paths
from automated_video_editing_backend.core.composition import (
    CompositionRequest,
    MappedNarrationRequest,
)
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import CaptureSelection
from automated_video_editing_backend.services.capture import sidecar_path
from automated_video_editing_backend.services.composition import CompositionService
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.mapped_narration import (
    MappedNarrationService,
    check_alignment,
    narration_windows,
)
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.recording_segments import (
    build_timeline,
    selected_ranges,
)
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.timeline import EditPlanner


@pytest.fixture
def studio(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "APP_ROOT", tmp_path)
    for area in paths.GENERATED_DIRS:
        folder = tmp_path / area
        folder.mkdir()
        monkeypatch.setitem(paths.GENERATED_DIRS, area, folder)
    media = MediaService(path=tmp_path / "data/library.json")
    jobs = JobService(EventHub(), media, None, EditPlanner(), RenderService())
    service = CompositionService(jobs, directory=tmp_path / "data/combinations")
    jobs.compositions = service
    media.captures.external_path_in_use = jobs.is_path_in_use
    return service


def video(path, color, duration=2, size="64x64"):
    subprocess.run(
        [
            RenderService().ffmpeg_binary(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r=30:d={duration}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    return path


def evidence():
    return {
        "capture_session_id": "capture",
        "title": "A点拍摄",
        "segments": [
            {
                "index": 0,
                "path_name": "route",
                "goal_id": 1,
                "status": "arrived",
                "shots": [
                    {
                        "id": "origin-left",
                        "label": "原点 → 左",
                        "kind": "shot",
                        "start": 0.3,
                        "end": 0.8,
                        "status": "complete",
                    },
                    {
                        "id": "left-right",
                        "label": "左 → 右",
                        "kind": "shot",
                        "start": 0.8,
                        "end": 1.2,
                        "status": "complete",
                    },
                    {
                        "id": "right-origin",
                        "label": "右 → 原点",
                        "kind": "shot",
                        "start": 1.2,
                        "end": 1.7,
                        "status": "complete",
                    },
                ],
            }
        ],
        "recording_events": [
            {"visit_index": 0, "type": "goal_write", "seconds": 0.1},
            {"visit_index": 0, "type": "goal_done", "seconds": 0.3},
        ],
        "notes": ["A点是新品区，仅供画面定位"],
    }


async def ready(service, request):
    created = await service.create(request)
    await asyncio.wait_for(service.tasks[created["id"]], 30)
    record = service.get(created["id"])
    assert record["status"] == "ready", record.get("error")
    return record


def test_nested_union_never_duplicates_whole_or_parent_and_child():
    segments = build_timeline(evidence(), 2)
    dwell = next(n for n in segments if n["kind"] == "dwell")
    assert len(dwell["children"]) == 4  # three shots, then recording tail
    group = {"segments": segments, "duration": 2}
    child = dwell["children"][0]
    assert selected_ranges(group, {"segment_ids": [dwell["id"], child["id"]]}) == [(0.3, 2)]
    assert selected_ranges(group, {"include_full": True, "segment_ids": [child["id"]]}) == [(0, 2)]
    assert selected_ranges(group, {"segment_ids": [child["id"], dwell["children"][2]["id"]]}) == [
        (0.3, 0.8),
        (1.2, 1.7),
    ]


@pytest.mark.asyncio
async def test_real_preview_keeps_roots_nested_and_partial_root_is_one_input(studio, tmp_path):
    path = video(tmp_path / "red.mp4", "red")
    sidecar_path(path).write_text(json.dumps(evidence()), encoding="utf8")
    root = studio.media.import_path(str(path))
    blue = studio.media.import_path(str(video(tmp_path / "blue.mp4", "blue", 0.5)))
    studio.media.list_items()
    group = await studio.media.captures.prepare("capture")
    studio.media.list_items()
    dwell = next(n for n in group["segments"] if n["kind"] == "dwell")
    choice = CaptureSelection(
        capture_id="capture", segment_ids=[dwell["children"][0]["id"], dwell["children"][2]["id"]]
    )
    record = await ready(
        studio,
        CompositionRequest(
            media_ids=[root.id, blue.id], capture_selections=[choice], subtitles=False
        ),
    )
    assert record["duration"] == pytest.approx(1.5)
    assert record["measured_duration"] == pytest.approx(1.5, abs=1 / 30)
    assert len(record["timeline"]["clips"]) == 2
    assert len(record["tree"]) == 2
    assert len(record["tree"][0]["children"]) == 1
    assert len(record["tree"][0]["children"][0]["children"]) == 2
    assert record["tree"][0]["children"][0]["children"][1]["start"] == pytest.approx(0.5)
    assert studio.is_path_in_use(record["timeline"]["clips"][0]["source_path"])
    # The encoded picture really follows root order, not a planner's score or random deal.
    for position, expected in ((0.2, (255, 0, 0)), (1.2, (0, 0, 255))):
        pixels = subprocess.check_output(
            [
                studio.renderer.ffmpeg_binary(),
                "-v",
                "error",
                "-ss",
                str(position),
                "-i",
                record["preview_path"],
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "pipe:1",
            ]
        )
        assert len(pixels) == 3
        assert max(range(3), key=lambda i: pixels[i]) == max(range(3), key=lambda i: expected[i])
    with pytest.raises(ValueError, match="预览"):
        await studio.confirm(record["id"], "wrong")
    # Persisted partial inputs still render after reconstructing the review service.
    restored = CompositionService(studio.jobs, directory=studio.directory)
    studio.jobs.compositions = restored
    job = await restored.confirm(record["id"], record["signature"])
    for _ in range(200):
        if job.status.value in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.025)
    assert job.status.value == "succeeded", job.error
    assert studio.renderer.probe_duration(job.result_path) == pytest.approx(1.5, abs=1 / 30)
    Path(blue.path).touch()
    with pytest.raises(ValueError, match="改变"):
        await studio.confirm(record["id"], record["signature"])


@pytest.mark.asyncio
async def test_whole_and_child_selection_is_original_once_and_effects_in_total(studio, tmp_path):
    path = video(tmp_path / "source.mp4", "red")
    sidecar_path(path).write_text(json.dumps(evidence()), encoding="utf8")
    root = studio.media.import_path(str(path))
    studio.media.list_items()
    await studio.media.captures.prepare("capture")
    studio.media.list_items()
    fx = studio.media.register_generated_path(
        video(tmp_path / "data/fx.mp4", "blue", 0.5, size="32x64"),
        kind="video",
        metadata={"role": "seedance_effect"},
    )
    record = await ready(
        studio,
        CompositionRequest(
            media_ids=[root.id],
            intro_effect_media_id=fx.id,
            capture_selections=[
                CaptureSelection(
                    capture_id="capture",
                    include_full=True,
                    segment_ids=["visit-0-dwell:origin-left"],
                )
            ],
            subtitles=False,
        ),
    )
    assert record["duration"] == pytest.approx(2.5)
    assert record["timeline"]["output_width"] == 64
    assert record["timeline"]["output_height"] == 64
    assert record["timeline"]["clips"][1]["source_path"] == str(path)
    assert record["tree"][1]["start"] == 0.5
    job = await studio.confirm(record["id"], record["signature"])
    assert await studio.confirm(record["id"], record["signature"]) is job
    for _ in range(200):
        if job.status.value in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.025)
    assert job.status.value == "succeeded", job.error
    assert studio.renderer.probe_duration(job.result_path) == pytest.approx(2.5, abs=1 / 30)


class FakeLLM:
    settings = SimpleNamespace(llm_config=lambda: {"enabled": True})

    @staticmethod
    def _configured(cfg):
        return True

    async def _chat(self, cfg, **kwargs):
        return "短句。" if "上一版实测" in kwargs["user"] else "首版口播。"


class FakeTTS:
    def __init__(self, directory, media, durations):
        self.tts_dir = directory
        self.media = media
        self.durations = iter(durations)
        self.calls = []

    async def synthesize(self, request, text):
        self.calls.append(text)
        path = self.tts_dir / f"voice-{len(self.calls)}.wav"
        subprocess.run(
            [
                RenderService().ffmpeg_binary(),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={next(self.durations)}",
                "-c:a",
                "pcm_s16le",
                str(path),
            ],
            check=True,
        )
        item = self.media.register_generated_path(
            path, kind="audio", metadata={"role": "tts_voice"}
        )
        return SimpleNamespace(
            media_item=item, asset=SimpleNamespace(timing_quality="unavailable"), words=[]
        )


async def saved_material(studio, tmp_path, duration=1):
    source = studio.media.import_path(str(video(tmp_path / "source.mp4", "red", duration)))
    record = await ready(studio, CompositionRequest(purpose="library", media_ids=[source.id]))
    material = await studio.save_material(record["id"], record["signature"])
    return record, material


@pytest.mark.asyncio
async def test_one_complete_synthesis_binds_saved_video_and_follows_pool(studio, tmp_path):
    from automated_video_editing_backend.core.composition import StudioPreviewRequest

    record, material = await saved_material(studio, tmp_path)
    studio.media.update_media_pool({"source_media_ids": [material.id]})
    directory = tmp_path / "data/tts"
    directory.mkdir(exist_ok=True)
    tts = FakeTTS(directory, studio.media, [0.6])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    request = MappedNarrationRequest(
        text="一篇完整旁白", sections=[{"node_id": record["tree"][0]["id"], "text": "一篇完整旁白"}]
    )
    await service.start(record["id"], request)
    await service.tasks[record["id"]]
    state = record["narration"]
    assert state["status"] == "pending_review", state["error"]
    assert not studio.material(record["id"])[1].metadata.get("bound_voice_id")
    assert studio.media.media_pool()["voiceover_media_ids"] == []
    service.confirm(record["id"], state["attempt_id"], state["review_id"])
    assert state["status"] == "ready"
    assert tts.calls == [request.text]
    assert studio.renderer.probe_duration(state["audio_path"]) == pytest.approx(0.6, abs=0.002)
    metadata = json.loads(
        Path(state["audio_path"]).with_suffix(".json").read_text(encoding="utf-8")
    )
    assert metadata["whole_audio"] is True
    assert metadata["mapped_cues"] == [
        {"text": request.text, "start": 0, "end": pytest.approx(0.6)}
    ]
    assert state["checks"][0]["status"] == "unverified"
    assert studio.media.media_pool()["voiceover_media_ids"] == [state["media_id"]]
    material = studio.material(record["id"])[1]
    assert material.metadata["bound_voice_id"] == state["media_id"]
    assert studio.media.get(state["media_id"]).metadata["bound_source_id"] == material.id
    # Workbench reads the durable binding; UI does not need to manually reselect it.
    previews = await studio.studio_previews(
        StudioPreviewRequest(media_ids=[material.id], subtitles=True)
    )
    await studio.tasks[previews[0]["id"]]
    voiced = studio.get(previews[0]["id"])
    assert voiced["status"] == "ready", voiced["error"]
    assert voiced["timeline"]["voiceover_path"] == state["audio_path"]
    assert voiced["timeline"]["subtitles"]["timing_quality"] == "estimated"
    assert voiced["duration"] == pytest.approx(1)
    assert len(voiced["timeline"]["clips"]) == 1
    another = studio.media.import_path(str(video(tmp_path / "another.mp4", "blue", 1)))
    stale = await studio.create(
        CompositionRequest(media_ids=[another.id], voiceover_media_id=state["media_id"])
    )
    await studio.tasks[stale["id"]]
    assert "不属于" in studio.get(stale["id"])["error"]
    # New scanner IDs after restart still resolve the sidecar's UUID binding.
    restored = MediaService(path=tmp_path / "data/library.json")
    pool = restored.media_pool()
    saved = next(
        i
        for i in restored.list_items()
        if i.metadata.get("role") == "raw_video"
        and i.metadata.get("composition_id") == record["id"]
    )
    assert saved.id in pool["source_media_ids"]
    assert saved.metadata["bound_voice_id"] in pool["voiceover_media_ids"]
    assert restored.get(saved.metadata["bound_voice_id"]).metadata["bound_source_id"] == saved.id
    cleared = restored.update_media_pool(
        {"source_media_ids": [], "voiceover_media_ids": pool["voiceover_media_ids"]}
    )
    assert cleared["voiceover_media_ids"] == []


@pytest.mark.asyncio
async def test_overshoot_retains_audio_without_silent_retry_or_replacing_binding(studio, tmp_path):
    from automated_video_editing_backend.services.composition_assets import read_manifest

    record, material = await saved_material(studio, tmp_path, 0.5)
    tts = FakeTTS(tmp_path / "data/tts", studio.media, [0.3, 1.2])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    req = MappedNarrationRequest(text="原文")
    await service.start(record["id"], req)
    await service.tasks[record["id"]]
    state = record["narration"]
    service.confirm(record["id"], state["attempt_id"], state["review_id"])
    previous = read_manifest(material.path)["narration_binding_id"]
    assert previous
    await service.start(record["id"], req)
    await service.tasks[record["id"]]
    state = record["narration"]
    assert state["status"] == "needs_revision"
    assert "1.1 倍内仍放不下" in state["error"]
    assert Path(state["audio_path"]).is_file()
    assert len(tts.calls) == 2  # one provider call for each explicit user request
    assert read_manifest(material.path)["narration_binding_id"] == previous
    assert not service.tasks


@pytest.mark.asyncio
async def test_library_saves_independent_root_materials_in_tree_order(studio, tmp_path):
    from automated_video_editing_backend.core.composition import StudioPreviewRequest

    path = video(tmp_path / "red.mp4", "red")
    sidecar_path(path).write_text(json.dumps(evidence()), encoding="utf8")
    root = studio.media.import_path(str(path))
    other = studio.media.import_path(str(video(tmp_path / "blue.mp4", "blue", 0.5)))
    studio.media.list_items()
    group = await studio.media.captures.prepare("capture")
    studio.media.list_items()
    dwell = next(n for n in group["segments"] if n["kind"] == "dwell")
    choice = CaptureSelection(
        capture_id="capture", segment_ids=[dwell["children"][2]["id"], dwell["children"][0]["id"]]
    )
    request = CompositionRequest(
        purpose="library", media_ids=[other.id, root.id], capture_selections=[choice]
    )
    record = await ready(studio, request)
    assert record["request"]["media_ids"] == [root.id, other.id]
    old_preview = record["preview_path"]
    material = await studio.save_material(record["id"], record["signature"])
    assert material.path != old_preview
    assert not material.metadata.get("capture_group")
    assert material.metadata["composition_tree"][0]["children"]
    assert studio.renderer.probe_duration(material.path) == pytest.approx(1.5, abs=1 / 30)
    assert record["preview_path"] == material.path
    assert not studio.is_path_in_use(root.path)
    assert (await studio.save_material(record["id"], record["signature"])).id == material.id
    assert material.id not in studio.media.media_pool()["source_media_ids"]
    second = await ready(studio, request)
    another = await studio.save_material(second["id"], second["signature"])
    assert another.path != material.path and Path(material.path).exists()
    assert root.id not in studio.media.media_pool()["source_media_ids"]
    with pytest.raises(ValueError, match="工作台"):
        await studio.studio_previews(StudioPreviewRequest(media_ids=[root.id]))
    # Both saved combinations remain whole, independent inputs.
    previews = await studio.studio_previews(
        StudioPreviewRequest(media_ids=[material.id, another.id], subtitles=False)
    )
    for preview in previews:
        await studio.tasks[preview["id"]]
        r = studio.get(preview["id"])
        assert r["status"] == "ready", r["error"]
        assert len(r["timeline"]["clips"]) == 1
        assert r["duration"] == pytest.approx(1.5)


def test_word_clock_alignment_detects_early_point_and_retains_transit():
    from automated_video_editing_backend.core.composition import NarrationBinding

    tree = [
        {
            "id": "root",
            "label": "录制",
            "start": 0,
            "end": 8,
            "duration": 8,
            "notes": ["现场备注"],
            "children": [
                {
                    "id": "A",
                    "label": "A点",
                    "kind": "dwell",
                    "start": 0,
                    "end": 3,
                    "duration": 3,
                    "children": [{"id": "shot"}],
                },
                {
                    "id": "AB",
                    "label": "A→B",
                    "kind": "transit",
                    "start": 3,
                    "end": 5,
                    "duration": 2,
                },
                {"id": "B", "label": "B点", "kind": "dwell", "start": 5, "end": 8, "duration": 3},
            ],
        }
    ]
    windows = narration_windows(tree)
    assert [w["id"] for w in windows] == ["A", "AB", "B"]
    assert all(w["notes"] == ["现场备注"] for w in windows)
    sections = [
        NarrationBinding(node_id="A", text="甲区"),
        NarrationBinding(node_id="B", text="乙区"),
    ]
    checks = check_alignment(
        windows,
        sections,
        [
            {"text": "甲区", "start_time": 0, "end_time": 1000},
            {"text": "乙区", "start_time": 2000, "end_time": 3000},
        ],
    )
    assert checks[0]["status"] == "aligned"
    assert checks[1]["status"] == "review"
    assert checks[1]["actual_start"] == 2
    assert check_alignment(windows, sections, [])[1]["status"] == "unverified"


@pytest.mark.asyncio
async def test_invalid_effect_and_release_preview_leases(studio, tmp_path):
    source = studio.media.import_path(str(video(tmp_path / "source.mp4", "red", 1)))
    invalid = await studio.create(
        CompositionRequest(media_ids=[source.id], intro_effect_media_id=source.id)
    )
    await studio.tasks[invalid["id"]]
    assert studio.get(invalid["id"])["status"] == "failed"
    assert "必须选择特效" in studio.get(invalid["id"])["error"]
    valid = await ready(studio, CompositionRequest(media_ids=[source.id], subtitles=False))
    assert studio.is_path_in_use(source.path)
    studio.discard(valid["id"])
    assert not studio.is_path_in_use(source.path)
    assert Path(source.path).is_file()
    assert Path(valid["preview_path"]).is_file()


@pytest.mark.asyncio
async def test_music_uses_beginning_and_never_extends_picture_or_covers_effects_by_default(
    studio, tmp_path
):
    from array import array

    source = studio.media.import_path(str(video(tmp_path / "source.mp4", "red", 1)))
    intro = studio.media.register_generated_path(
        video(tmp_path / "data/intro.mp4", "blue", 0.2),
        kind="video",
        metadata={"role": "seedance_effect"},
    )
    outro = studio.media.register_generated_path(
        video(tmp_path / "data/outro.mp4", "green", 0.3),
        kind="video",
        metadata={"role": "seedance_effect"},
    )
    music_path = tmp_path / "music.wav"
    subprocess.run(
        [
            studio.renderer.ffmpeg_binary(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=0.4",
            "-af",
            "apad=whole_dur=3",
            str(music_path),
        ],
        check=True,
    )
    music = studio.media.import_path(str(music_path))
    record = await ready(
        studio,
        CompositionRequest(
            media_ids=[source.id],
            music_media_id=music.id,
            intro_effect_media_id=intro.id,
            outro_effect_media_id=outro.id,
            subtitles=False,
        ),
    )
    assert record["measured_duration"] == pytest.approx(1.5, abs=1 / 30)
    assert record["timeline"]["music_delay_seconds"] == pytest.approx(0.2)
    samples = array(
        "h",
        subprocess.check_output(
            [
                studio.renderer.ffmpeg_binary(),
                "-v",
                "error",
                "-i",
                record["preview_path"],
                "-vn",
                "-ar",
                "8000",
                "-ac",
                "1",
                "-f",
                "s16le",
                "pipe:1",
            ]
        ),
    )

    def energy(start, end):
        window = samples[int(start * 8000) : int(end * 8000)]
        return sum(abs(v) for v in window) / len(window)

    assert energy(0.04, 0.12) < 10  # intro retains its silence
    assert energy(0.35, 0.45) > 100  # beginning of music, delayed past intro
    assert energy(0.9, 1.0) < 10  # no repeated/shifted beat excerpt
    assert energy(1.3, 1.4) < 10  # outro retains its silence


@pytest.mark.asyncio
async def test_binding_survives_video_and_voice_rename_and_nested_notes(studio, tmp_path):
    from automated_video_editing_backend.services.composition_assets import manifest_path
    from automated_video_editing_backend.services.rename import MediaRenameService

    record, material = await saved_material(studio, tmp_path)
    tts = FakeTTS(tmp_path / "data/tts", studio.media, [0.5])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    await service.start(record["id"], MappedNarrationRequest(text="完整旁白"))
    await service.tasks[record["id"]]
    state = record["narration"]
    service.confirm(record["id"], state["attempt_id"], state["review_id"])
    voice_id = state["media_id"]
    old_path = material.path
    renamer = MediaRenameService(
        studio.media, studio.jobs, SimpleNamespace(effects_dir=tmp_path / "data/effects")
    )
    renamed = renamer.rename(material.id, "更清楚的组合名")
    voice = renamer.rename(voice_id, "完整讲解")
    assert not manifest_path(old_path).exists()
    assert manifest_path(renamed.path).is_file()
    current = studio.material(record["id"])[1]
    assert current.metadata["bound_voice_id"] == voice.id
    assert record["preview_path"] == renamed.path
    restored = MediaService(path=tmp_path / "data/library.json")
    restored.list_items()
    source = next(i for i in restored.list_items() if i.path == renamed.path)
    assert restored.get(source.metadata["bound_voice_id"]).path == voice.path


@pytest.mark.asyncio
async def test_whole_draft_sees_transit_notes_and_orders_text_without_synthesizing(
    studio, tmp_path
):
    from automated_video_editing_backend.core.composition import NarrationAllocateRequest
    from automated_video_editing_backend.services.composition_assets import (
        read_manifest,
        save_manifest,
    )

    record, material = await saved_material(studio, tmp_path, 2)
    metadata = read_manifest(material.path)
    metadata["composition_tree"] = [
        {
            "id": "root",
            "kind": "recording",
            "label": "录制",
            "start": 0,
            "end": 2,
            "duration": 2,
            "notes": ["入口是A点"],
            "children": [
                {"id": "A", "kind": "dwell", "label": "A点", "start": 0, "end": 1, "duration": 1},
                {
                    "id": "AB",
                    "kind": "transit",
                    "label": "A→B",
                    "start": 1,
                    "end": 2,
                    "duration": 1,
                },
            ],
        }
    ]
    save_manifest(material.path, metadata)

    class DraftLLM(FakeLLM):
        async def _chat(self, cfg, **kwargs):
            payload = json.loads(kwargs["user"])
            assert payload["画面与备注"][0]["notes"] == ["入口是A点"]
            assert payload["画面与备注"][1]["kind"] == "transit"
            assert "唯一事实来源" in kwargs["system"]
            return json.dumps(
                {
                    "sections": [
                        {"node_id": "AB", "text": "接着向前走。"},
                        {"node_id": "A", "text": "这里是入口。"},
                    ]
                }
            )

    tts = FakeTTS(tmp_path / "data/tts", studio.media, [])
    service = MappedNarrationService(studio, DraftLLM(), tts)
    draft = await service.allocate(record["id"], NarrationAllocateRequest(text="入口介绍"))
    assert draft["text"] == "这里是入口。\n\n接着向前走。"
    assert [s["node_id"] for s in draft["sections"]] == ["A", "AB"]
    assert tts.calls == []


@pytest.mark.asyncio
async def test_intro_offsets_one_voice_and_its_exact_subtitles_together(studio, tmp_path):
    from automated_video_editing_backend.core.composition import StudioPreviewRequest

    record, material = await saved_material(studio, tmp_path, 1)

    class ClockTTS(FakeTTS):
        async def synthesize(self, request, text):
            result = await super().synthesize(request, text)
            result.asset.timing_quality = "exact"
            result.words = [{"text": text, "start_time": 0, "end_time": 400}]
            return result

    tts = ClockTTS(tmp_path / "data/tts", studio.media, [0.5])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    await service.start(record["id"], MappedNarrationRequest(text="你好"))
    await service.tasks[record["id"]]
    state = record["narration"]
    service.confirm(record["id"], state["attempt_id"], state["review_id"])
    effect = studio.media.register_generated_path(
        video(tmp_path / "data/intro.mp4", "blue", 0.5),
        kind="video",
        metadata={"role": "seedance_effect"},
    )
    created = await studio.studio_previews(
        StudioPreviewRequest(
            media_ids=[material.id], intro_effect_media_id=effect.id, subtitles=True
        )
    )
    await studio.tasks[created[0]["id"]]
    preview = studio.get(created[0]["id"])
    assert preview["status"] == "ready", preview["error"]
    timeline = preview["timeline"]
    assert timeline["voiceover_start_seconds"] == 0.5
    assert timeline["subtitles"]["cues"][0]["start"] == 0  # renderer adds the voice track offset
    assert timeline["target_duration_seconds"] == 1.5
    assert len(tts.calls) == 1


@pytest.mark.asyncio
async def test_adjust_reuses_one_synthesis_and_stale_review_cannot_commit(studio, tmp_path):
    from automated_video_editing_backend.core.composition import NarrationAdjustRequest
    from automated_video_editing_backend.services.composition_assets import read_manifest

    record, material = await saved_material(studio, tmp_path, 2)
    tts = FakeTTS(tmp_path / "data/tts", studio.media, [2.1])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    await service.start(record["id"], MappedNarrationRequest(text="完整旁白"))
    await service.tasks[record["id"]]
    state = record["narration"]
    assert state["status"] == "pending_review", state.get("error")
    assert state["actual_seconds"] == pytest.approx(2, abs=0.002)
    assert state["playback_blocks"][0]["rate"] == pytest.approx(1.05)
    assert not read_manifest(material.path).get("narration_binding_id")
    assert studio.renderer.probe_duration(state["preview_path"]) == pytest.approx(2, abs=0.05)
    previous_review = state["review_id"]
    await service.adjust(
        record["id"], NarrationAdjustRequest(attempt_id=state["attempt_id"], playback_rate=1.1)
    )
    await service.tasks[record["id"]]
    assert len(tts.calls) == 1
    assert state["actual_seconds"] == pytest.approx(2.1 / 1.1, abs=0.002)
    with pytest.raises(ValueError, match="当前版本"):
        service.confirm(record["id"], state["attempt_id"], previous_review)
    # A pending review and its private input survive a backend restart and safe cleanup.
    assert studio.is_path_in_use(state["source_audio_path"])
    assert studio.is_path_in_use(state["audio_path"])
    restored = CompositionService(studio.jobs, directory=studio.directory)
    reviewer = MappedNarrationService(restored, FakeLLM(), tts)
    reviewer.confirm(record["id"], state["attempt_id"], state["review_id"])
    assert read_manifest(material.path)["narration_binding_id"] == state["attempt_id"]
    with pytest.raises(ValueError, match="未确认"):
        await reviewer.adjust(record["id"], NarrationAdjustRequest(attempt_id=state["attempt_id"]))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper_metadata", [False, True])
async def test_new_candidate_and_tampered_audio_preserve_confirmed_binding(
    studio, tmp_path, tamper_metadata
):
    from automated_video_editing_backend.services.composition_assets import read_manifest

    record, material = await saved_material(studio, tmp_path, 2)
    tts = FakeTTS(tmp_path / "data/tts", studio.media, [0.5, 0.6])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    await service.start(record["id"], MappedNarrationRequest(text="第一版"))
    await service.tasks[record["id"]]
    old = record["narration"]
    service.confirm(record["id"], old["attempt_id"], old["review_id"])
    await service.start(record["id"], MappedNarrationRequest(text="第二版"))
    await service.tasks[record["id"]]
    new = record["narration"]
    assert new["status"] == "pending_review"
    assert read_manifest(material.path)["narration_binding_id"] == old["attempt_id"]
    changed = (
        Path(new["audio_path"]).with_suffix(".json") if tamper_metadata else Path(new["audio_path"])
    )
    with changed.open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(ValueError, match="已改变"):
        service.confirm(record["id"], new["attempt_id"], new["review_id"])
    assert read_manifest(material.path)["narration_binding_id"] == old["attempt_id"]


@pytest.mark.asyncio
async def test_local_audio_waits_for_next_point_and_subtitle_clock_follows(studio, tmp_path):
    from automated_video_editing_backend.services.composition_assets import (
        read_manifest,
        save_manifest,
    )
    from automated_video_editing_backend.core.composition import StudioPreviewRequest

    record, material = await saved_material(studio, tmp_path, 4)
    metadata = read_manifest(material.path)
    metadata["composition_tree"] = [
        {"id": "A", "label": "A", "kind": "dwell", "start": 0, "end": 2, "duration": 2},
        {"id": "B", "label": "B", "kind": "dwell", "start": 2, "end": 4, "duration": 2},
    ]
    save_manifest(material.path, metadata)

    class ClockTTS(FakeTTS):
        async def synthesize(self, request, text):
            result = await super().synthesize(request, text)
            result.asset.timing_quality = "exact"
            result.words = [
                {"text": "第一点。", "start_time": 100, "end_time": 700},
                {"text": "第二点。", "start_time": 900, "end_time": 1300},
            ]
            return result

    tts = ClockTTS(tmp_path / "data/tts", studio.media, [1.5])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    await service.start(
        record["id"],
        MappedNarrationRequest(
            text="第一点。第二点。",
            sections=[
                {"node_id": "A", "text": "第一点。"},
                {"node_id": "B", "text": "第二点。"},
            ],
        ),
    )
    await service.tasks[record["id"]]
    state = record["narration"]
    assert state["status"] == "pending_review", state.get("error")
    assert state["local_alignment"]
    assert state["checks"][1]["actual_start"] == pytest.approx(2.1)
    words = json.loads(
        Path(state["audio_path"]).with_suffix(".json").read_text(encoding="utf-8")
    )["words"]
    assert words[1]["start_time"] == 2100
    # The rendered audio really contains silence before B, not only a changed metadata clock.
    samples = subprocess.check_output(
        [
            studio.renderer.ffmpeg_binary(),
            "-v",
            "error",
            "-ss",
            "1",
            "-t",
            "0.5",
            "-i",
            state["audio_path"],
            "-f",
            "s16le",
            "-ac",
            "1",
            "-ar",
            "8000",
            "pipe:1",
        ]
    )
    assert samples and set(samples) == {0}
    service.confirm(record["id"], state["attempt_id"], state["review_id"])
    created = await studio.studio_previews(StudioPreviewRequest(media_ids=[material.id]))
    await studio.tasks[created[0]["id"]]
    final = studio.get(created[0]["id"])
    assert final["status"] == "ready", final.get("error")
    assert final["duration"] == 4
    assert final["timeline"]["subtitles"]["cues"][-1]["start"] >= 2


@pytest.mark.asyncio
async def test_mp3_provider_produces_only_one_selectable_candidate_after_confirmation(
    studio, tmp_path
):
    record, material = await saved_material(studio, tmp_path, 2)

    class Mp3TTS(FakeTTS):
        async def synthesize(self, request, text):
            result = await super().synthesize(request, text)
            wav = Path(result.media_item.path)
            mp3 = wav.with_suffix(".mp3")
            subprocess.run(
                [studio.renderer.ffmpeg_binary(), "-v", "error", "-y", "-i", str(wav), str(mp3)],
                check=True,
            )
            wav.unlink()
            result.media_item = studio.media.register_generated_path(
                mp3, kind="audio", metadata={"source": "data/tts", "role": "tts_voice"}
            )
            return result

    tts = Mp3TTS(tmp_path / "data/tts", studio.media, [0.7])
    service = MappedNarrationService(studio, FakeLLM(), tts)
    await service.start(record["id"], MappedNarrationRequest(text="只有一条完整旁白"))
    await service.tasks[record["id"]]
    state = record["narration"]
    assert state["status"] == "pending_review", state.get("error")
    voices = [v for v in studio.media.list_items() if v.metadata.get("role") == "tts_voice"]
    assert len(voices) == 1
    assert Path(voices[0].path).suffix == ".wav"
    assert Path(state["source_audio_path"]).suffix == ".mp3"
    studio.media.update_media_pool(
        {"source_media_ids": [material.id], "voiceover_media_ids": [voices[0].id]}
    )
    assert studio.media.media_pool()["voiceover_media_ids"] == []
    service.confirm(record["id"], state["attempt_id"], state["review_id"])
    assert studio.media.media_pool()["voiceover_media_ids"] == [voices[0].id]
    assert len(tts.calls) == 1
