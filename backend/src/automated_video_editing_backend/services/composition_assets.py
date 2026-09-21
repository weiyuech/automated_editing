"""Durable composition provenance and one active narration binding, beside the video."""

from datetime import datetime
from pathlib import Path

from automated_video_editing_backend.core.store import read_json, write_json


def manifest_path(path: str | Path) -> Path:
    return Path(str(path) + ".composition.json")


def read_manifest(path: str | Path) -> dict:
    raw, _ = read_json(manifest_path(path))
    if not isinstance(raw, dict) or raw.get("version") != 1 or not raw.get("composition_id"):
        return {}
    return raw


def save_manifest(path: str | Path, metadata: dict) -> None:
    if not write_json(manifest_path(path), {**metadata, "version": 1}):
        raise OSError("组合的来源与旁白绑定无法保存")


def enrich(items) -> None:
    """Resolve durable binding keys to current media IDs; IDs can change after a restart."""
    voices = {}
    for item in items:
        if item.kind == "audio" and item.metadata.get("role") == "tts_voice":
            raw, _ = read_json(Path(item.path).with_suffix(".json"))
            if isinstance(raw, dict):
                for key in (
                    "binding_id",
                    "composition_id",
                    "visual_signature",
                    "whole_audio",
                    "duration_ms",
                    "timing_quality",
                    "narration_status",
                    "narration_style",
                    "created_at",
                ):
                    if key in raw:
                        item.metadata[key] = raw[key]
                item.metadata["metadata_path"] = str(Path(item.path).with_suffix(".json"))
            item.metadata.pop("bound_source_id", None)
            if item.metadata.get("binding_id"):
                voices[item.metadata["binding_id"]] = item
    for item in items:
        if item.kind != "video":
            continue
        # Scanner IDs and insertion times change after restart; ordering must not.
        started = (item.metadata.get("capture_group") or {}).get("started_at")
        try:
            item.metadata["composition_order"] = (
                datetime.fromisoformat(started).timestamp()
                if started
                else Path(item.path).stat().st_mtime
            )
        except (OSError, TypeError, ValueError):
            item.metadata["composition_order"] = item.created_at.timestamp()
        metadata = read_manifest(item.path)
        if not metadata:
            continue
        item.metadata.update(metadata)
        item.metadata.pop("bound_voice_id", None)
        item.metadata["bound_voice_missing"] = False
        voice = voices.get(metadata.get("narration_binding_id"))
        if voice and voice.metadata.get("composition_id") == metadata["composition_id"]:
            item.metadata["bound_voice_id"] = voice.id
            voice.metadata["bound_source_id"] = item.id
        elif metadata.get("narration_binding_id"):
            item.metadata["bound_voice_missing"] = True
