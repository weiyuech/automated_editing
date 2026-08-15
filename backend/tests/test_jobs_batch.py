import tempfile
from pathlib import Path

import pytest

from automated_video_editing_backend.core.models import EditBatchRequest, EditJobRequest
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService


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
    service = JobService(DummyEvents(), media, None, None, RenderService())

    async def noop(_job_id):
        return None

    service._run = noop

    def timeline_for(source, mute):
        return EditTimeline(
            title="tune", output_path=str(tmp_path / "out.mp4"), mute_original_audio=mute,
            clips=[TimelineClip(media_id="c1", source_path=str(source), start=0, duration=1, timeline_start=0)],
        )

    kept = await service.create_from_timeline(timeline_for(clip_path, mute=False))
    assert kept.timeline.include_original_audio is True

    muted = await service.create_from_timeline(timeline_for(clip_path, mute=True))
    assert muted.timeline.include_original_audio is False

    # A source with no audio track downgrades instead of failing the whole render.
    downgraded = await service.create_from_timeline(timeline_for(silent_path, mute=False))
    assert downgraded.timeline.include_original_audio is False
    assert any("没有声音轨" in warning for warning in downgraded.timeline.warnings)

    effect_timeline = timeline_for(clip_path, mute=True)
    effect_timeline.clips[0].include_audio = True
    effect_timeline.clips[0].audio_volume = 0.3
    effect = await service.create_from_timeline(effect_timeline)
    assert effect.timeline.clips[0].include_audio is True
    assert effect.timeline.clips[0].audio_volume == 0.3

    silent_effect_timeline = timeline_for(silent_path, mute=True)
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
