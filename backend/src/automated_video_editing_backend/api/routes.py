from __future__ import annotations

from pathlib import Path
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from automated_video_editing_backend.core.models import (
    CameraAngle,
    CruiseRequest,
    CruiseRouteSaveRequest,
    EditBatchRequest,
    EditJobRequest,
    EditTimeline,
    FramingPreferenceSaveRequest,
    MoveCommand,
    RobotGoalCommand,
    SeedanceFrameRequest,
    SeedanceGenerateRequest,
    SettingsUpdateRequest,
    TimelineDraftRequest,
    TTSGenerateRequest,
)
from automated_video_editing_backend.core.paths import RootPathError, ensure_inside_root
from automated_video_editing_backend.core.security import require_http_token, token_matches
from automated_video_editing_backend.services import subtitles as subtitle_layer
from automated_video_editing_backend.services.admin_access import AdminAccessService
from automated_video_editing_backend.services.capture import CaptureService
from automated_video_editing_backend.services.cruise import CruiseService
from automated_video_editing_backend.services.cruise_routes import CruiseRouteStore
from automated_video_editing_backend.services.framing_test import FramingTestService
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.llm import LLMService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.media_vault import MediaVaultService
from automated_video_editing_backend.services.rename import MediaRenameService
from automated_video_editing_backend.services.robot import RobotService
from automated_video_editing_backend.services.seedance import SeedanceService
from automated_video_editing_backend.services.settings import SettingsService
from automated_video_editing_backend.services.tts import TTSService


class ImportMediaRequest(BaseModel):
    path: str


class DownloadMediaRequest(BaseModel):
    url: str


class EditingCapabilityRequest(BaseModel):
    media_ids: list[str] = Field(default_factory=list, max_length=20)
    music_media_ids: list[str] = Field(default_factory=list, max_length=100)


class CaptureStartRequest(BaseModel):
    title: str = "Untitled capture"


class CaptureNoteRequest(BaseModel):
    note: str


class SwitchMapRequest(BaseModel):
    map_name: str


class RenameMediaRequest(BaseModel):
    name: str


class AdminUnlockRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


Secured = Annotated[None, Depends(require_http_token)]

# Video playback needs its own router. A <video> tag cannot set request headers, so this
# route authenticates on a query token like the WebSocket does, instead of x-bridge-token.
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v", ".mkv"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def build_media_file_router(
    media: MediaService, framing_test: FramingTestService | None = None
) -> APIRouter:
    router = APIRouter()

    @router.get("/media/file")
    async def media_file(path: str, token: str = ""):
        """Stream a local media file so the app can preview footage without an external player.

        Starlette's FileResponse answers Range requests, so scrubbing works.

        A file qualifies two ways: it lives inside the app root, or the operator imported it
        (so it is in the media library). Imported clips sit wherever the user keeps them —
        Desktop, Photos — so app-root-only would make imported footage unpreviewable, which
        is most of what someone wants to preview. Arbitrary paths are still refused.
        """
        if not token_matches(token):
            raise HTTPException(status_code=401, detail="Unauthorized")

        candidate = Path(path).expanduser()
        try:
            resolved = ensure_inside_root(candidate)
        except RootPathError:
            resolved = candidate.resolve()
            if not media.is_known_path(str(resolved)):
                raise HTTPException(
                    status_code=403,
                    detail="Refusing a file that is neither in the app folder nor the media library",
                ) from None

        if not resolved.is_file():
            raise HTTPException(status_code=404, detail="File not found")
        if resolved.suffix.lower() not in VIDEO_SUFFIXES | AUDIO_SUFFIXES | IMAGE_SUFFIXES:
            raise HTTPException(status_code=415, detail="Not a previewable media file")

        return FileResponse(resolved)

    @router.get("/framing-test/media")
    async def framing_test_media(token: str = "", preview_id: str = ""):
        if not token_matches(token):
            raise HTTPException(status_code=401, detail="Unauthorized")
        if framing_test is None:
            raise HTTPException(status_code=404, detail="Framing test is unavailable")
        status = framing_test.status()
        path = framing_test.preview_path
        if not path or not preview_id or preview_id != status["preview_id"]:
            raise HTTPException(status_code=404, detail="Temporary preview is no longer available")
        return FileResponse(path)

    return router


def build_router(
    robot: RobotService,
    capture: CaptureService,
    cruise: CruiseService,
    cruise_routes: CruiseRouteStore,
    media: MediaService,
    jobs: JobService,
    vault: MediaVaultService,
    settings: SettingsService,
    llm: LLMService,
    tts: TTSService,
    seedance: SeedanceService,
    renamer: MediaRenameService,
    framing_test: FramingTestService,
    admin_access: AdminAccessService,
) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_http_token)])

    async def require_settings_admin(
        x_admin_token: str | None = Header(default=None),
    ) -> None:
        admin_access.require(x_admin_token)

    @router.get("/health")
    async def health(_: Secured = None) -> dict[str, str]:
        return {"ok": "true", "service": "automated-video-editing-backend"}

    @router.get("/robot/status")
    async def robot_status(_: Secured = None):
        return await robot.status()

    @router.post("/robot/connect")
    async def robot_connect(_: Secured = None):
        return await robot.connect()

    @router.get("/robot/maps")
    async def robot_maps(_: Secured = None):
        try:
            return await robot.map_list()
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/robot/switch-map")
    async def robot_switch_map(request: SwitchMapRequest, _: Secured = None):
        try:
            return await robot.switch_map(request.map_name)
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/robot/paths")
    async def robot_paths(map_name: str, _: Secured = None):
        try:
            return await robot.path_list(map_name)
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/robot/goal")
    async def robot_goal(command: RobotGoalCommand, _: Secured = None):
        try:
            return await robot.set_goal(command)
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/robot/stop")
    async def robot_stop(_: Secured = None):
        return await robot.stop_motion()

    @router.post("/robot/move")
    async def robot_move(command: MoveCommand, _: Secured = None):
        try:
            return await robot.move(command)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/robot/camera-angle")
    async def robot_camera_angle(angle: CameraAngle, _: Secured = None):
        try:
            return await robot.set_camera_angle(angle)
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/robot/start-recording")
    async def robot_start_recording(_: Secured = None):
        try:
            return await robot.start_recording()
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/robot/stop-recording")
    async def robot_stop_recording(_: Secured = None):
        try:
            return await robot.stop_recording()
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/robot/capture-photo")
    async def robot_capture_photo(_: Secured = None):
        try:
            return await robot.capture_photo()
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.get("/framing-test/status")
    async def framing_test_status(_: Secured = None):
        return framing_test.status()

    @router.post("/framing-test/start")
    async def framing_test_start(_: Secured = None):
        if cruise.is_running or capture.active_session() is not None:
            raise HTTPException(status_code=409, detail="请先停止当前采集或巡游")
        try:
            return await framing_test.start()
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except (RuntimeError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.post("/framing-test/discard")
    async def framing_test_discard(_: Secured = None):
        await framing_test.discard()
        return framing_test.status()

    @router.post("/framing-test/confirm")
    async def framing_test_confirm(
        request: FramingPreferenceSaveRequest, _: Secured = None
    ):
        # A centred 16:9/9:16 preset needs no test clip. Only a custom position claims to have
        # been chosen from the real lens, so only that mode requires a live preview.
        if request.mode == "custom" and framing_test.preview_path is None:
            raise HTTPException(status_code=409, detail="自定义位置需要先完成取景测试")
        crop_x = 0.5 if request.mode == "center" else request.crop_x
        crop_y = 0.5 if request.mode == "center" else request.crop_y
        summary = settings.update(SettingsUpdateRequest.model_validate({
            "automation": {
                "output_aspect_ratio": request.aspect_ratio,
                "framing_configured": True,
                "framing_mode": request.mode,
                "framing_crop_x": crop_x,
                "framing_crop_y": crop_y,
            }
        }))
        await framing_test.discard()
        return summary

    @router.post("/framing-preference/clear")
    async def framing_preference_clear(_: Secured = None):
        """Return future edits to their source frame; a test preview may remain on screen."""
        return settings.update(SettingsUpdateRequest.model_validate({
            "automation": {
                "framing_configured": False,
                "framing_mode": "center",
                "framing_crop_x": 0.5,
                "framing_crop_y": 0.5,
            }
        }))

    @router.get("/capture/sessions")
    async def capture_sessions(_: Secured = None):
        return capture.list_sessions()

    @router.post("/capture/start")
    async def capture_start(request: CaptureStartRequest, _: Secured = None):
        if cruise.is_running:
            raise HTTPException(status_code=409, detail="A cruise is running; it already controls recording")
        try:
            await robot.start_recording()
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return await capture.start(request.title)

    @router.post("/capture/stop")
    async def capture_stop(_: Secured = None):
        if cruise.is_running:
            raise HTTPException(
                status_code=409,
                detail="A cruise is running; cancel it instead of stopping the recording",
            )
        try:
            state = await robot.stop_recording()
        except Exception as exc:
            await capture.stop()
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        session = await capture.stop()
        if session is not None and state.media_local_path:
            capture.attach_to_recording(session, state.media_local_path)
        return session

    @router.post("/capture/note")
    async def capture_note(request: CaptureNoteRequest, _: Secured = None):
        return await capture.add_note(request.note)

    @router.get("/cruise")
    async def cruise_current(_: Secured = None):
        return cruise.current()

    @router.post("/cruise/start")
    async def cruise_start(request: CruiseRequest, _: Secured = None):
        try:
            return await cruise.start(request)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @router.post("/cruise/cancel")
    async def cruise_cancel(_: Secured = None):
        return await cruise.cancel()

    @router.get("/cruise/routes")
    async def cruise_route_list(_: Secured = None):
        return cruise_routes.list_routes()

    @router.post("/cruise/routes")
    async def cruise_route_save(request: CruiseRouteSaveRequest, _: Secured = None):
        return cruise_routes.save(request.name, request.request)

    @router.delete("/cruise/routes/{route_id}")
    async def cruise_route_delete(route_id: str, _: Secured = None):
        if not cruise_routes.delete(route_id):
            raise HTTPException(status_code=404, detail="Route not found")
        return {"deleted": True}

    @router.post("/cruise/routes/{route_id}/validate")
    async def cruise_route_validate(route_id: str, _: Secured = None):
        route = cruise_routes.get(route_id)
        if route is None:
            raise HTTPException(status_code=404, detail="Route not found")
        return await cruise.validate_route(route)

    @router.post("/cruise/routes/{route_id}/start")
    async def cruise_route_start(route_id: str, _: Secured = None):
        route = cruise_routes.get(route_id)
        if route is None:
            raise HTTPException(status_code=404, detail="Route not found")

        # Verify what can be verified before anything moves; goal ids are checked at
        # dispatch, where a rejected one costs no movement.
        validation = await cruise.validate_route(route)
        if not validation.ok:
            raise HTTPException(status_code=409, detail=validation.model_dump(mode="json"))

        try:
            run = await cruise.start(route.request)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ConnectionError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

        cruise_routes.touch(route_id)
        return {"run": run, "validation": validation}

    @router.get("/media")
    async def media_list(_: Secured = None):
        return media.list_items()

    @router.get("/media/vault")
    async def media_vault(_: Secured = None):
        return vault.list_assets()

    @router.get("/media/calendar")
    async def media_calendar(_: Secured = None):
        return vault.calendar()

    @router.get("/media/storage")
    async def media_storage(_: Secured = None):
        return vault.storage_report()

    @router.post("/media/cleanup-safe")
    async def media_cleanup_safe(_: Secured = None):
        return vault.safe_cleanup()

    @router.post("/media/{media_id}/rename")
    async def media_rename(media_id: str, request: RenameMediaRequest, _: Secured = None):
        """Rename the file on disk so every screen and Finder agree on one name."""
        try:
            return media.get(media_id) and renamer.rename(media_id, request.name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/media/{media_id}/forget")
    async def media_forget(media_id: str, _: Secured = None):
        """Remove an imported clip from the library without touching the file on disk."""
        item = media.forget(media_id)
        if item is None:
            raise HTTPException(
                status_code=404,
                detail="Only imported clips can be removed from the library",
            )
        return {"forgotten": True, "path": item.path}

    @router.post("/media/import")
    async def media_import(request: ImportMediaRequest, _: Secured = None):
        return media.import_path(request.path)

    @router.post("/media/download")
    async def media_download(request: DownloadMediaRequest, _: Secured = None):
        try:
            return await media.download_url(request.url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/jobs")
    async def jobs_list(_: Secured = None):
        return jobs.list_jobs()

    @router.post("/editing/capabilities")
    async def editing_capabilities(request: EditingCapabilityRequest, _: Secured = None):
        return await jobs.editing_capabilities(request.media_ids, request.music_media_ids)

    @router.post("/jobs")
    async def jobs_create(request: EditJobRequest, _: Secured = None):
        try:
            return await jobs.create(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/jobs/batch")
    async def jobs_create_batch(request: EditBatchRequest, _: Secured = None):
        try:
            return await jobs.create_batch(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/timeline/draft")
    async def timeline_draft(request: TimelineDraftRequest, _: Secured = None):
        try:
            return await jobs.draft_timeline(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/timeline/render")
    async def timeline_render(timeline: EditTimeline, _: Secured = None):
        try:
            return await jobs.create_from_timeline(timeline)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/subtitles/track")
    async def subtitle_track(media_id: str, _: Secured = None):
        """The cue list belonging to an export, for re-cutting it in 手动微调.

        Times are in that export's own timeline. The caller says where the sound it came with
        now sits, and the renderer shifts the cues by the same amount — so swapping the picture
        underneath for a still or an effect clip leaves the words where the voice put them.
        """
        item = media.get(media_id)
        if item is None:
            raise HTTPException(status_code=404, detail="That media item no longer exists")
        sidecar = (item.metadata or {}).get("subtitles_path")
        # Falling back to the paired name covers an export made before this was recorded, and
        # one picked up by a library scan rather than registered by the job that made it.
        track = jobs.renderer.read_subtitle_sidecar(sidecar or item.path)
        # "This export never had subtitles" and "its cue file has gone missing" both arrive here
        # as an empty track, and they are not the same news. Only the second means the operator
        # is about to re-cut something and silently lose words it used to have.
        problem = ""
        if track is None and sidecar:
            problem = "这个成片记录过字幕，但字幕文件已经找不到了，重新剪不会带上字幕。"
        return {
            "media_id": media_id,
            "has_burned_subtitles": bool((item.metadata or {}).get("has_burned_subtitles")),
            # A subtitle track proves narration is present. New exports also record this
            # explicitly, covering voiceovers made without burned subtitles.
            "has_voiceover": bool(
                (item.metadata or {}).get("has_voiceover")
                or (track and (track.get("has_voiceover") or track.get("cues")))
            ),
            "track": track,
            "problem": problem,
        }

    @router.get("/subtitles/fonts")
    async def subtitle_fonts(_: Secured = None):
        """The bundled fonts, and whether subtitles can be burned in at all.

        `can_burn` is reported alongside, because a missing font and an FFmpeg without libass
        both end with an export that has no text on it, and the operator can act on the two only
        if told which one they have.
        """
        can_burn = jobs.renderer.supports_subtitles()
        return {
            "fonts": subtitle_layer.available_fonts(),
            "can_burn": can_burn,
            "ffmpeg": jobs.renderer.ffmpeg_binary() if can_burn else None,
            # The fractions, not just the names. The picker shows 小/中/大 at their true relative
            # sizes, and sending the real numbers means that preview cannot drift away from what
            # gets rendered — there is no second copy of the ratios to keep in step.
            "sizes": [
                {"key": key, "fraction": fraction}
                for key, fraction in subtitle_layer.SIZE_PRESETS.items()
            ],
        }

    @router.get("/settings")
    async def settings_get(_: Secured = None):
        return settings.summary()

    @router.post("/settings/admin/unlock")
    async def settings_admin_unlock(request: AdminUnlockRequest, _: Secured = None):
        return admin_access.unlock(request.username, request.password)

    @router.get("/settings/admin/status")
    async def settings_admin_status(
        x_admin_token: str | None = Header(default=None), _: Secured = None
    ):
        return admin_access.status(x_admin_token)

    @router.post("/settings/admin/lock")
    async def settings_admin_lock(
        x_admin_token: str | None = Header(default=None), _: Secured = None
    ):
        admin_access.lock(x_admin_token)
        return {"unlocked": False}

    @router.put("/settings", dependencies=[Depends(require_settings_admin)])
    async def settings_update(request: SettingsUpdateRequest, _: Secured = None):
        summary = settings.update(request)
        if request.robot:
            await robot.configure_websocket_url(summary.robot.websocket_url)
        return summary

    @router.post("/settings/test/llm", dependencies=[Depends(require_settings_admin)])
    async def settings_test_llm(_: Secured = None):
        return await llm.test()

    @router.post("/settings/test/tts", dependencies=[Depends(require_settings_admin)])
    async def settings_test_tts(_: Secured = None):
        return await tts.test()

    @router.post("/settings/test/seedance", dependencies=[Depends(require_settings_admin)])
    async def settings_test_seedance(_: Secured = None):
        return await seedance.test()

    @router.get("/tts/assets")
    async def tts_assets(_: Secured = None):
        return tts.list_assets()

    @router.post("/tts/generate")
    async def tts_generate(request: TTSGenerateRequest, _: Secured = None):
        try:
            # Capture notes describe the picture for local semantic matching. They are never
            # narration input: a camera/location reminder must not become customer-facing speech.
            if not request.text.strip():
                raise ValueError("Voiceover needs text")

            if request.use_llm:
                final_text = await llm.draft_voiceover(request.text)
            else:
                final_text = request.text

            if not final_text.strip():
                raise ValueError("Nothing usable was found in the text")
            return await tts.synthesize(request, final_text)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/seedance/assets")
    async def seedance_assets(_: Secured = None):
        return seedance.list_assets()

    @router.get("/seedance/quota")
    async def seedance_quota(_: Secured = None):
        return seedance.quota()

    @router.delete("/seedance/assets/{asset_id}")
    async def seedance_delete(asset_id: str, _: Secured = None):
        if not seedance.delete_asset(asset_id):
            raise HTTPException(status_code=404, detail="Effect not found")
        return {"deleted": True}

    @router.post("/seedance/frame")
    async def seedance_frame(request: SeedanceFrameRequest, _: Secured = None):
        try:
            return await seedance.extract_frame(request.source_video_media_id, request.timestamp_seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.post("/seedance/generate")
    async def seedance_generate(request: SeedanceGenerateRequest, _: Secured = None):
        try:
            return await seedance.generate(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return router
