import re
import tempfile
from pathlib import Path

import pytest

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.services.capture import CaptureService

STAMPED = r"\d{2}-\d{2} \d{2}:\d{2}"


def _tmp_sessions():
    """Never the real data/capture-sessions.json: tests must not write project data."""
    return Path(tempfile.mkdtemp()) / "capture-sessions.json"


@pytest.mark.asyncio
async def test_every_session_is_stamped_with_its_start_time():
    capture = CaptureService(EventHub(), path=_tmp_sessions())

    await capture.start("产品晨拍")
    await capture.stop()

    title = capture.list_sessions()[0].title
    assert re.fullmatch(rf"产品晨拍 {STAMPED}", title), title


@pytest.mark.asyncio
async def test_sessions_started_in_the_same_minute_are_still_distinguishable():
    capture = CaptureService(EventHub(), path=_tmp_sessions())

    for _ in range(3):
        await capture.start("产品晨拍")
        await capture.stop()

    titles = [session.title for session in capture.list_sessions()]
    assert len(set(titles)) == 3, titles
    assert all(title.startswith("产品晨拍 ") for title in titles)
    assert titles[1].endswith("-2") and titles[2].endswith("-3")


@pytest.mark.asyncio
async def test_a_blank_title_still_produces_a_usable_name():
    capture = CaptureService(EventHub(), path=_tmp_sessions())

    await capture.start("")
    assert re.fullmatch(rf"采集 {STAMPED}", capture.active_session().title)

    await capture.stop()
    await capture.start("   ")
    assert capture.active_session().title.startswith("采集 ")


@pytest.mark.asyncio
async def test_starting_while_active_returns_the_same_session_without_restamping():
    capture = CaptureService(EventHub(), path=_tmp_sessions())

    first = await capture.start("产品晨拍")
    again = await capture.start("产品晨拍")

    assert again.id == first.id
    assert again.title == first.title
    assert len(capture.list_sessions()) == 1


@pytest.mark.asyncio
async def test_markers_need_an_active_session():
    capture = CaptureService(EventHub(), path=_tmp_sessions())

    assert await capture.add_marker(1.0, "path1#1") is None

    await capture.start("产品晨拍")
    marker = await capture.add_marker(12.5, "path1#3")

    assert marker is not None
    assert marker.timestamp == 12.5
    assert capture.active_session().markers[0].label == "path1#3"
