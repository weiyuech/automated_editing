import random

from automated_video_editing_backend.core.models import (
    EditJobRequest,
    EditTimeline,
    MediaItem,
    TimelineClip,
)
from automated_video_editing_backend.services.jobs import JobService


class DummyEvents:
    async def publish(self, *_args, **_kwargs):
        return None


class FakeMedia:
    def __init__(self, items):
        self.items = {item.id: item for item in items}

    def get(self, media_id):
        return self.items.get(media_id)


class FakeRenderer:
    def __init__(self, duration=3.0):
        self.duration = duration

    def probe_duration(self, _path):
        return self.duration

    def has_audio_stream(self, _path):
        return True


def _timeline(**kw):
    clips = [
        TimelineClip(media_id="c1", source_path="/x/c1.mp4", start=0, duration=10, timeline_start=0, kind="video"),
        TimelineClip(media_id="c2", source_path="/x/c2.mp4", start=0, duration=8, timeline_start=10, kind="video"),
    ]
    return EditTimeline(
        title="t", clips=clips, output_path="/x/out.mp4",
        voiceover_path="/x/v.mp3", music_path="/x/m.mp3", **kw,
    )


# ── distribution ────────────────────────────────────────────────────────────────────────────

def test_deal_effects_empty_pool_decorates_nothing():
    svc = JobService(DummyEvents(), None, None, None, None)
    assert svc._deal_effects([], 3, "auto", random.Random(0)) == [None, None, None]


def test_deal_effects_all_scope_gives_every_output_one():
    svc = JobService(DummyEvents(), None, None, None, None)
    assert svc._deal_effects(["e1"], 3, "all", random.Random(0)) == ["e1", "e1", "e1"]


def test_deal_effects_auto_scope_decorates_best_ranked_outputs_only():
    svc = JobService(DummyEvents(), None, None, None, None)
    # 2 effects, 5 outputs, best-first ranking [2,0,...] -> outputs 2 and 0 get distinct effects.
    res = svc._deal_effects(["e1", "e2"], 5, "auto", random.Random(1), ranking=[2, 0, 4, 1, 3])
    assert sum(1 for x in res if x) == 2
    assert res[2] is not None and res[0] is not None
    assert res[1] is None and res[3] is None and res[4] is None
    assert res[0] != res[2]


def test_deal_effects_auto_without_ranking_is_random_subset():
    svc = JobService(DummyEvents(), None, None, None, None)
    res = svc._deal_effects(["e1", "e2"], 5, "auto", random.Random(2))
    assert sum(1 for x in res if x) == 2
    assert {x for x in res if x} == {"e1", "e2"}


# ── timeline decoration ─────────────────────────────────────────────────────────────────────

def _service(items, duration=3.0):
    return JobService(DummyEvents(), FakeMedia(items), None, None, FakeRenderer(duration))


def test_intro_prepends_shifts_clips_and_pushes_bed_when_cover_off():
    intro = MediaItem(id="fx-intro", path="/x/intro.mp4", kind="video")
    svc = _service([intro], duration=3.0)
    tl = _timeline()  # include_original_audio defaults False (muted)
    svc._decorate_timeline(tl, EditJobRequest(intro_effect_media_id="fx-intro", effect_cover_audio=False))

    assert tl.clips[0].media_id == "fx-intro"
    assert tl.clips[0].timeline_start == 0 and tl.clips[0].duration == 3.0
    assert tl.clips[0].include_audio is True          # muted → effect keeps its own audio
    assert tl.clips[1].timeline_start == 3.0          # content pushed back
    assert tl.clips[2].timeline_start == 13.0
    assert tl.voiceover_start_seconds == 3.0          # narration + subtitles follow
    assert tl.music_delay_seconds == 3.0              # music follows too


def test_cover_on_treats_effect_as_plain_clip_and_does_not_move_bed():
    intro = MediaItem(id="fx-intro", path="/x/intro.mp4", kind="video")
    svc = _service([intro], duration=3.0)
    tl = _timeline()
    svc._decorate_timeline(tl, EditJobRequest(intro_effect_media_id="fx-intro", effect_cover_audio=True))

    assert tl.clips[0].media_id == "fx-intro"
    assert tl.clips[0].include_audio is False
    assert tl.voiceover_start_seconds == 0
    assert tl.music_delay_seconds == 0


def test_outro_appends_at_end_without_moving_bed():
    outro = MediaItem(id="fx-outro", path="/x/outro.mp4", kind="video")
    svc = _service([outro], duration=4.0)
    tl = _timeline()
    svc._decorate_timeline(tl, EditJobRequest(outro_effect_media_id="fx-outro"))

    last = tl.clips[-1]
    assert last.media_id == "fx-outro"
    assert last.timeline_start == 18.0 and last.duration == 4.0  # after 10 + 8 of content
    assert tl.voiceover_start_seconds == 0


def test_image_effect_is_skipped_in_automatic_edit():
    img = MediaItem(id="fx-img", path="/x/pic.png", kind="image")
    svc = _service([img], duration=3.0)
    tl = _timeline()
    svc._decorate_timeline(tl, EditJobRequest(intro_effect_media_id="fx-img"))
    assert all(clip.media_id != "fx-img" for clip in tl.clips)
    assert any("不是视频" in w for w in tl.warnings)


def test_decoration_is_idempotent():
    outro = MediaItem(id="fx-outro", path="/x/outro.mp4", kind="video")
    svc = _service([outro], duration=4.0)
    tl = _timeline()
    req = EditJobRequest(outro_effect_media_id="fx-outro")
    svc._decorate_timeline(tl, req)
    svc._decorate_timeline(tl, req)  # re-run (e.g. retry) must not add it twice
    assert sum(1 for clip in tl.clips if clip.media_id == "fx-outro") == 1
