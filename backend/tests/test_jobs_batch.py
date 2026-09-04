import json
import tempfile
from pathlib import Path

import pytest

from automated_video_editing_backend.core.models import (
    EditBatchRequest,
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
from automated_video_editing_backend.services.media import (
    GeneratedMetadataPersistenceError,
    MediaService,
)
from automated_video_editing_backend.services.render import RenderService


class DummyEvents:
    async def publish(self, *_args, **_kwargs):
        return None


def test_three_sources_are_dealt_four_three_three_across_ten_outputs():
    service = JobService(DummyEvents(), None, None, None, None)

    dealt = service._deal_sources(["source-a", "source-b", "source-c"], 10)

    assert dealt == [
        "source-a", "source-b", "source-c",
        "source-a", "source-b", "source-c",
        "source-a", "source-b", "source-c",
        "source-a",
    ]


class CompatibilitySemantic:
    def compatibility(self, source, voice):
        return 0.9 if source.metadata.get("topic") == voice.metadata.get("topic") else 0.2


@pytest.mark.asyncio
async def test_job_service_auto_names_exports_and_creates_batch(tmp_path):
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    music = media.import_path(str(_write(tmp_path / "music.mp3")))
    voice = media.import_path(str(_write(tmp_path / "voice.mp3")))
    voice.metadata.update({"source": "data/tts", "role": "tts_voice"})
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    single = await service.create(EditJobRequest(media_ids=[video.id]))
    batch = await service.create_batch(
        EditBatchRequest(
            media_ids=[video.id],
            music_media_ids=[music.id],
            voiceover_media_ids=[voice.id],
            output_count=3,
        )
    )

    names = [single.request.output_name, *[job.request.output_name for job in batch]]
    assert all(name.endswith(".mp4") for name in names)
    assert len(set(names)) == 4
    # Ticking a pool means "use it": every output gets one from each, never a silent gap.
    # The pools used to be spread one-asset-per-output and padded with None, so one music
    # and one voiceover over three outputs left one with music, one with narration, one mute.
    assert all(job.request.music_media_id == music.id for job in batch)
    assert all(job.request.voiceover_media_id == voice.id for job in batch)


@pytest.mark.asyncio
async def test_single_job_rejects_an_output_name_that_escapes_into_downloads(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    with pytest.raises(ValueError, match="cannot contain"):
        await service.create(EditJobRequest(
            media_ids=[video.id],
            output_name="../data/downloads/escaped.mp4",
        ))

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_batch_title_is_only_a_filename_stem_and_cannot_escape_exports(tmp_path):
    from automated_video_editing_backend.core.paths import GENERATED_DIRS

    media = MediaService(path=tmp_path / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop
    jobs = await service.create_batch(EditBatchRequest(
        title="../../data/downloads/escaped",
        media_ids=[video.id],
        output_count=2,
    ))

    export_root = GENERATED_DIRS["exports"].resolve()
    assert len(jobs) == 2
    assert all("/" not in job.request.output_name for job in jobs)
    assert all("\\" not in job.request.output_name for job in jobs)
    assert all(
        (export_root / job.request.output_name).resolve().parent == export_root
        for job in jobs
    )


@pytest.mark.asyncio
async def test_manual_timeline_rejects_downloads_output_before_render_or_restart(tmp_path):
    from automated_video_editing_backend.core.paths import generated_path

    media_path = tmp_path / "media-library.json"
    media = MediaService(path=media_path)
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    escaped = generated_path("data", "downloads", "escaped-manual-output.mp4")
    escaped.unlink(missing_ok=True)
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unsafe manual output",
        output_path=str(escaped),
        clips=[TimelineClip(
            media_id=video.id,
            source_path=video.path,
            start=0,
            duration=1,
            timeline_start=0,
        )],
    )

    with pytest.raises(ValueError, match="managed exports"):
        await service.create_from_timeline(timeline)

    assert not escaped.exists()
    reopened = MediaService(path=media_path)
    assert all(item.path != str(escaped) for item in reopened.list_items())


@pytest.mark.asyncio
async def test_manual_timeline_rejects_a_clip_path_unknown_to_the_media_library(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    unknown = _write(tmp_path / "unknown.mp4")
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unknown clip",
        output_path="",
        clips=[TimelineClip(
            media_id="forged-or-stale-id",
            source_path=str(unknown),
            start=0,
            duration=1,
            timeline_start=0,
        )],
    )

    with pytest.raises(ValueError, match="不在媒体库"):
        await service.create_from_timeline(timeline)

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_manual_timeline_rejects_an_unknown_retained_audio_bed(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    clip = media.import_path(str(_write(tmp_path / "known.mp4")))
    unknown_bed = _write(tmp_path / "unknown-bed.mp4")
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unknown bed",
        output_path="",
        clips=[TimelineClip(
            media_id=clip.id,
            source_path=clip.path,
            start=0,
            duration=1,
            timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(source_path=str(unknown_bed)),
    )

    with pytest.raises(ValueError, match="原声不在媒体库"):
        await service.create_from_timeline(timeline)

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_manual_timeline_rejects_client_subtitles_without_a_bed_sidecar(tmp_path):
    media = MediaService(path=tmp_path / "media-library.json")
    clip = media.import_path(str(_write(tmp_path / "known-no-sidecar.mp4")))
    service = JobService(DummyEvents(), media, None, None, RenderService())
    timeline = EditTimeline(
        title="unverified client track",
        output_path="",
        clips=[TimelineClip(
            media_id=clip.id,
            source_path=clip.path,
            start=0,
            duration=1,
            timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(source_path=clip.path),
        subtitles=SubtitleTrack(cues=[
            SubtitleCue(start=0, end=0.8, text="客户端提交的未知字幕"),
        ]),
    )

    with pytest.raises(ValueError, match="没有可验证的字幕数据"):
        await service.create_from_timeline(timeline)

    assert service.list_jobs() == []


@pytest.mark.asyncio
async def test_professional_batch_balances_outputs_without_mixing_recordings(tmp_path):
    """Five selected recordings and ten outputs means two intact timelines per recording,
    never ten timelines each made from all five sources."""
    from collections import Counter

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    videos = [
        media.import_path(str(_write(tmp_path / f"source-{index}.mp4")))
        for index in range(5)
    ]
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop
    jobs = await service.create_batch(EditBatchRequest(
        media_ids=[video.id for video in videos], output_count=10, seed=27,
    ))

    assert all(len(job.request.media_ids) == 1 for job in jobs)
    assert Counter(job.request.media_ids[0] for job in jobs) == Counter({
        video.id: 2 for video in videos
    })
    assert {job.request.recording_scope for job in jobs} == {"all"}
    assert {job.request.start_rotation for job in jobs} == {0}


@pytest.mark.asyncio
async def test_every_output_gets_both_pools_and_pairs_stay_distinct(tmp_path):
    """Two music and two voiceovers over four outputs must spend all four combinations."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    musics = [media.import_path(str(_write(tmp_path / f"m{index}.mp3"))) for index in range(2)]
    voices = [media.import_path(str(_write(tmp_path / f"v{index}.mp3"))) for index in range(2)]
    for voice in voices:
        voice.metadata.update({"source": "data/tts", "role": "tts_voice"})
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    batch = await service.create_batch(
        EditBatchRequest(
            media_ids=[video.id],
            music_media_ids=[item.id for item in musics],
            voiceover_media_ids=[item.id for item in voices],
            output_count=4,
        )
    )

    pairs = [(job.request.music_media_id, job.request.voiceover_media_id) for job in batch]
    assert all(music and voice for music, voice in pairs)
    assert len(set(pairs)) == 4, pairs


@pytest.mark.asyncio
async def test_voiceover_deck_stays_balanced_but_pairs_with_matching_source_content(tmp_path):
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    videos = [
        media.import_path(str(_write(tmp_path / "showroom.mp4"))),
        media.import_path(str(_write(tmp_path / "warehouse.mp4"))),
    ]
    voices = [
        media.import_path(str(_write(tmp_path / "showroom.mp3"))),
        media.import_path(str(_write(tmp_path / "warehouse.mp3"))),
    ]
    for item, topic in zip(videos, ("showroom", "warehouse")):
        item.metadata["topic"] = topic
    for item, topic in zip(voices, ("showroom", "warehouse")):
        item.metadata.update({"source": "data/tts", "role": "tts_voice", "topic": topic})
    service = JobService(
        DummyEvents(), media, None, None, None, semantic=CompatibilitySemantic(),
    )

    async def noop(_job_id):
        return None

    service._run = noop
    jobs = await service.create_batch(EditBatchRequest(
        media_ids=[item.id for item in videos],
        voiceover_media_ids=[item.id for item in voices],
        output_count=2,
        seed=4,
    ))

    for job in jobs:
        source = media.get(job.request.media_ids[0])
        voice = media.get(job.request.voiceover_media_id)
        assert source.metadata["topic"] == voice.metadata["topic"]


@pytest.mark.asyncio
async def test_unticked_pool_stays_empty(tmp_path):
    """Not wanting music is expressed by leaving the pool empty, and nothing else."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    voice = media.import_path(str(_write(tmp_path / "voice.mp3")))
    voice.metadata.update({"source": "data/tts", "role": "tts_voice"})
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    batch = await service.create_batch(
        EditBatchRequest(media_ids=[video.id], voiceover_media_ids=[voice.id], output_count=3)
    )

    assert all(job.request.music_media_id is None for job in batch)
    assert all(job.request.voiceover_media_id == voice.id for job in batch)


@pytest.mark.asyncio
async def test_a_batch_seed_replays_the_whole_day(tmp_path):
    """A hundred outputs a day makes "roll that batch again" routine, and an unseeded run
    can only be repeated by luck."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    musics = [media.import_path(str(_write(tmp_path / f"m{index}.mp3"))) for index in range(3)]
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    def batch(seed):
        return EditBatchRequest(
            media_ids=[video.id],
            music_media_ids=[item.id for item in musics],
            output_count=6,
            seed=seed,
        )

    def shape(jobs):
        return [(job.request.music_media_id, job.request.variant_seed) for job in jobs]

    first = await service.create_batch(batch(1234))
    again = await service.create_batch(batch(1234))
    other = await service.create_batch(batch(99))

    assert shape(first) == shape(again)
    assert shape(first) != shape(other)
    # Every job records the seed it was built from, so one output can be rebuilt alone.
    assert all(job.request.batch_seed == 1234 for job in first)
    assert len({job.request.variant_seed for job in first}) == 6


@pytest.mark.asyncio
async def test_cut_variation_chooses_between_variety_and_a_held_picture(tmp_path):
    """Varied cuts are the usual want, but not the only one: judging two soundtracks against
    each other needs the picture held still, and an approved edit must not re-cut itself."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    musics = [media.import_path(str(_write(tmp_path / f"m{index}.mp3"))) for index in range(2)]
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    async def seeds_for(variation):
        jobs = await service.create_batch(EditBatchRequest(
            media_ids=[video.id],
            music_media_ids=[item.id for item in musics],
            output_count=4,
            seed=7,
            cut_variation=variation,
        ))
        return [job.request.variant_seed for job in jobs], [job.request.music_media_id for job in jobs]

    varied, _ = await seeds_for("per_output")
    shared, shared_music = await seeds_for("shared")
    fixed, _ = await seeds_for("fixed")

    assert len(set(varied)) == 4
    # One picture across the batch, so the soundtrack is the only thing that changed.
    assert len(set(shared)) == 1 and shared[0] is not None
    assert len(set(shared_music)) > 1
    # No seed at all: the pick every export used before seeding existed.
    assert fixed == [None] * 4


@pytest.mark.asyncio
async def test_a_batch_spreads_its_editing_policies_across_the_outputs(tmp_path):
    """One recording must not yield a hundred videos that all made the same editorial
    choices, and dealing from a reshuffled deck beats independent rolls at clumping."""
    from collections import Counter

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write_cruise(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = await service.create_batch(
        EditBatchRequest(media_ids=[video.id], output_count=12, seed=5)
    )
    mixes = Counter(job.request.footage_mix for job in jobs)
    emphases = Counter(job.request.emphasis for job in jobs)

    assert set(mixes) == {"dwell_heavy", "balanced", "transit_heavy"}
    assert set(emphases) == {"target", "coverage"}
    # Evenly dealt, not rolled: twelve outputs over three mixes is four each.
    assert set(mixes.values()) == {4}
    assert set(emphases.values()) == {6}


@pytest.mark.asyncio
async def test_one_video_asked_for_alone_still_rolls_its_policy(tmp_path):
    """A policy left unset means "you pick", not "use the middle setting". Quietly
    defaulting would make every hand-made edit the same edit."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    singles = [await service.create(EditJobRequest(media_ids=[video.id])) for _ in range(40)]
    mixes = {job.request.footage_mix for job in singles}
    emphases = {job.request.emphasis for job in singles}

    assert len(mixes) > 1 and len(emphases) > 1
    # Whatever was rolled is recorded, so the job says what it actually made.
    assert None not in mixes and None not in emphases


@pytest.mark.asyncio
async def test_an_explicit_policy_is_never_overridden(tmp_path):
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = [
        await service.create(EditJobRequest(
            media_ids=[video.id], footage_mix="transit_heavy", emphasis="coverage",
        ))
        for _ in range(10)
    ]

    assert {job.request.footage_mix for job in jobs} == {"transit_heavy"}
    assert {job.request.emphasis for job in jobs} == {"coverage"}


@pytest.mark.asyncio
async def test_a_seeded_job_rolls_the_same_policy_every_time(tmp_path):
    """The seed reproduces the whole job, policy included, not just the cuts."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    rolled = [
        await service.create(EditJobRequest(media_ids=[video.id], variant_seed=808))
        for _ in range(5)
    ]

    assert len({(job.request.footage_mix, job.request.emphasis) for job in rolled}) == 1


@pytest.mark.asyncio
async def test_a_hundred_outputs_are_a_hundred_different_edits(tmp_path):
    """Both properties at once, and neither implies the other: every level of every dimension
    gets its fair share, and no two outputs make the same set of choices."""
    from collections import Counter

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write_cruise(tmp_path / "source.mp4")))
    # With music, because `follow_energy` reads the track — without one it is not a level the
    # batch has, and the space is smaller by exactly that.
    music = media.import_path(str(_write(tmp_path / "m.mp3")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = await service.create_batch(
        EditBatchRequest(
            media_ids=[video.id], music_media_ids=[music.id], output_count=100, seed=7,
        )
    )
    signatures = [
        (job.request.pace, job.request.contour, job.request.footage_mix, job.request.emphasis,
         job.request.point_scope, job.request.recording_scope, job.request.start_rotation)
        for job in jobs
    ]

    assert len(set(signatures)) == 100
    for field, levels in (("pace", 3), ("contour", 5), ("footage_mix", 3), ("emphasis", 2)):
        counts = Counter(getattr(job.request, field) for job in jobs)
        assert len(counts) == levels, field
        # Dealt, not rolled: no level gets more than one output above its fair share.
        assert max(counts.values()) - min(counts.values()) <= 1, (field, counts)


@pytest.mark.asyncio
async def test_a_batch_does_not_deal_levels_that_cannot_do_anything(tmp_path):
    """Plain footage has no points to rotate, no places to narrow to, and nothing the robot
    classified. Dealing those levels anyway would not make different videos — it would spend
    the batch's variety on choices that change nothing and report a spread that never existed.
    """
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    plain = media.import_path(str(_write(tmp_path / "plain.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = await service.create_batch(
        EditBatchRequest(media_ids=[plain.id], output_count=12, seed=3)
    )

    # Pace and contour need nothing from the footage, so they still vary.
    assert len({job.request.pace for job in jobs}) == 3
    assert len({job.request.contour for job in jobs}) == 4  # no music, so no follow_energy
    # The rest have nothing to act on, so they are recorded at their neutral level — not
    # rolled per job, which would claim a decision that changed nothing about the video.
    assert {job.request.footage_mix for job in jobs} == {"balanced"}
    assert {job.request.emphasis for job in jobs} == {"target"}
    assert {job.request.point_scope for job in jobs} == {"all"}
    assert {job.request.start_rotation for job in jobs} == {0}


@pytest.mark.asyncio
async def test_one_output_of_every_batch_uses_everything(tmp_path):
    """Left to the draw, the complete edit — the one most likely to be wanted — can simply
    fail to be made."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    for seed in range(5):
        jobs = await service.create_batch(
            EditBatchRequest(media_ids=[video.id], output_count=20, seed=seed)
        )
        full = [
            job for job in jobs
            if job.request.point_scope == "all"
            and job.request.recording_scope == "all"
            and job.request.start_rotation == 0
        ]
        assert full, seed


@pytest.mark.asyncio
async def test_naming_policies_narrows_the_spread_to_those(tmp_path):
    """The operator buttons come later, but the field they will drive works now."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write_cruise(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = await service.create_batch(EditBatchRequest(
        media_ids=[video.id], output_count=6, seed=5,
        footage_mixes=["dwell_heavy"], emphases=["coverage"],
    ))

    assert {job.request.footage_mix for job in jobs} == {"dwell_heavy"}
    assert {job.request.emphasis for job in jobs} == {"coverage"}


@pytest.mark.asyncio
async def test_an_unseeded_batch_still_records_the_seed_it_drew(tmp_path):
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = await service.create_batch(EditBatchRequest(media_ids=[video.id], output_count=3))
    seeds = {job.request.batch_seed for job in jobs}

    assert len(seeds) == 1 and seeds != {None}


@pytest.mark.asyncio
async def test_job_service_rejects_export_as_source(tmp_path):
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    export = media.import_path(str(_write(tmp_path / "export.mp4")))
    export.metadata.update({"source": "exports", "role": "export"})
    service = JobService(DummyEvents(), media, None, None, None)

    with pytest.raises(ValueError, match="Source videos"):
        await service.create(EditJobRequest(media_ids=[export.id]))


@pytest.mark.asyncio
async def test_physical_export_path_cannot_be_forged_into_an_automatic_source(
    tmp_path,
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    path = generated_path("exports", f"forged-source-{uuid4().hex[:8]}.mp4")
    path.write_bytes(b"delivery")
    try:
        media = MediaService(path=tmp_path / "media-library.json")
        forged = MediaItem(
            path=str(path),
            kind="video",
            metadata={"source": "local_import", "role": "raw_video"},
        )
        media._items[forged.id] = forged
        service = JobService(DummyEvents(), media, None, None, None)

        with pytest.raises(ValueError, match="Source videos"):
            await service.create(EditJobRequest(media_ids=[forged.id]))

        with pytest.raises(ValueError, match="Source videos"):
            await service.create_batch(EditBatchRequest(media_ids=[forged.id]))
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_managed_effect_path_cannot_be_forged_into_an_automatic_source(tmp_path):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    path = generated_path(
        "data", "seedance", "effects", f"forged-source-{uuid4().hex[:8]}.mp4"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"effect")
    try:
        media = MediaService(path=tmp_path / "media-library.json")
        forged = MediaItem(
            path=str(path),
            kind="video",
            metadata={"source": "local_import", "role": "raw_video"},
        )
        media._items[forged.id] = forged
        service = JobService(DummyEvents(), media, None, None, None)

        with pytest.raises(ValueError, match="Source videos"):
            await service.create(EditJobRequest(media_ids=[forged.id]))

        with pytest.raises(ValueError, match="Source videos"):
            await service.create_batch(EditBatchRequest(media_ids=[forged.id]))
    finally:
        path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_failed_delivery_registration_removes_the_new_flat_file_family(
    tmp_path, monkeypatch
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"failed-delivery-{uuid4().hex[:8]}.mp4")
    renderer = RenderService()
    media = MediaService(path=tmp_path / "media-library.json")
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="failure",
        output_path=str(output),
        clips=[TimelineClip(
            media_id="source", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=None,
    )
    job = JobRecord(
        request=EditJobRequest(title="failure", media_ids=["source"]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_render(_timeline):
        output.write_bytes(b"delivery")
        output.with_suffix(".ass").write_text("subtitle", encoding="utf-8")
        output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(output)

    monkeypatch.setattr(renderer, "render", fake_render)
    monkeypatch.setattr(
        media,
        "register_generated_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GeneratedMetadataPersistenceError("simulated registration failure")
        ),
    )

    await service._run_job(job.id)

    assert job.status == JobStatus.FAILED
    assert "simulated registration failure" in job.error
    assert not output.exists()
    assert not output.with_suffix(".ass").exists()
    assert not output.with_suffix(".subtitles.json").exists()


@pytest.mark.asyncio
async def test_failed_group_registration_removes_delivery_and_master_without_publishing(
    tmp_path, monkeypatch
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"failed-master-{uuid4().hex[:8]}.mp4")
    master = output.with_name(f"{output.stem} 母版{output.suffix}")
    renderer = RenderService()
    media = MediaService(path=tmp_path / "media-library.json")
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="master failure",
        output_path=str(output),
        clips=[TimelineClip(
            media_id="source", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    job = JobRecord(
        request=EditJobRequest(title="master failure", media_ids=["source"]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_render(_timeline):
        output.write_bytes(b"delivery")
        output.with_suffix(".ass").write_text("subtitle", encoding="utf-8")
        output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(output)

    async def fake_master(_timeline):
        master.write_bytes(b"master")
        master.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(master)

    monkeypatch.setattr(renderer, "render", fake_render)
    monkeypatch.setattr(renderer, "render_master", fake_master)
    monkeypatch.setattr(
        media,
        "register_generated_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            GeneratedMetadataPersistenceError("simulated group registration failure")
        ),
    )
    try:
        await service._run_job(job.id)

        assert job.status == JobStatus.FAILED
        assert "simulated group registration failure" in job.error
        assert not output.exists()
        assert not output.with_suffix(".ass").exists()
        assert not output.with_suffix(".subtitles.json").exists()
        assert not master.exists()
        assert not master.with_suffix(".subtitles.json").exists()
        assert all(
            item.path not in {str(output), str(master)} for item in media.list_items()
        )
        if media.generated_metadata_path.exists():
            manifest = json.loads(
                media.generated_metadata_path.read_text(encoding="utf-8")
            )
            assert str(output) not in manifest["items"]
            assert str(master) not in manifest["items"]
    finally:
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".subtitles.json"),
        ):
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_master_render_failure_removes_the_entire_new_export_group(
    tmp_path, monkeypatch
):
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"master-render-failure-{uuid4().hex[:8]}.mp4")
    master = output.with_name(f"{output.stem} 母版{output.suffix}")
    renderer = RenderService()
    media = MediaService(path=tmp_path / "media-library.json")
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="master render failure",
        output_path=str(output),
        clips=[TimelineClip(
            media_id="source", source_path=str(tmp_path / "source.mp4"),
            start=0, duration=1, timeline_start=0,
        )],
        subtitles=SubtitleTrack(cues=[SubtitleCue(start=0, end=0.8, text="一句")]),
    )
    job = JobRecord(
        request=EditJobRequest(title="master render failure", media_ids=["source"]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_render(_timeline):
        output.write_bytes(b"delivery")
        output.with_suffix(".ass").write_text("subtitle", encoding="utf-8")
        output.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        return str(output)

    async def fail_master(_timeline):
        master.write_bytes(b"partial master")
        master.with_suffix(".subtitles.json").write_text("{}", encoding="utf-8")
        raise RuntimeError("simulated master render failure")

    monkeypatch.setattr(renderer, "render", fake_render)
    monkeypatch.setattr(renderer, "render_master", fail_master)
    try:
        await service._run_job(job.id)

        assert job.status == JobStatus.FAILED
        assert "simulated master render failure" in job.error
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".ass"),
            master.with_suffix(".subtitles.json"),
        ):
            assert not path.exists()
        assert all(
            item.path not in {str(output), str(master)} for item in media.list_items()
        )
    finally:
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".ass"),
            master.with_suffix(".subtitles.json"),
        ):
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_out_of_range_carried_cues_publish_one_unsubtitled_output(
    tmp_path, monkeypatch
):
    """Rendering, registration and master creation share the same output-clock boundary."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    output = generated_path("exports", f"outside-cues-{uuid4().hex[:8]}.mp4")
    master = output.with_name(f"{output.stem} 母版{output.suffix}")
    source_path = _write(tmp_path / "source.mp4")
    media = MediaService(path=tmp_path / "media-library.json")
    source = media.import_path(str(source_path))
    renderer = RenderService()
    service = JobService(DummyEvents(), media, None, None, renderer)
    timeline = EditTimeline(
        title="outside cues",
        output_path=str(output),
        clips=[TimelineClip(
            media_id=source.id, source_path=source.path,
            start=0, duration=2, timeline_start=0,
        )],
        audio_bed=TimelineAudioBed(
            source_path=source.path,
            source_start=10,
            timeline_start=0,
            has_voiceover=True,
        ),
        subtitles=SubtitleTrack(cues=[
            SubtitleCue(start=1, end=2, text="no longer in this cut"),
        ]),
    )
    job = JobRecord(
        request=EditJobRequest(title="outside cues", media_ids=[source.id]),
        timeline=timeline,
    )
    service._jobs[job.id] = job

    async def fake_ffmpeg(args):
        Path(args[-1]).write_bytes(b"single output")

    monkeypatch.setattr(renderer, "_run", fake_ffmpeg)
    try:
        await service._run_job(job.id)

        assert job.status == JobStatus.SUCCEEDED
        assert job.result_path == str(output)
        published = next(item for item in media.list_items() if item.path == str(output))
        assert published.metadata["variant"] == "single"
        assert published.metadata["variant_label"] == "成片"
        assert published.metadata["subtitles_path"] == ""
        assert published.metadata["has_burned_subtitles"] is False
        assert output.exists()
        assert not output.with_suffix(".ass").exists()
        assert not output.with_suffix(".subtitles.json").exists()
        assert not master.exists()
        assert not master.with_suffix(".subtitles.json").exists()
    finally:
        for path in (
            output,
            output.with_suffix(".ass"),
            output.with_suffix(".subtitles.json"),
            master,
            master.with_suffix(".subtitles.json"),
        ):
            path.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_manual_tune_rejects_a_burned_export_even_when_it_is_not_the_first_clip(
    tmp_path,
):
    """Exports remain selectable for inspection, but cannot be submitted as re-cut picture."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    token = uuid4().hex[:8]
    original = generated_path("exports", f"burned-original-{token}.mp4")
    try:
        original.write_bytes(b"subtitle pixels")
        media = MediaService(path=tmp_path / "media-library.json")
        original_item = media.register_generated_path(
            original,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "variant": "subtitled",
                "has_burned_subtitles": True,
            },
        )
        safe_path = _write(tmp_path / "safe-source.mp4")
        safe = media.import_path(str(safe_path))
        timeline = EditTimeline(
            title="unsafe recut",
            output_path="",
            clips=[
                TimelineClip(
                    media_id=safe.id,
                    source_path=safe.path,
                    start=0,
                    duration=1,
                    timeline_start=0,
                ),
                TimelineClip(
                    # Simulate a stale/direct client trying to make a burned path look unrelated.
                    # The backend must bind provenance to the path FFmpeg will actually open.
                    media_id="stale-or-forged-export-id",
                    source_path=original_item.path,
                    start=0,
                    duration=1,
                    timeline_start=1,
                ),
            ],
        )
        service = JobService(DummyEvents(), media, None, None, RenderService())

        with pytest.raises(ValueError, match="字幕已烧录"):
            await service.create_from_timeline(timeline)

        assert service.list_jobs() == []
        assert not Path(timeline.output_path).exists()
    finally:
        original.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_manual_tune_restores_its_authoritative_track_when_an_old_client_omits_it(
    tmp_path,
):
    """The backend closes the old-client/loading-race path without silently dropping words."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    master = generated_path("exports", f"clean-master-{uuid4().hex[:8]}.mp4")
    sidecar = master.with_suffix(".subtitles.json")
    try:
        master.write_bytes(b"clean picture and mixed sound")
        sidecar.write_text(
            '{"cues":[{"start":0,"end":1,"text":"六和桥"}]}',
            encoding="utf-8",
        )
        media = MediaService(path=tmp_path / "media-library.json")
        master_item = media.register_generated_path(
            master,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "variant": "master",
                "subtitles_path": str(sidecar),
                "has_burned_subtitles": False,
                "has_voiceover": True,
            },
        )
        timeline = EditTimeline(
            title="missing carried track",
            output_path="",
            clips=[TimelineClip(
                media_id=master_item.id,
                source_path=master_item.path,
                start=0,
                duration=1,
                timeline_start=0,
            )],
            audio_bed=TimelineAudioBed(
                source_path=master_item.path,
                source_start=0,
                timeline_start=0,
                has_voiceover=True,
            ),
            subtitles=None,
        )
        service = JobService(DummyEvents(), media, None, None, RenderService())

        async def noop(_job_id):
            return None

        service._run = noop
        job = await service.create_from_timeline(timeline)

        assert job.timeline.subtitles is not None
        assert [cue.text for cue in job.timeline.subtitles.cues] == ["六和桥"]
        assert job.request.subtitles is True
        assert not Path(timeline.output_path).exists()
    finally:
        master.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_manual_tune_replaces_another_exports_subtitles_with_the_beds_own_track(
    tmp_path,
):
    """A direct client cannot pair export A's soundtrack with export B's submitted words."""
    from uuid import uuid4

    from automated_video_editing_backend.core.paths import generated_path

    master = generated_path("exports", f"bound-master-{uuid4().hex[:8]}.mp4")
    sidecar = master.with_suffix(".subtitles.json")
    try:
        master.write_bytes(b"clean picture and mixed sound")
        sidecar.write_text(
            json.dumps({
                "video": master.name,
                "cues": [{"start": 0, "end": 1, "text": "六和桥"}],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        media = MediaService(path=tmp_path / "media-library.json")
        master_item = media.register_generated_path(
            master,
            kind="video",
            metadata={
                "source": "exports",
                "role": "export",
                "variant": "master",
                "subtitles_path": str(sidecar),
                "has_burned_subtitles": False,
                "has_voiceover": True,
            },
        )
        timeline = EditTimeline(
            title="mismatched client track",
            output_path="",
            clips=[TimelineClip(
                media_id=master_item.id,
                source_path=master_item.path,
                start=0,
                duration=1,
                timeline_start=0,
            )],
            audio_bed=TimelineAudioBed(
                source_path=master_item.path,
                source_start=0,
                timeline_start=0,
                has_voiceover=True,
            ),
            subtitles=SubtitleTrack(
                cues=[SubtitleCue(start=0, end=1, text="错误的六合桥")]
            ),
        )
        service = JobService(DummyEvents(), media, None, None, RenderService())

        async def noop(_job_id):
            return None

        service._run = noop
        job = await service.create_from_timeline(timeline)

        assert job.timeline.subtitles is not None
        assert [cue.text for cue in job.timeline.subtitles.cues] == ["六和桥"]
        assert all(cue.text != "错误的六合桥" for cue in job.timeline.subtitles.cues)
        assert not Path(timeline.output_path).exists()
    finally:
        master.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)


def _write(path):
    path.write_bytes(b"placeholder")
    return path


def _write_cruise(path, points=6):
    """A recording with cruise spans beside it.

    Most editing dimensions act on the points a cruise recorded, so plain footage cannot
    exercise them — and, since the batch stopped dealing levels that cannot do anything, no
    longer even receives them.
    """
    import json

    path.write_bytes(b"placeholder")
    cursor, segments = 0.0, []
    for index in range(points):
        segments.append({
            "index": index, "path_name": "path1", "goal_id": index + 1, "status": "arrived",
            "transit_start_seconds": cursor, "arrived_at_seconds": cursor + 12.0,
            "departed_at_seconds": cursor + 20.0,
        })
        cursor += 20.0
    path.with_name(path.name + ".capture.json").write_text(
        json.dumps({"markers": [], "segments": segments}), encoding="utf-8",
    )
    return path


def test_export_is_named_after_the_edit_title(tmp_path):
    """The 剪辑标题 box was ignored: every export came out as 导出 regardless of what you
    typed. A Chinese title also had to survive, since the old sanitiser stripped non-ASCII."""
    import re

    from automated_video_editing_backend.core.events import EventHub
    from automated_video_editing_backend.services.jobs import JobService
    from automated_video_editing_backend.services.render import RenderService

    jobs = JobService(EventHub(), MediaService(path=tmp_path / "m.json"), None, None, RenderService())

    named = jobs._next_output_name("节拍剪辑")
    assert re.fullmatch(r"节拍剪辑 \d{2}-\d{2} \d{2}-\d{2}\.mp4", named), named

    # A blank title still falls back to something sensible.
    assert jobs._next_output_name("").startswith("导出 ")
    # A title that is only illegal characters must not produce an empty filename.
    assert jobs._next_output_name("///").startswith("导出 ")


def test_two_exports_in_the_same_minute_do_not_collide(tmp_path):
    from automated_video_editing_backend.core.events import EventHub
    from automated_video_editing_backend.services.jobs import JobService
    from automated_video_editing_backend.services.render import RenderService

    jobs = JobService(EventHub(), MediaService(path=tmp_path / "m.json"), None, None, RenderService())
    names = {jobs._next_output_name("节拍剪辑") for _ in range(3)}
    assert len(names) == 3, names


@pytest.mark.parametrize(
    "blocked_member",
    [
        "delivery",
        "master",
        "delivery_ass",
        "delivery_subtitles",
        "master_ass",
        "master_subtitles",
    ],
)
def test_output_name_reserves_the_whole_export_file_family(
    tmp_path, monkeypatch, blocked_member
):
    """An orphaned hidden companion must not be overwritten by a later render."""
    from datetime import datetime as real_datetime

    import automated_video_editing_backend.services.jobs as jobs_module

    media = MediaService(path=tmp_path / "media-library.json")
    jobs = JobService(DummyEvents(), media, None, None, None)

    class FrozenDatetime:
        @classmethod
        def now(cls):
            return real_datetime(2026, 9, 4, 14, 23).astimezone()

    monkeypatch.setattr(jobs_module, "datetime", FrozenDatetime)
    monkeypatch.setitem(jobs_module.GENERATED_DIRS, "exports", tmp_path)

    delivery = tmp_path / "客户成片 09-04 14-23.mp4"
    master = tmp_path / "客户成片 09-04 14-23 母版.mp4"
    family = {
        "delivery": delivery,
        "master": master,
        "delivery_ass": delivery.with_suffix(".ass"),
        "delivery_subtitles": delivery.with_suffix(".subtitles.json"),
        "master_ass": master.with_suffix(".ass"),
        "master_subtitles": master.with_suffix(".subtitles.json"),
    }
    family[blocked_member].write_bytes(b"existing")

    assert jobs._next_output_name("客户成片") == "客户成片 09-04 14-23_01.mp4"


@pytest.mark.asyncio
async def test_manual_tune_render_also_honours_the_original_audio_toggle(tmp_path):
    """手动微调 renders a client-built timeline, which skipped the audio resolution entirely.

    The toggle was therefore dead on that path even after it was wired up for automation.
    """
    import subprocess

    from automated_video_editing_backend.core.models import EditTimeline, TimelineClip
    from automated_video_editing_backend.services.render import RenderService

    clip_path = tmp_path / "withsound.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-c:v", "libx264", "-c:a", "aac", "-shortest", str(clip_path)],
        check=True,
    )
    silent_path = tmp_path / "nosound.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=160x120:rate=30:duration=2", str(silent_path)],
        check=True,
    )

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    clip_item = media.import_path(str(clip_path))
    silent_item = media.import_path(str(silent_path))
    service = JobService(DummyEvents(), media, None, None, RenderService())

    async def noop(_job_id):
        return None

    service._run = noop

    def timeline_for(source, mute):
        return EditTimeline(
            title="tune", output_path="", mute_original_audio=mute,
            clips=[TimelineClip(
                media_id=source.id,
                source_path=source.path,
                start=0,
                duration=1,
                timeline_start=0,
            )],
        )

    kept = await service.create_from_timeline(timeline_for(clip_item, mute=False))
    assert kept.timeline.include_original_audio is True

    muted = await service.create_from_timeline(timeline_for(clip_item, mute=True))
    assert muted.timeline.include_original_audio is False

    # A source with no audio track downgrades instead of failing the whole render.
    downgraded = await service.create_from_timeline(timeline_for(silent_item, mute=False))
    assert downgraded.timeline.include_original_audio is False
    assert any("没有声音轨" in warning for warning in downgraded.timeline.warnings)

    effect_timeline = timeline_for(clip_item, mute=True)
    effect_timeline.clips[0].include_audio = True
    effect_timeline.clips[0].audio_volume = 0.3
    effect = await service.create_from_timeline(effect_timeline)
    assert effect.timeline.clips[0].include_audio is True
    assert effect.timeline.clips[0].audio_volume == 0.3

    silent_effect_timeline = timeline_for(silent_item, mute=True)
    silent_effect_timeline.clips[0].include_audio = True
    silent_effect = await service.create_from_timeline(silent_effect_timeline)
    assert silent_effect.timeline.clips[0].include_audio is False
    assert any("静音特效" in warning for warning in silent_effect.timeline.warnings)


@pytest.mark.asyncio
async def test_a_batch_makes_only_as_many_as_can_differ(tmp_path):
    """Asking for a hundred videos out of twelve combinations does not make a hundred videos.
    It makes twelve, eight times each, at eight times the render cost — and someone has to
    watch them all to find that out."""
    from automated_video_editing_backend.services.settings import SettingsService

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    plain = media.import_path(str(_write(tmp_path / "plain.mp4")))
    music = [media.import_path(str(_write(tmp_path / f"m{index}.mp3"))) for index in range(2)]
    settings = SettingsService(path=tmp_path / "settings.json")
    service = JobService(DummyEvents(), media, None, None, None, settings)

    async def noop(_job_id):
        return None

    service._run = noop

    bare = await service.create_batch(EditBatchRequest(media_ids=[plain.id], output_count=100))
    # Plain footage supports pace x contour only, and without music there is no follow_energy.
    assert len(bare) == 3 * 4

    with_music = await service.create_batch(EditBatchRequest(
        media_ids=[plain.id], music_media_ids=[item.id for item in music], output_count=100,
    ))
    # Two soundtracks double it, and the fifth contour becomes available.
    assert len(with_music) == 3 * 5 * 2

    # What is asked for still wins when it is the smaller number.
    modest = await service.create_batch(EditBatchRequest(media_ids=[plain.id], output_count=5))
    assert len(modest) == 5


@pytest.mark.asyncio
async def test_the_days_allowance_is_spent_across_batches_and_survives_a_restart(tmp_path):
    """A limit that resets when the process does is not a limit."""
    from automated_video_editing_backend.core.models import (
        AutomationSettingsUpdate,
        SettingsUpdateRequest,
    )
    from automated_video_editing_backend.services.settings import SettingsService

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write_cruise(tmp_path / "source.mp4")))
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.update(SettingsUpdateRequest(automation=AutomationSettingsUpdate(daily_output_limit=10)))
    service = JobService(DummyEvents(), media, None, None, None, settings)

    async def noop(_job_id):
        return None

    service._run = noop

    first = await service.create_batch(EditBatchRequest(media_ids=[video.id], output_count=8))
    second = await service.create_batch(EditBatchRequest(media_ids=[video.id], output_count=8))

    assert len(first) == 8
    # Only what is left of the day, not what was asked for.
    assert len(second) == 2
    assert settings.output_quota().remaining_today == 0

    with pytest.raises(ValueError, match="今日"):
        await service.create_batch(EditBatchRequest(media_ids=[video.id], output_count=1))

    reopened = SettingsService(path=tmp_path / "settings.json")
    assert reopened.output_quota().used_today == 10


@pytest.mark.asyncio
async def test_without_settings_only_the_material_limit_applies(tmp_path):
    """The planner is exercised in tests with no settings file, and must still run."""
    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    service = JobService(DummyEvents(), media, None, None, None)

    async def noop(_job_id):
        return None

    service._run = noop

    jobs = await service.create_batch(EditBatchRequest(media_ids=[video.id], output_count=4))
    assert len(jobs) == 4
    assert all(job.request.output_aspect_ratio is None for job in jobs)
    assert all(job.request.output_crop_x is None for job in jobs)


@pytest.mark.asyncio
async def test_global_framing_preset_is_frozen_onto_new_jobs(tmp_path):
    from automated_video_editing_backend.core.models import (
        AutomationSettingsUpdate,
        SettingsUpdateRequest,
    )
    from automated_video_editing_backend.services.settings import SettingsService

    media = MediaService(path=Path(tempfile.mkdtemp()) / "media-library.json")
    video = media.import_path(str(_write(tmp_path / "source.mp4")))
    settings = SettingsService(path=tmp_path / "settings.json")
    settings.update(SettingsUpdateRequest(
        automation=AutomationSettingsUpdate(
            output_aspect_ratio="9:16", framing_configured=True,
            framing_mode="custom", framing_crop_x=0.8, framing_crop_y=0.5,
        )
    ))
    service = JobService(DummyEvents(), media, None, None, None, settings)

    async def noop(_job_id):
        return None

    service._run = noop
    job = await service.create(EditJobRequest(media_ids=[video.id]))

    assert job.request.output_aspect_ratio == "9:16"
    assert job.request.output_crop_x == 0.8
    assert job.request.output_crop_y == 0.5
