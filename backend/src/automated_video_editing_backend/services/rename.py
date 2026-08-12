from __future__ import annotations

import json
from pathlib import Path

from automated_video_editing_backend.core.models import MediaItem
from automated_video_editing_backend.services.jobs import JobService
from automated_video_editing_backend.services.media import MediaService
from automated_video_editing_backend.services.capture import sidecar_path
from automated_video_editing_backend.services.naming import validate_filename
from automated_video_editing_backend.services.seedance import SeedanceService


class MediaRenameService:
    """Renames a generated file and everything that points at it.

    The rename itself is trivial; the care is in the references. A file left renamed with a
    stale pointer somewhere shows up later as a mysteriously missing clip.
    """

    def __init__(self, media: MediaService, jobs: JobService, seedance: SeedanceService) -> None:
        self.media = media
        self.jobs = jobs
        self.seedance = seedance

    def rename(self, media_id: str, new_name: str) -> MediaItem:
        item = self.media.get(media_id)
        if item is None:
            raise ValueError("That media item no longer exists")

        source = Path(item.path)
        if not source.is_file():
            raise ValueError("That file no longer exists on disk")

        filename = validate_filename(new_name, source.suffix)
        target = source.with_name(filename)
        if target == source:
            return item
        if target.exists():
            raise ValueError(f"'{filename}' already exists here")
        if self.jobs.is_path_in_use(str(source)):
            raise ValueError("That file is being used by a render right now")

        # The TTS sidecar is paired by filename, so it has to move in step.
        tts_metadata = source.with_suffix(".json")
        tts_target = target.with_suffix(".json")
        move_tts_metadata = (
            item.metadata.get("role") == "tts_voice"
            and tts_metadata.is_file()
            and not tts_target.exists()
        )

        # A recording's notes and markers live in "<full filename>.capture.json", so they
        # are orphaned unless the sidecar moves with the video.
        capture_sidecar = sidecar_path(source)
        capture_target = sidecar_path(target)
        move_capture_sidecar = capture_sidecar.is_file() and not capture_target.exists()

        # Export subtitle layers are paired by stem as well. Keeping them beside both the
        # delivery and its clean master is what lets 手动微调 rediscover the timing after a
        # restart or a rename, rather than depending on transient in-memory metadata.
        export_sidecars = []
        if item.metadata.get("role") == "export":
            for suffix in (".subtitles.json", ".ass"):
                paired = source.with_suffix(suffix)
                paired_target = target.with_suffix(suffix)
                if paired.is_file() and not paired_target.exists():
                    export_sidecars.append((paired, paired_target))

        source.rename(target)
        if move_tts_metadata:
            tts_metadata.rename(tts_target)
        if move_capture_sidecar:
            capture_sidecar.rename(capture_target)
        for paired, paired_target in export_sidecars:
            paired.rename(paired_target)
            if item.metadata.get("subtitles_path") == str(paired):
                item.metadata["subtitles_path"] = str(paired_target)

        self._update_seedance_metadata(str(source), target)
        self._update_finished_jobs(str(source), target)
        self.media.repoint(media_id, str(target))
        return self.media.get(media_id) or item

    def _update_finished_jobs(self, old_path: str, target: Path) -> None:
        """Keep 渲染队列 pointing at the export after it is renamed."""
        for job in self.jobs.list_jobs():
            if job.result_path == old_path:
                job.result_path = str(target)
            timeline = job.timeline
            if timeline is not None and getattr(timeline, "output_path", None) == old_path:
                timeline.output_path = str(target)

    def _update_seedance_metadata(self, old_path: str, target: Path) -> None:
        """Effects are paired by a field inside their .json, not by filename."""
        for metadata_path in self.seedance.effects_dir.glob("*.json"):
            try:
                data = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("output_path") != old_path and data.get("video_path") != old_path:
                continue
            data["output_path"] = str(target)
            data.pop("video_path", None)
            data["name"] = target.name
            try:
                metadata_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            except OSError:
                pass
            return
