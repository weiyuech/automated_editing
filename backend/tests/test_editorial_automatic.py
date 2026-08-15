import json
from pathlib import Path

import pytest

from automated_video_editing_backend.core.models import (
    AnalysisResult,
    EditBatchRequest,
    EditJobRequest,
    MediaItem,
    MusicAnalysis,
)
from automated_video_editing_backend.services.capture import sidecar_path
from automated_video_editing_backend.services.editorial import FAMILY_POLICIES
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.timeline import EditPlanner


class Events:
    async def publish(self, *_args, **_kwargs):
        return None


class Analysis:
    async def analyze_video(self, item, _music=None):
        scenes = []
        for index in range(12):
            scenes.append({
                "start": index * 5.0,
                "end": (index + 1) * 5.0,
                "kind": "transit" if index % 2 == 0 else "dwell",
                "label": f"path#{index // 4 + 1}",
                "quality": 0.58 + (index % 4) * 0.1,
                "steadiness": 0.65 + (index % 3) * 0.1,
                "motion": 0.3 + (index % 4) * 0.12,
                "colour": [90 + index * 2, 126, 132],
                "fingerprint": 1 << index,
                "boundary_score": 0.7 + (index % 3) * 0.1,
            })
        return AnalysisResult(media_id=item.id, scenes=scenes)

    def analyze_music(self, _path, _warnings):
        return MusicAnalysis(
            duration_seconds=120.0,
            tempo_bpm=120.0,
            beats=[index * 0.5 for index in range(240)],
            onset_times=[index * 2.0 + 0.25 for index in range(60)],
            onset_strength=[(index % 8) / 7 for index in range(256)],
            energy=[(index % 32) / 31 for index in range(256)],
            section_boundaries=[0.0, 28.0, 60.0, 91.0, 120.0],
            beat_reliability=0.92,
            evidence="structured",
        )


class Renderer:
    def probe_frame_size(self, _path):
        return 1920, 1080

    def probe_duration(self, _path):
        return 30.0


def _write_cruise(path: Path) -> Path:
    path.write_bytes(b"video")
    path.with_name(path.name + ".capture.json").write_text(json.dumps({
        "segments": [
            {
                "path_name": "path", "goal_id": index, "status": "arrived",
                "transit_start_seconds": (index - 1) * 20,
                "arrived_at_seconds": (index - 1) * 20 + 12,
                "departed_at_seconds": index * 20,
            }
            for index in range(1, 4)
        ]
    }), encoding="utf-8")
    return path


@pytest.mark.asyncio
async def test_smart_batch_plans_a_scored_diverse_portfolio(monkeypatch, tmp_path):
    from automated_video_editing_backend.services import jobs as jobs_module
    from automated_video_editing_backend.services import timeline as timeline_module

    media = MediaService(path=tmp_path / "media.json")
    video = media.import_path(str(_write_cruise(tmp_path / "robot.mp4")))
    music_path = tmp_path / "music.mp3"
    music_path.write_bytes(b"music")
    music = media.import_path(str(music_path))
    service = JobService(Events(), media, Analysis(), EditPlanner(), Renderer())

    async def noop(_job_id):
        return None

    service._run = noop
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "logs", tmp_path / "logs")
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "exports", tmp_path / "exports")
    monkeypatch.setattr(
        timeline_module, "generated_path",
        lambda area, name: tmp_path / area / name,
    )

    created = await service.create_batch(EditBatchRequest(
        title="Commercial batch",
        media_ids=[video.id],
        music_media_ids=[music.id],
        output_count=10,
        target_duration_seconds=20,
        seed=73,
        editorial_preset="smart",
    ))

    assert len(created) == 10
    assert {job.request.editorial_preset for job in created} == {
        "showcase", "dynamic", "immersive",
    }
    picture_variants = {
        tuple((clip.media_id, round(clip.start, 2), round(clip.duration, 2)) for clip in job.timeline.clips)
        for job in created
    }
    delivery_variants = {
        (
            job.request.editorial_preset,
            round(job.timeline.music_start_seconds, 2),
            tuple((clip.media_id, round(clip.start, 2), round(clip.duration, 2)) for clip in job.timeline.clips),
        )
        for job in created
    }
    assert len(picture_variants) >= 5
    assert len(delivery_variants) >= 9
    assert all(job.timeline.planning_diagnostics["quality_score"] >= 0 for job in created)
    assert all("visual_flow" in job.timeline.planning_diagnostics["score_components"] for job in created)
    assert all(job.timeline.music_duration_seconds == 20 for job in created)

    manifest = json.loads((tmp_path / "logs" / "batch-plan-73.json").read_text(encoding="utf-8"))
    assert manifest["candidate_count"] == 40
    assert manifest["selected_count"] == 10
    assert len(manifest["rejected"]) == 30
    assert all(item["output_name"].endswith(".mp4") for item in manifest["selected"])


@pytest.mark.asyncio
async def test_one_hundred_deliveries_plan_four_hundred_candidates(monkeypatch, tmp_path):
    from automated_video_editing_backend.services import jobs as jobs_module
    from automated_video_editing_backend.services import timeline as timeline_module

    media = MediaService(path=tmp_path / "media.json")
    video_path = tmp_path / "long-tour.mp4"
    video_path.write_bytes(b"video")
    video = media.import_path(str(video_path))
    service = JobService(Events(), media, Analysis(), EditPlanner(), Renderer())

    async def noop(_job_id):
        return None

    service._run = noop
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "logs", tmp_path / "logs")
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "exports", tmp_path / "exports")
    monkeypatch.setattr(
        timeline_module, "generated_path", lambda area, name: tmp_path / area / name,
    )

    created = await service.create_batch(EditBatchRequest(
        title="Hundred-output portfolio",
        media_ids=[video.id],
        output_count=100,
        target_duration_seconds=10,
        seed=100,
        editorial_preset="smart",
    ))

    manifest = json.loads((tmp_path / "logs" / "batch-plan-100.json").read_text())
    assert len(created) == 100
    assert manifest["selected_count"] == 100
    assert manifest["candidate_count"] == 400
    assert len(manifest["rejected"]) == 300


@pytest.mark.asyncio
async def test_automatic_portfolio_is_balanced_per_source_and_stays_chronological(
    monkeypatch, tmp_path,
):
    from collections import Counter

    from automated_video_editing_backend.services import jobs as jobs_module
    from automated_video_editing_backend.services import timeline as timeline_module

    media = MediaService(path=tmp_path / "media.json")
    videos = [
        media.import_path(str(_write_cruise(tmp_path / f"robot-{index}.mp4")))
        for index in range(5)
    ]
    service = JobService(Events(), media, Analysis(), EditPlanner(), Renderer())

    async def noop(_job_id):
        return None

    service._run = noop
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "logs", tmp_path / "logs")
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "exports", tmp_path / "exports")
    monkeypatch.setattr(
        timeline_module, "generated_path", lambda area, name: tmp_path / area / name,
    )

    created = await service.create_batch(EditBatchRequest(
        title="Five tours",
        media_ids=[video.id for video in videos],
        output_count=10,
        target_duration_seconds=20,
        seed=91,
        editorial_preset="smart",
    ))

    assert Counter(job.request.media_ids[0] for job in created) == Counter({
        video.id: 2 for video in videos
    })
    for job in created:
        assert len(job.request.media_ids) == 1
        assert {clip.media_id for clip in job.timeline.clips} == set(job.request.media_ids)
        for earlier, later in zip(job.timeline.clips, job.timeline.clips[1:]):
            assert earlier.start + earlier.duration <= later.start + 1e-6
        components = job.timeline.planning_diagnostics["score_components"]
        assert components["source_integrity"] == 1.0
        assert components["chronological_integrity"] == 1.0

    manifest = json.loads((tmp_path / "logs" / "batch-plan-91.json").read_text())
    assert manifest["source_allocation"] == "one_recording_per_output"
    assert set(manifest["source_output_counts"].values()) == {2}
    assert manifest["candidate_count"] == 40


def test_each_public_direction_resolves_only_inside_its_coherent_family():
    import random

    from automated_video_editing_backend.services.editorial import resolve_family_policy

    for family, allowed in FAMILY_POLICIES.items():
        for seed in range(20):
            request = EditJobRequest(media_ids=["video"], editorial_preset=family)
            resolve_family_policy(
                request, family, random.Random(seed), has_points=True, has_music=True,
            )
            assert request.pace in allowed["pace"]
            assert request.contour in allowed["contour_music"]
            assert request.footage_mix in allowed["footage_mix"]
            assert request.emphasis in allowed["emphasis"]
            assert request.point_scope in allowed["point_scope"]


@pytest.mark.asyncio
async def test_capabilities_keep_one_ui_and_report_fallback_evidence(tmp_path):
    media = MediaService(path=tmp_path / "media.json")
    plain_path = tmp_path / "plain.mp4"
    plain_path.write_bytes(b"video")
    plain = media.import_path(str(plain_path))
    service = JobService(Events(), media, Analysis(), EditPlanner(), Renderer())

    empty = await service.editing_capabilities([], [])
    ordinary = await service.editing_capabilities([plain.id], [])

    sidecar_path(plain_path).write_text(json.dumps({
        "notes": ["点位1：产品展示区"],
        "segments": [{"path_name": "route", "goal_id": 1, "status": "arrived"}],
    }, ensure_ascii=False), encoding="utf-8")
    described = await service.editing_capabilities([plain.id], [])

    assert empty["points"]["evidence"] == "none"
    assert empty["semantic"]["evidence"] == "none"
    assert ordinary["points"]["evidence"] == "none"
    assert ordinary["semantic"]["evidence"] == "none"
    assert described["semantic"]["evidence"] == "full"
    assert described["semantic"]["count"] == 1
    assert "画面内容" in ordinary["points"]["message"]
    assert ordinary["music"]["evidence"] == "none"


def test_ambient_music_never_drives_beat_snapping():
    video = MediaItem(path="/tmp/video.mp4", kind="video")
    music = MediaItem(path="/tmp/music.mp3", kind="audio")
    request = EditJobRequest(
        media_ids=[video.id], music_media_id=music.id, target_duration_seconds=20,
        pace="normal", contour="follow_energy", editorial_preset="dynamic",
    )
    analysis = AnalysisResult(media_id=video.id, scenes=[{"start": 0, "end": 60}])
    ambient = MusicAnalysis(
        duration_seconds=60, beats=[index * 0.5 for index in range(100)],
        beat_reliability=0.1, evidence="ambient",
    )

    timeline = EditPlanner().plan(
        request, [video], [analysis], music, music_analysis=ambient,
    )

    assert timeline.music_evidence == "ambient"
    assert timeline.planning_diagnostics["music"]["cut_event_count"] == 0


def test_dynamic_music_grid_uses_strong_accents_not_every_note_onset():
    music = MusicAnalysis(
        duration_seconds=20,
        beats=[2.0, 4.0, 6.0],
        onset_times=[1.0, 1.2, 1.4, 1.6, 1.8, 3.0],
        accent_times=[1.6, 3.0],
        beat_reliability=0.9,
        evidence="structured",
    )

    view = EditPlanner()._music_view(music, 10.0, 0, "dynamic")

    assert view["cut_events"] == [1.6, 2.0, 3.0, 4.0, 6.0]
    assert 1.0 not in view["cut_events"]


def test_dynamic_music_window_ranking_counts_accents_not_all_onsets():
    music = MusicAnalysis(
        duration_seconds=20,
        beats=[1.0, 3.0, 5.0, 7.0, 9.0, 11.0, 13.0, 15.0, 17.0, 19.0],
        # Deliberately put ordinary note attacks in the second half and the genuinely strong
        # accents in the first. Dynamic ranking must prefer the latter.
        onset_times=[12.0, 14.0, 16.0, 18.0],
        accent_times=[2.0, 4.0, 6.0, 8.0],
        onset_strength=[0.5] * 20,
        energy=[0.5] * 20,
        section_boundaries=[0.0, 10.0, 20.0],
        beat_reliability=0.9,
        evidence="structured",
    )
    planner = EditPlanner()

    first = planner._music_window_score(music, 0.0, 10.0, "dynamic")
    second = planner._music_window_score(music, 10.0, 10.0, "dynamic")

    assert first > second
