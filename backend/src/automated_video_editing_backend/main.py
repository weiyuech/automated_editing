from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware

from automated_video_editing_backend.api.routes import build_media_file_router, build_router
from automated_video_editing_backend.api.ws import websocket_endpoint
from automated_video_editing_backend.core.diagnostics import configure_diagnostics, log_event
from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.paths import GENERATED_DIRS, ensure_generated_dirs
from automated_video_editing_backend.services.analysis import AnalysisService
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.cruise_routes import CruiseRouteStore
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.framing_test import FramingTestService
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.media_vault import MediaVaultService
from automated_video_editing_backend.services.rename import MediaRenameService
from automated_video_editing_backend.services.render import RenderService
from automated_video_editing_backend.services.robot import RobotService
from automated_video_editing_backend.services.seedance import SeedanceService
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.timeline import EditPlanner
from automated_video_editing_backend.services.tts import TTSService


def create_app() -> FastAPI:
    ensure_generated_dirs()
    configure_diagnostics(GENERATED_DIRS["logs"] / "diagnostics.log")
    log_event("info", "backend.started", version="0.1.1")
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
    cruise = CruiseService(events, robot, capture)
    cruise_routes = CruiseRouteStore()
    vault = MediaVaultService(media)
    llm = LLMService(settings)
    tts = TTSService(settings, media)
    analysis = AnalysisService()
    planner = EditPlanner()
    renderer = RenderService()
    seedance = SeedanceService(settings, media, renderer)
    jobs = JobService(events, media, analysis, planner, renderer, settings)
    renamer = MediaRenameService(media, jobs, seedance)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.robot_config().get("websocket_url"):
            asyncio.create_task(robot.connect())
        try:
            yield
        finally:
            await cruise.cancel()
            await framing_test.close()
            await robot.disconnect()

    app = FastAPI(title="Automated Video Editing Backend", version="0.1.1", lifespan=lifespan)
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
        ),
        prefix="/api",
    )
    # Mounted separately: it authenticates on a query token, not the x-bridge-token header.
    app.include_router(build_media_file_router(media, framing_test), prefix="/api")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket_endpoint(websocket, events, robot, capture, cruise)

    return app


app = create_app()


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--scene-detect-worker":
        from automated_video_editing_backend.services.scene_detect_worker import detect

        print(json.dumps(detect(Path(sys.argv[2]))))
    else:
        host = os.environ.get("APP_BACKEND_HOST", "127.0.0.1")
        if host != "127.0.0.1":
            raise SystemExit("Backend must bind to 127.0.0.1")
        port = int(os.environ.get("APP_BACKEND_PORT", "4817"))
        # Pass the object in frozen builds: there is no importable source tree outside the exe.
        target = app if getattr(sys, "frozen", False) else "automated_video_editing_backend.main:app"
        uvicorn.run(target, host=host, port=port, reload=False, access_log=False)
