import asyncio
from io import StringIO

import pytest

from automated_video_editing_backend import main as main_module
from automated_video_editing_backend.core.models import CaptureSession, RobotState
from automated_video_editing_backend.services import robot as robot_module


class ImmediateLoop:
    def __init__(self):
        self.calls = []

    def call_soon_threadsafe(self, callback, *args):
        self.calls.append((callback, args))


class ClosedLoop:
    def call_soon_threadsafe(self, _callback, *_args):
        raise RuntimeError("event loop is closed")


class StoppableServer:
    started = False
    should_exit = False


@pytest.mark.parametrize("line", ["shutdown\n", "shutdown\r\n", "shutdown"])
def test_shutdown_stdin_accepts_only_the_exact_command_line(line):
    loop = ImmediateLoop()
    server = StoppableServer()

    assert main_module._watch_shutdown_stdin(StringIO(line), loop, server) is True
    assert server.should_exit is False
    assert len(loop.calls) == 1


@pytest.mark.parametrize(
    "contents",
    ["", "status\n", " shutdown\n", "shutdown \n", "SHUTDOWN\n", "shutdown\r"],
)
def test_shutdown_stdin_ignores_eof_and_unrelated_or_inexact_lines(contents):
    loop = ImmediateLoop()
    server = StoppableServer()

    assert main_module._watch_shutdown_stdin(StringIO(contents), loop, server) is False
    assert server.should_exit is False
    assert loop.calls == []


def test_managed_shutdown_stdin_treats_eof_as_the_same_graceful_request():
    loop = ImmediateLoop()
    server = StoppableServer()

    assert (
        main_module._watch_shutdown_stdin(
            StringIO("ignored\n"),
            loop,
            server,
            managed_by_electron=True,
        )
        is True
    )
    assert len(loop.calls) == 1


def test_shutdown_stdin_safely_ignores_a_closed_event_loop():
    assert (
        main_module._watch_shutdown_stdin(
            StringIO("shutdown\n"),
            ClosedLoop(),
            StoppableServer(),
        )
        is False
    )


def test_shutdown_stdin_watcher_is_daemon_and_schedules_on_the_server_loop():
    loop = ImmediateLoop()
    server = StoppableServer()

    watcher = main_module._start_shutdown_stdin_watcher(
        StringIO("ignored\nshutdown\n"),
        loop,
        server,
    )

    assert watcher is not None
    watcher.join(timeout=1)
    assert watcher.daemon is True
    assert watcher.is_alive() is False
    assert server.should_exit is False
    assert len(loop.calls) == 1


@pytest.mark.asyncio
async def test_early_shutdown_waits_for_started_and_yields_before_stopping_server():
    server = StoppableServer()
    task = asyncio.create_task(main_module._set_server_should_exit_when_started(server))

    await asyncio.sleep(0)
    assert server.should_exit is False
    server.started = True
    await asyncio.sleep(0)
    assert server.should_exit is False
    await task
    assert server.should_exit is True


def test_uvicorn_server_uses_the_app_object_and_a_bounded_request_drain():
    application = main_module.create_app()
    server = main_module._build_uvicorn_server(application, "127.0.0.1", 4817)

    assert server.config.app is application
    assert server.config.host == "127.0.0.1"
    assert server.config.port == 4817
    assert server.config.reload is False
    assert server.config.access_log is False
    assert (
        server.config.timeout_graceful_shutdown
        == main_module._UVICORN_GRACEFUL_REQUEST_TIMEOUT_SECONDS
    )


def test_backend_shutdown_phases_fit_the_electron_force_kill_window_with_margin():
    complete_budget_seconds = (
        main_module._UVICORN_GRACEFUL_REQUEST_TIMEOUT_SECONDS
        + main_module._APP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS * 2
        + robot_module._RECORDING_SHUTDOWN_TIMEOUT_SECONDS * 2
    )

    assert complete_budget_seconds <= 55


@pytest.mark.asyncio
async def test_pre_shutdown_cleanup_deadlines_never_block_robot_shutdown(monkeypatch):
    monkeypatch.setattr(main_module, "_APP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS", 0.01)
    cruise_started = asyncio.Event()
    framing_started = asyncio.Event()
    robot_shutdown_called = asyncio.Event()

    async def stuck_cruise_cleanup():
        cruise_started.set()
        await asyncio.Event().wait()

    async def stuck_framing_cleanup():
        framing_started.set()
        await asyncio.Event().wait()

    async def shutdown_robot():
        robot_shutdown_called.set()

    await asyncio.wait_for(
        main_module._shutdown_app_services(
            stuck_cruise_cleanup,
            stuck_framing_cleanup,
            shutdown_robot,
        ),
        timeout=0.2,
    )

    assert cruise_started.is_set()
    assert framing_started.is_set()
    assert robot_shutdown_called.is_set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_failed_pre_shutdown_cleanups_do_not_block_robot_shutdown():
    robot_shutdown_called = asyncio.Event()

    async def fail_cruise_cleanup():
        raise RuntimeError("cruise cleanup failed")

    async def fail_framing_cleanup():
        raise RuntimeError("framing cleanup failed")

    async def shutdown_robot():
        robot_shutdown_called.set()

    await main_module._shutdown_app_services(
        fail_cruise_cleanup,
        fail_framing_cleanup,
        shutdown_robot,
    )

    assert robot_shutdown_called.is_set()


@pytest.mark.asyncio
async def test_app_exit_persists_only_video_recovery_and_never_completes_from_photo(tmp_path):
    photo = tmp_path / "PHOTO.jpg"
    photo.write_bytes(b"photo")
    session = CaptureSession(
        active=True,
        pending_media_url="http://camera.local/PHOTO.jpg",
        pending_media_local_path=str(photo),
    )

    class Capture:
        def __init__(self):
            self.saved = []

        def active_session(self):
            return session

        def remember_pending_media(
            self,
            current_session,
            media_url,
            sync_error=None,
            local_path=None,
        ):
            self.saved.append((current_session, media_url, sync_error, local_path))

    class Robot:
        async def shutdown(self, *, on_media_url, require_idle, sync_media):
            assert require_idle is True
            assert sync_media is False
            assert session.pending_media_local_path is None
            on_media_url("http://camera.local/REC_EXIT.mp4")
            return RobotState(
                recording=False,
                media_url="http://camera.local/REC_EXIT.mp4",
                media_local_path=str(photo),
            )

    capture = Capture()
    await main_module._shutdown_robot_and_capture(Robot(), capture)

    assert session.pending_media_local_path is None
    assert capture.saved[-1][1:] == (
        "http://camera.local/REC_EXIT.mp4",
        None,
        None,
    )
