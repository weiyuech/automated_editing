from __future__ import annotations

import json
from pathlib import Path

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


class CaptureService:
    def __init__(self, events: EventHub, path: Path | None = None) -> None:
        self.events = events
        self.path = path or generated_path("data", "capture-sessions.json")
        self.load_problem = ""
        self._sessions: dict[str, CaptureSession] = self._load()
        self._active_id: str | None = None

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
        self._sessions[session.id] = session
        self._active_id = session.id
        self._trim()
        self._save()
        await self.events.publish("CAPTURE_STARTED", session.model_dump(mode="json"))
        return session

    async def stop(self) -> CaptureSession | None:
        session = self.active_session()
        if not session:
            return None
        session.active = False
        session.ended_at = utc_now()
        self._active_id = None
        self._save()
        await self.events.publish("CAPTURE_STOPPED", session.model_dump(mode="json"))
        return session

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

        payload = {
            "capture_session_id": session.id,
            "title": session.title,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "ended_at": session.ended_at.isoformat() if session.ended_at else None,
            "notes": list(session.notes),
            "markers": [marker.model_dump(mode="json") for marker in session.markers],
            "segments": [segment.model_dump(mode="json") for segment in (segments or [])],
        }
        target = sidecar_path(video)
        return target if write_json(target, payload) else None

    def _session_title(self, title: str) -> str:
        """Stamp every session with its local start time: '产品晨拍 08-04 17:20'.

        Applied here so manual and cruise sessions are named identically. A cruise passes
        its 清单 name; stamping at start time keeps a saved 清单 from replaying an old one.
        """
        stamp = utc_now().astimezone().strftime("%m-%d %H:%M")
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
        self._save()
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
        self._save()
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
            # Nothing can still be recording after a restart.
            session.active = False
            sessions[session.id] = session
        return sessions

    def _save(self) -> None:
        write_json(self.path, [session.model_dump(mode="json") for session in self._sessions.values()])
