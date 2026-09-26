from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import ValidationError

from automated_video_editing_backend.core.events import EventHub
from automated_video_editing_backend.core.models import (
    CaptureSession,
    CruiseSegment,
    TimelineMarker,
    utc_now,
)
from automated_video_editing_backend.core.paths import generated_path
from automated_video_editing_backend.core.store import read_json, write_json

# Sessions are small; keeping a long tail costs nothing and preserves the notes an
# operator wrote weeks ago against footage still sitting in the vault.
MAX_SESSIONS = 500


def sidecar_path(video_path: str | Path) -> Path:
    """Where a recording's notes and markers live: next to the video itself.

    A sidecar rather than in-memory metadata because MediaService rebuilds its item list by
    rescanning directories on startup, so anything held only in RAM is lost on restart.
    Keeping it beside the file means the notes and markers travel with the footage.
    """
    video = Path(video_path)
    return video.with_name(video.name + ".capture.json")


def gimbal_sidecar_path(video_path: str | Path) -> Path:
    """Where physical yaw/pitch samples live for downstream motion classification."""
    video = Path(video_path)
    return video.with_name(video.name + ".gimbal.json")


def read_sidecar(video_path: str | Path) -> dict | None:
    path = sidecar_path(video_path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def inspect_sidecar(video_path: str | Path) -> dict:
    """Describe the point evidence attached to a video without decoding the video.

    ``read_sidecar`` deliberately returns ``None`` for both absence and corruption because
    analysis must always be able to fall back to pixels.  The UI and planner diagnostics need
    the distinction: a broken file is actionable, while an absent one is ordinary footage.
    """
    path = sidecar_path(video_path)
    if not path.exists():
        return {
            "evidence": "none", "point_count": 0, "successful_points": 0,
            "failed_points": 0, "message": "未识别点位，将按画面内容剪辑",
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "evidence": "invalid", "point_count": 0, "successful_points": 0,
            "failed_points": 0, "message": "点位文件无法读取",
        }
    if not isinstance(payload, dict):
        return {
            "evidence": "invalid", "point_count": 0, "successful_points": 0,
            "failed_points": 0, "message": "点位文件格式无效",
        }

    if payload.get("recording_timeline"):
        return inspect_timeline(payload["recording_timeline"])
    segments = [item for item in (payload.get("segments") or []) if isinstance(item, dict)]
    markers = [item for item in (payload.get("markers") or []) if isinstance(item, dict)]
    segment_identities = {
        f"{item.get('path_name')}#{item.get('goal_id')}" for item in segments
        if item.get("path_name") and item.get("goal_id") is not None
    }
    usable_segments = [
        item for item in segments
        if item.get("path_name") and item.get("goal_id") is not None
        and item.get("transit_start_seconds") is not None
        and (
            item.get("status") in {"failed", "skipped"}
            or item.get("arrived_at_seconds") is not None
        )
    ]
    if usable_segments:
        identities = {
            f"{item.get('path_name')}#{item.get('goal_id')}" for item in usable_segments
        }
        successful = {
            f"{item.get('path_name')}#{item.get('goal_id')}" for item in usable_segments
            if item.get("status") == "arrived"
        }
        failed = identities - successful
        return {
            "evidence": "full", "point_count": len(identities),
            "successful_points": len(successful), "failed_points": len(failed),
            "message": f"已识别 {len(identities)} 个点位 · 信息完整",
        }

    # Older sidecars and hand-authored marker files may identify points without carrying
    # enough timestamps to split transit from dwell.  They still answer the UI's point-count
    # question; the pixel analyser simply keeps its ordinary-scene fallback for the edit.
    if segment_identities:
        successful = {
            f"{item.get('path_name')}#{item.get('goal_id')}" for item in segments
            if item.get("path_name") and item.get("goal_id") is not None
            and item.get("status") == "arrived"
        }
        failed = segment_identities - successful
        return {
            "evidence": "markers_only", "point_count": len(segment_identities),
            "successful_points": len(successful), "failed_points": len(failed),
            "message": f"已识别 {len(segment_identities)} 个点位 · 基础信息",
        }

    usable_markers = [
        marker for marker in markers
        if marker.get("timestamp") is not None and str(marker.get("label") or "").strip()
    ]
    if usable_markers:
        labels = {str(marker.get("label")).strip() for marker in usable_markers}
        return {
            "evidence": "markers_only", "point_count": len(labels),
            "successful_points": len(labels), "failed_points": 0,
            "message": f"已识别 {len(labels)} 个点位 · 基础信息",
        }

    # Manual capture writes the same sidecar shape with empty point arrays. It is valid
    # ordinary footage, not a broken cruise file.
    if not segments and not markers:
        return {
            "evidence": "none", "point_count": 0, "successful_points": 0,
            "failed_points": 0, "message": "未识别点位，将按画面内容剪辑",
        }
    return {
        "evidence": "invalid", "point_count": 0, "successful_points": 0,
        "failed_points": 0, "message": "点位文件没有可用的到达信息",
    }


def inspect_timeline(timeline: list[dict]) -> dict:
    visits = {str(s.get("id", "")).rsplit("-", 1)[0] for s in timeline if s.get("kind") in {"dwell", "transit", "unknown"}}
    parked = {str(s.get("id", "")).rsplit("-", 1)[0] for s in timeline if s.get("kind") == "dwell"}
    failed = {str(s.get("id", "")).rsplit("-", 1)[0] for s in timeline if s.get("kind") in {"failed", "unknown"}}
    return {"evidence": "full" if visits else "none", "point_count": len(visits),
            "successful_points": len(parked), "failed_points": len(failed - parked),
            "message": f"所选内容包含 {len(visits)} 个点位 · 统一录制时间轴"}


class CaptureService:
    def __init__(self, events: EventHub, path: Path | None = None) -> None:
        self.events = events
        self.path = path or generated_path("data", "capture-sessions.json")
        self.load_problem = ""
        self._sessions: dict[str, CaptureSession] = self._load()
        active_ids = [session.id for session in self._sessions.values() if session.active]
        self._active_id: str | None = active_ids[-1] if active_ids else None
        # A valid store should contain at most one active capture. If an older build or an
        # interrupted write left several, retain the newest one and make the in-memory view
        # unambiguous without discarding any session metadata.
        for session_id in active_ids[:-1]:
            self._sessions[session_id].active = False

    def list_sessions(self) -> list[CaptureSession]:
        return list(self._sessions.values())

    def active_session(self) -> CaptureSession | None:
        if not self._active_id:
            return None
        return self._sessions.get(self._active_id)

    async def start(self, title: str = "") -> CaptureSession:
        if self._active_id and self._active_id in self._sessions:
            return self._sessions[self._active_id]
        session = CaptureSession(title=self._session_title(title), active=True, started_at=utc_now())
        previous_sessions = self._sessions.copy()
        self._sessions[session.id] = session
        self._active_id = session.id
        self._trim()
        try:
            self._save()
        except OSError:
            self._sessions = previous_sessions
            self._active_id = next(
                (item.id for item in reversed(list(self._sessions.values())) if item.active),
                None,
            )
            raise
        await self.events.publish("CAPTURE_STARTED", session.model_dump(mode="json"))
        return session

    async def stop(self) -> CaptureSession | None:
        session = self.active_session()
        if not session:
            return None
        previous_ended_at = session.ended_at
        session.active = False
        session.ended_at = utc_now()
        self._active_id = None
        try:
            self._save()
        except OSError:
            session.active = True
            session.ended_at = previous_ended_at
            self._active_id = session.id
            raise
        await self.events.publish("CAPTURE_STOPPED", session.model_dump(mode="json"))
        return session

    async def discard(self) -> CaptureSession | None:
        """Explicitly close an idle unrecoverable capture without deleting any robot file."""
        session = self.active_session()
        if session is None:
            return None
        previous = session.model_copy(deep=True)
        session.active = False
        session.ended_at = utc_now()
        session.gimbal_samples = []
        session.pending_media_url = None
        session.pending_media_local_path = None
        session.pending_media_sync_error = None
        self._active_id = None
        try:
            self._save()
        except OSError:
            self._sessions[session.id] = previous
            self._active_id = session.id
            raise
        await self.events.publish("CAPTURE_DISCARDED", session.model_dump(mode="json"))
        return session

    def remember_segments(
        self,
        session: CaptureSession,
        segments: list[CruiseSegment],
    ) -> None:
        """Persist a cruise's spans before its recording has necessarily synced locally."""
        stored = self._sessions.get(session.id)
        if stored is None:
            return
        previous = stored.segments
        stored.segments = [segment.model_dump(mode="json") for segment in segments]
        try:
            self._save()
        except OSError:
            stored.segments = previous
            raise

    def remember_recording_event(self, session: CaptureSession, event: dict) -> None:
        """Persist only capture transitions, never the high-frequency heartbeat stream."""
        session.recording_events.append(dict(event))
        # Keep the in-memory evidence even if disk is temporarily unavailable. A later
        # transition/finalization can persist it; recording shutdown must still proceed.
        self._save()

    def remember_recording_clock(self, session: CaptureSession, **values) -> None:
        session.recording_clock.update(values)
        self._save()

    def remember_gimbal_samples(
        self,
        session: CaptureSession,
        samples: list[tuple[float, ...]],
    ) -> None:
        """Persist measured camera movement until the recording is safely attached."""
        stored = self._sessions.get(session.id)
        if stored is None:
            return
        previous = stored.gimbal_samples
        stored.gimbal_samples = [tuple(float(value) for value in sample) for sample in samples]
        try:
            self._save()
        except OSError:
            stored.gimbal_samples = previous
            raise

    def remember_pending_media(
        self,
        session: CaptureSession,
        media_url: str | None,
        sync_error: str | None = None,
        local_path: str | None = None,
    ) -> None:
        """Keep a retryable video URL with an unfinished capture, including across restarts."""
        stored = self._sessions.get(session.id)
        if stored is None:
            return
        previous_url = stored.pending_media_url
        previous_local_path = stored.pending_media_local_path
        previous_error = stored.pending_media_sync_error
        candidate = str(media_url or "").strip()
        suffix = Path(urlsplit(candidate).path).suffix.casefold() if candidate else ""
        if candidate and suffix not in {
            ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff",
        }:
            stored.pending_media_url = candidate
        if local_path:
            stored.pending_media_local_path = str(local_path)
        stored.pending_media_sync_error = str(sync_error or "").strip() or None
        try:
            self._save()
        except OSError:
            stored.pending_media_url = previous_url
            stored.pending_media_local_path = previous_local_path
            stored.pending_media_sync_error = previous_error
            raise

    def clear_pending_media(self, session: CaptureSession) -> None:
        """Drop recovery pointers only after attachment and inactive state both committed."""
        stored = self._sessions.get(session.id)
        if stored is None:
            return
        previous = (
            stored.pending_media_url,
            stored.pending_media_local_path,
            stored.pending_media_sync_error,
        )
        stored.pending_media_url = None
        stored.pending_media_local_path = None
        stored.pending_media_sync_error = None
        try:
            self._save()
        except OSError:
            (
                stored.pending_media_url,
                stored.pending_media_local_path,
                stored.pending_media_sync_error,
            ) = previous
            raise

    async def complete_with_recording(
        self,
        video_path: str,
        segments: list[CruiseSegment] | None = None,
    ) -> CaptureSession | None:
        """Attach all metadata before making an unfinished capture disappear from recovery UI."""
        session = self.active_session()
        if session is None:
            return None
        if self.attach_to_recording(session, video_path, segments) is None:
            raise OSError("视频已保存，但拍摄信息写入失败，请重试保存")
        stopped = await self.stop()
        if stopped is not None:
            try:
                self.clear_pending_media(stopped)
            except OSError:
                # Inactive is already durable and the sidecars are complete. Retaining a small
                # recovery pointer is harmless and safer than reporting the video as lost.
                pass
        return stopped

    def attach_to_recording(
        self,
        session: CaptureSession,
        video_path: str,
        segments: list[CruiseSegment] | None = None,
    ) -> Path | None:
        """Write a session's notes, markers and cruise spans beside the recording.

        Only called for a recording that actually produced a file, so a failed run leaves
        no stray sidecar claiming to describe footage that does not exist.

        Markers alone record the instant of each arrival and nothing else, which leaves the
        editor unable to tell a parked shot from a moving one: there is no departure marker,
        so a dwell has a start but no end. The segments carry both edges of every span plus
        the point's outcome, which is what lets planning treat travelling footage differently
        from footage shot standing still. A manual capture passes none and keeps the old shape.
        """
        video = Path(video_path)
        if not video.exists():
            return None

        serialized_segments = (
            [segment.model_dump(mode="json") for segment in segments]
            if segments is not None
            else [dict(segment) for segment in session.segments]
        )
        payload = {
            "capture_session_id": session.id,
            "title": session.title,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "ended_at": (session.ended_at or utc_now()).isoformat(),
            "notes": list(session.notes),
            "markers": [marker.model_dump(mode="json") for marker in session.markers],
            "segments": serialized_segments,
            "recording_events": list(session.recording_events),
            "recording_clock": dict(session.recording_clock),
        }
        # Publish the capture document last: CaptureLibrary treats it as the enrollment marker.
        # If motion telemetry cannot be persisted, leaving only a gimbal sidecar is harmless and
        # retryable; publishing capture first would let the background splitter create children
        # without the physical-motion evidence that was actually collected.
        if session.gimbal_samples and not write_json(
                gimbal_sidecar_path(video),
                {"samples": [list(sample) for sample in session.gimbal_samples]},
        ):
            return None
        target = sidecar_path(video)
        if not write_json(target, payload):
            return None

        # The durable sidecars now own the high-volume motion track. Do not retain hundreds of
        # thousands of samples in the 500-session index after a successful attachment.
        stored = self._sessions.get(session.id)
        if stored is not None:
            stored.gimbal_samples = []
            try:
                self._save()
            except OSError:
                # Both sidecars and the inactive/active state were committed separately; a
                # cleanup failure must not pretend the recording itself was lost.
                pass
        return target

    def _session_title(self, title: str) -> str:
        """Stamp every session with its local start time: '产品晨拍 08-04 17-20'.

        Applied here so manual and cruise sessions are named identically. A cruise passes
        its 清单 name; stamping at start time keeps a saved 清单 from replaying an old one.
        The time uses '-' rather than ':' because this title becomes a filename: capture
        groups are named after it and compositions saved from them are named after that.
        """
        stamp = utc_now().astimezone().strftime("%m-%d %H-%M")
        name = title.strip() or "采集"
        return self._unique_title(f"{name} {stamp}")

    def _unique_title(self, title: str) -> str:
        """Break ties between sessions started inside the same minute."""
        taken = {session.title for session in self._sessions.values()}
        if title not in taken:
            return title
        suffix = 2
        while f"{title}-{suffix}" in taken:
            suffix += 1
        return f"{title}-{suffix}"

    async def add_marker(self, timestamp: float, label: str) -> TimelineMarker | None:
        """Index a moment in the running recording, e.g. the instant a cruise point was reached.

        Scene detection cannot recover these boundaries from pixels when the robot glides
        between visually similar points, so they are recorded from robot state instead.
        """
        session = self.active_session()
        if not session:
            return None
        marker = TimelineMarker(timestamp=max(0.0, timestamp), label=label)
        session.markers.append(marker)
        try:
            self._save()
        except OSError:
            session.markers.pop()
            raise
        await self.events.publish(
            "CAPTURE_MARKER",
            {"session_id": session.id, "marker": marker.model_dump(mode="json")},
        )
        return marker

    async def add_note(self, note: str) -> CaptureSession | None:
        session = self.active_session()
        if not session:
            return None
        session.notes.append(note)
        try:
            self._save()
        except OSError:
            session.notes.pop()
            raise
        await self.events.publish("CAPTURE_NOTE", {"session_id": session.id, "note": note})
        return session

    def _trim(self) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            self._sessions.pop(next(iter(self._sessions)))

    def _load(self) -> dict[str, CaptureSession]:
        raw, problem = read_json(self.path)
        if problem:
            # Someone's notes and a run's markers, not an absence. Recorded rather than
            # silently becoming an empty store that the next save would make permanent.
            self.load_problem = problem
        if not isinstance(raw, list):
            return {}

        sessions: dict[str, CaptureSession] = {}
        for entry in raw:
            # Every field has a default, so an unrecognised object would otherwise load as
            # a blank "Untitled capture" rather than being rejected.
            if not isinstance(entry, dict) or not entry.get("id") or not entry.get("title"):
                continue
            try:
                session = CaptureSession(**entry)
            except (ValidationError, TypeError):
                continue
            # An active session may be waiting for a camera transfer retry. Preserve that
            # ownership across a backend restart so its notes, point spans and gimbal samples
            # remain attached to the eventual recording instead of becoming orphaned.
            sessions[session.id] = session
        return sessions

    def _save(self) -> None:
        if self.load_problem:
            try:
                self.path.stat()
            except FileNotFoundError:
                # Invalid JSON is quarantined by read_json(), so creating a clean replacement
                # is safe once the original path is gone and its bytes have been preserved.
                pass
            except OSError:
                # If even existence cannot be checked, fail closed rather than gambling with
                # the recovery record that may still own a recording on the robot.
                raise OSError(
                    f"{self.load_problem}；为避免覆盖原有采集记录，已阻止写入"
                ) from None
            else:
                raise OSError(
                    f"{self.load_problem}；为避免覆盖原有采集记录，已阻止写入"
                )
        saved = write_json(
            self.path,
            [session.model_dump(mode="json") for session in self._sessions.values()],
        )
        if not saved:
            raise OSError(f"无法保存采集会话：{self.path}")
        self.load_problem = ""
