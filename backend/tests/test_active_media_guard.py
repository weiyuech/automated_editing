from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from automated_video_editing_backend.api.routes import build_router
from automated_video_editing_backend.core.models import (
    EditJobRequest,
    EditTimeline,
    JobRecord,
    JobStatus,
    MediaItem,
    SubtitleCue,
    SubtitleTrack,
    TimelineAudioBed,
    TimelineClip,
)
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.render import RenderService


class DummyEvents:
    async def publish(self, *_args, **_kwargs):
        return None


def _write(path: Path, content: bytes = b"media") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_request_media_stays_in_use_before_an_automatic_timeline_exists(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    source = media.import_path(str(_write(tmp_path / "source.mp4")))
    music = media.import_path(str(_write(tmp_path / "music.mp3")))
    voiceover = media.import_path(str(_write(tmp_path / "voiceover.mp3")))
    intro = media.import_path(str(_write(tmp_path / "intro.mp4")))
    outro = media.import_path(str(_write(tmp_path / "outro.mp4")))
    voiceover.metadata["role"] = "tts_voice"
    intro.metadata["role"] = "seedance_effect"
    outro.metadata["role"] = "seedance_effect"
    jobs = JobService(DummyEvents(), media, None, None, RenderService())
    job = JobRecord(
        request=EditJobRequest(
            media_ids=[source.id],
            music_media_id=music.id,
            voiceover_media_id=voiceover.id,
            intro_effect_media_id=intro.id,
            outro_effect_media_id=outro.id,
        ),
        status=JobStatus.QUEUED,
        timeline=None,
    )
    jobs._jobs[job.id] = job

    for item in (source, music, voiceover, intro, outro):
        assert jobs.is_path_in_use(item.path), item.path
    assert not jobs.is_path_in_use(str(tmp_path / "unrelated.mp4"))

    job.status = JobStatus.SUCCEEDED
    for item in (source, music, voiceover, intro, outro):
        assert not jobs.is_path_in_use(item.path), item.path


def test_timeline_inputs_audio_bed_and_both_outputs_stay_in_use(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    renderer = RenderService()
    jobs = JobService(DummyEvents(), media, None, None, renderer)
    clip_path = _write(tmp_path / "clip.mp4")
    music_path = _write(tmp_path / "music.mp3")
    voiceover_path = _write(tmp_path / "voice.mp3")
    bed_path = _write(tmp_path / "previous-export.mp4")
    output_path = tmp_path / "delivery.mp4"
    result_path = tmp_path / "published-delivery.mp4"
    timeline = EditTimeline(
        title="manual refinement",
        clips=[
            TimelineClip(
                media_id="clip-id",
                source_path=str(clip_path),
                start=0,
                duration=2,
                timeline_start=0,
            )
        ],
        music_path=str(music_path),
        voiceover_path=str(voiceover_path),
        audio_bed=TimelineAudioBed(source_path=str(bed_path)),
        subtitles=SubtitleTrack(
            cues=[SubtitleCue(start=0, end=1, text="字幕")]
        ),
        output_path=str(output_path),
        target_duration_seconds=2,
    )
    job = JobRecord(
        request=EditJobRequest(title="manual refinement"),
        status=JobStatus.RUNNING,
        timeline=timeline,
        result_path=str(result_path),
    )
    jobs._jobs[job.id] = job

    protected = [
        clip_path,
        music_path,
        voiceover_path,
        bed_path,
        output_path,
        result_path,
        renderer.master_output_path(timeline),
    ]
    for path in protected:
        assert jobs.is_path_in_use(str(path)), path

    job.status = JobStatus.CANCELED
    for path in protected:
        assert not jobs.is_path_in_use(str(path)), path


class StubJobs:
    def __init__(self):
        self.blocked: set[str] = set()

    def is_path_in_use(self, path: str) -> bool:
        return path in self.blocked


class StubMedia:
    generated_metadata_problem = ""

    def __init__(self, item: MediaItem):
        self.item = item
        self.forget_calls = 0

    def get(self, media_id: str):
        return self.item if self.item and self.item.id == media_id else None

    def forget(self, media_id: str):
        self.forget_calls += 1
        item = self.get(media_id)
        self.item = None
        return item


class StubSeedance:
    def __init__(self, output_path: str):
        self.asset = SimpleNamespace(output_path=output_path)
        self.delete_calls = 0

    def get_asset(self, asset_id: str):
        return self.asset if asset_id == "effect-id" else None

    def delete_asset(self, asset_id: str) -> bool:
        self.delete_calls += 1
        return self.get_asset(asset_id) is not None


def test_http_preflight_and_forget_refuse_media_owned_by_an_active_job(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("APP_BRIDGE_TOKEN", "test-token")
    path = str(tmp_path / "source.mp4")
    item = MediaItem(
        id="source-id",
        path=path,
        kind="video",
        metadata={"source": "local_import", "role": "raw_video"},
    )
    media = StubMedia(item)
    jobs = StubJobs()
    jobs.blocked.add(path)
    effect_path = str(tmp_path / "effect.mp4")
    seedance = StubSeedance(effect_path)
    unused = object()
    app = FastAPI()
    app.include_router(
        build_router(
            robot=unused,
            capture=unused,
            cruise=unused,
            cruise_routes=unused,
            media=media,
            jobs=jobs,
            vault=unused,
            settings=unused,
            llm=unused,
            tts=unused,
            seedance=seedance,
            renamer=unused,
            framing_test=unused,
            admin_access=unused,
        ),
        prefix="/api",
    )
    client = TestClient(app)
    headers = {"x-bridge-token": "test-token"}

    preflight = client.post(
        "/api/media/trash-preflight",
        headers=headers,
        json={"paths": [str(tmp_path / "other.mp4"), path]},
    )
    assert preflight.status_code == 409
    assert preflight.json()["detail"] == "An editing job is using this media right now"

    forget = client.post(f"/api/media/{item.id}/forget", headers=headers, json={})
    assert forget.status_code == 409
    assert media.forget_calls == 0
    assert media.get(item.id) is item

    jobs.blocked.add(effect_path)
    effect_delete = client.delete("/api/seedance/assets/effect-id", headers=headers)
    assert effect_delete.status_code == 409
    assert seedance.delete_calls == 0

    jobs.blocked.clear()
    assert client.post(
        "/api/media/trash-preflight", headers=headers, json={"paths": [path]}
    ).json() == {"ready": True}
    forgotten = client.post(f"/api/media/{item.id}/forget", headers=headers, json={})
    assert forgotten.status_code == 200
    assert forgotten.json() == {"forgotten": True, "path": path}
    assert media.forget_calls == 1
    assert client.delete(
        "/api/seedance/assets/effect-id", headers=headers
    ).json() == {"deleted": True}
    assert seedance.delete_calls == 1
