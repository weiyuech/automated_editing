from __future__ import annotations

import asyncio
import os
import sys
import threading
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, TextIO

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from automated_video_editing_backend.api.routes import build_media_file_router, build_router
from automated_video_editing_backend.api.ws import websocket_endpoint
from automated_video_editing_backend.core.diagnostics import configure_diagnostics, log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.paths import GENERATED_DIRS, ensure_generated_dirs
from automated_video_editing_backend.services.admin_access import AdminAccessService
from automated_video_editing_backend.services.composition import CompositionService
from automated_video_editing_backend.services.mapped_narration import MappedNarrationService
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.cruise_routes import CruiseRouteStore
from automated_video_editing_backend.services.framing_test import FramingTestService
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.media_download import cleanup_abandoned_download_parts
from automated_video_editing_backend.services.media_vault import MediaVaultService
from automated_video_editing_backend.services.rename import MediaRenameService
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.robot import RobotService
from automated_video_editing_backend.services.seedance import SeedanceService
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.timeline import EditPlanner
from automated_video_editing_backend.services.tts import TTSService

_APP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS = 5.0
_UVICORN_GRACEFUL_REQUEST_TIMEOUT_SECONDS = 3.0
_SHUTDOWN_STDIN_COMMAND = "shutdown"
_ELECTRON_MANAGED_ENV = "APP_MANAGED_BY_ELECTRON"
_SERVER_SHUTDOWN_TASKS: set[asyncio.Task[None]] = set()


def _is_shutdown_stdin_line(line: str) -> bool:
    """Accept only the private IPC command, with at most its line terminator removed."""
    if line.endswith("\r\n"):
        line = line[:-2]
    elif line.endswith("\n"):
        line = line[:-1]
    return line == _SHUTDOWN_STDIN_COMMAND


async def _set_server_should_exit_when_started(server: uvicorn.Server) -> None:
    """Let Uvicorn enter its lifespan before honoring an early shutdown request."""
    while not server.started:
        await asyncio.sleep(0.01)
    # In older Uvicorn startup orderings, ``started`` can become true in the same loop turn that
    # completes lifespan startup. Yield once more so shutdown cannot bypass that transition.
    await asyncio.sleep(0)
    server.should_exit = True


def _start_server_shutdown_task(server: uvicorn.Server) -> None:
    task = asyncio.create_task(_set_server_should_exit_when_started(server))
    # asyncio keeps only weak references to tasks. Startup can be long in a frozen build, so retain
    # this one until it has safely crossed the `server.started` boundary.
    _SERVER_SHUTDOWN_TASKS.add(task)
    task.add_done_callback(_SERVER_SHUTDOWN_TASKS.discard)


def _schedule_server_shutdown(
    loop: asyncio.AbstractEventLoop,
    server: uvicorn.Server,
) -> bool:
    try:
        loop.call_soon_threadsafe(_start_server_shutdown_task, server)
    except RuntimeError:
        # The backend already finished and closed its event loop before stdin was delivered.
        return False
    return True


def _watch_shutdown_stdin(
    stdin: TextIO,
    loop: asyncio.AbstractEventLoop,
    server: uvicorn.Server,
    *,
    managed_by_electron: bool = False,
) -> bool:
    """Block in a daemon thread until Electron requests graceful backend shutdown."""
    try:
        for line in stdin:
            if not _is_shutdown_stdin_line(line):
                continue
            return _schedule_server_shutdown(loop, server)
    except (OSError, ValueError):
        # A closed/unavailable stdin is ordinary for standalone launches. OS signals still use
        # Uvicorn's normal graceful path.
        return False
    # Electron owns the child pipe. Its EOF means the parent disappeared or deliberately closed
    # stdin; either way, a managed backend must not remain orphaned. Standalone terminals commonly
    # have no stdin, so preserve their ordinary signal-controlled lifetime.
    return managed_by_electron and _schedule_server_shutdown(loop, server)


def _start_shutdown_stdin_watcher(
    stdin: TextIO | None,
    loop: asyncio.AbstractEventLoop,
    server: uvicorn.Server,
    *,
    managed_by_electron: bool = False,
) -> threading.Thread | None:
    if stdin is None:
        return None
    watcher = threading.Thread(
        target=_watch_shutdown_stdin,
        args=(stdin, loop, server),
        kwargs={"managed_by_electron": managed_by_electron},
        name="backend-shutdown-stdin",
        daemon=True,
    )
    watcher.start()
    return watcher


def _build_uvicorn_server(application: FastAPI, host: str, port: int) -> uvicorn.Server:
    config = uvicorn.Config(
        application,
        host=host,
        port=port,
        reload=False,
        access_log=False,
        timeout_graceful_shutdown=_UVICORN_GRACEFUL_REQUEST_TIMEOUT_SECONDS,
    )
    return uvicorn.Server(config)


async def _serve_backend(
    application: FastAPI,
    host: str,
    port: int,
    stdin: TextIO | None,
    *,
    managed_by_electron: bool = False,
) -> None:
    """Serve one app object in source and frozen builds with private graceful-shutdown IPC."""
    server = _build_uvicorn_server(application, host, port)
    _start_shutdown_stdin_watcher(
        stdin,
        asyncio.get_running_loop(),
        server,
        managed_by_electron=managed_by_electron,
    )
    await server.serve()


def _consume_cleanup_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a late cleanup result so a timed-out task cannot emit a warning."""
    with suppress(asyncio.CancelledError, Exception):
        task.result()


async def _run_bounded_shutdown_cleanup(
    cleanup: Callable[[], Awaitable[Any]],
    *,
    event: str,
) -> None:
    """Give a pre-shutdown cleanup a deadline without delaying robot safety shutdown.

    ``wait_for`` can itself wait forever when the cancelled coroutine suppresses cancellation.
    Waiting on the task set gives the cleanup a hard wall-clock bound; after that the task is
    cancelled in the background and robot shutdown continues immediately.
    """
    task = asyncio.create_task(cleanup())
    done, _ = await asyncio.wait(
        {task},
        timeout=_APP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS,
    )
    if not done:
        task.cancel()
        task.add_done_callback(_consume_cleanup_task)
        log_event(
            "error",
            event,
            error=f"cleanup timed out after {_APP_SHUTDOWN_CLEANUP_TIMEOUT_SECONDS:g} seconds",
        )
        return
    try:
        task.result()
    except asyncio.CancelledError:
        log_event("error", event, error="cleanup was cancelled")
    except Exception as exc:  # noqa: BLE001 - cleanup failure must not skip camera shutdown
        log_event("error", event, error=str(exc))


async def _shutdown_app_services(
    cancel_cruise: Callable[[], Awaitable[Any]],
    close_framing_test: Callable[[], Awaitable[Any]],
    shutdown_robot: Callable[[], Awaitable[Any]],
) -> None:
    """Run optional cleanup independently, then always reach camera-safe shutdown."""
    await _run_bounded_shutdown_cleanup(
        cancel_cruise,
        event="app.shutdown.cruise_cleanup_failed",
    )
    await _run_bounded_shutdown_cleanup(
        close_framing_test,
        event="app.shutdown.framing_cleanup_failed",
    )
    await shutdown_robot()


async def _shutdown_robot_and_capture(
    robot: RobotService,
    capture: CaptureService,
) -> None:
    """Stop hardware safely while leaving any video transfer for an explicit retry."""
    session = capture.active_session()
    if session is not None:
        # Clear a path left by an in-recording photo before Stop starts. The URL callback below is
        # deliberately durable-before-download, so it must not accidentally preserve that photo
        # as proof that this recording is already local.
        session.pending_media_local_path = None
        with suppress(OSError):
            capture.remember_pending_media(
                session,
                session.pending_media_url,
                session.pending_media_sync_error,
            )

    def remember_shutdown_url(media_url: str) -> None:
        if session is not None:
            capture.remember_pending_media(session, media_url)

    state = await robot.shutdown(
        on_media_url=remember_shutdown_url,
        require_idle=session is not None,
        sync_media=False,
    )
    if session is None:
        return
    # Exit only records the robot video URL. A local path in shared robot state may belong to
    # a photo taken during the recording or to an older transfer; neither proves that this
    # recording is safely local. The operator's explicit retry-save flow owns the download and
    # final capture attachment on the next run.
    session.pending_media_local_path = None
    with suppress(OSError):
        capture.remember_pending_media(
            session,
            state.media_url,
            state.media_sync_error,
        )


def create_app() -> FastAPI:
    ensure_generated_dirs()
    configure_diagnostics(GENERATED_DIRS["logs"] / "diagnostics.log")
    log_event("info", "backend.started", version="0.1.18")
    abandoned_parts = cleanup_abandoned_download_parts(
        GENERATED_DIRS["data"] / "downloads"
    )
    if abandoned_parts:
        log_event(
            "info",
            "media.download.partials.cleaned",
            removed=abandoned_parts,
        )
    events = EventHub()
    settings = SettingsService()
    media = MediaService()
    robot = RobotService(
        events,
        websocket_url=str(settings.robot_config().get("websocket_url") or ""),
        media=media,
    )
    framing_test = FramingTestService(robot)
    capture = CaptureService(events)
    if pending_capture := capture.active_session():
        robot.restore_recoverable_video_url(
            pending_capture.pending_media_url,
            pending_capture.pending_media_local_path,
            pending_capture.pending_media_sync_error,
        )
    cruise = CruiseService(events, robot, capture, settings.camerawork_config)
    cruise_routes = CruiseRouteStore()
    vault = MediaVaultService(media)
    llm = LLMService(settings)
    tts = TTSService(settings, media)
    planner = EditPlanner()
    renderer = RenderService()
    seedance = SeedanceService(settings, media, renderer)
    encoding_slots = asyncio.Semaphore(1)
    jobs = JobService(events, media, None, planner, renderer, settings, render_slots=encoding_slots)
    jobs.compositions = CompositionService(jobs)
    jobs.narration = MappedNarrationService(jobs.compositions, llm, tts)
    media.captures.slots = encoding_slots
    media.captures.external_path_in_use = jobs.is_path_in_use
    renamer = MediaRenameService(media, jobs, seedance)
    admin_access = AdminAccessService()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await media.captures.start(media.list_items)
        if settings.robot_config().get("websocket_url"):
            asyncio.create_task(robot.connect())
        try:
            yield
        finally:
            await _shutdown_app_services(
                cruise.cancel,
                framing_test.close,
                lambda: _shutdown_robot_and_capture(robot, capture),
            )
            await jobs.narration.close()
            await jobs.compositions.close()
            await media.captures.close()

    app = FastAPI(title="Automated Video Editing Backend", version="0.1.18", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        # electron-vite serves the installed renderer from file://, whose browser origin is
        # serialised as `null`. Every request still needs Electron's random per-launch bridge
        # token, so allowing this local origin does not expose the backend to other pages.
        allow_origins=["app://automated-video-editing", "file://", "null"],
        allow_origin_regex=r"^http://(localhost|127\.0\.0\.1):\d+$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(
        build_router(
            robot, capture, cruise, cruise_routes, media, jobs, vault, settings, llm, tts,
            seedance, renamer, framing_test,
            admin_access,
        ),
        prefix="/api",
    )
    # Mounted separately: it authenticates on a query token, not the x-bridge-token header.
    app.include_router(build_media_file_router(media, framing_test), prefix="/api")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket, events, robot, capture, cruise, framing_test)

    return app


# A frozen music worker must never start the server, scan the library, or connect the robot.
if __name__ == "__main__" and "--music-analysis-worker" in sys.argv:
    from automated_video_editing_backend.services.music_selection import worker_main

    worker_main()
    raise SystemExit(0)

if __name__ == "__main__" and "--check-bundled-runtime" in sys.argv:
    from automated_video_editing_backend.runtime_check import main as check_bundled_runtime

    check_bundled_runtime()
    raise SystemExit(0)

app = create_app()


if __name__ == "__main__":
    host = os.environ.get("APP_BACKEND_HOST", "127.0.0.1")
    if host != "127.0.0.1":
        raise SystemExit("Backend must bind to 127.0.0.1")
    port = int(os.environ.get("APP_BACKEND_PORT", "4817"))
    # Source and frozen builds intentionally serve the same object: importing by string in
    # source mode would create a second app and leave the stdin watcher attached to the wrong
    # server lifecycle.
    asyncio.run(
        _serve_backend(
            app,
            host,
            port,
            sys.stdin,
            managed_by_electron=os.environ.get(_ELECTRON_MANAGED_ENV) == "1",
        )
    )
