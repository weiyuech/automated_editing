"""Reject media components that do not belong in the external-tools backend bundle."""

from __future__ import annotations

import sys
from pathlib import Path

FORBIDDEN_EXECUTABLES = {"ffmpeg.exe", "ffprobe.exe"}
FORBIDDEN_FILE_FRAGMENTS = ("libx264", "libx265", "x264-", "x265-")
FORBIDDEN_NATIVE_MARKERS = (
    b"--enable-gpl",
    b"--enable-nonfree",
    b"libx264",
    b"libx265",
    b"x264_encoder_open",
    b"x265_api_get",
)
NATIVE_SUFFIXES = {".dll", ".pyd", ".so", ".dylib"}


def audit(bundle: Path) -> tuple[int, list[str]]:
    files = [path for path in bundle.rglob("*") if path.is_file()]
    problems: list[str] = []
    for path in files:
        relative = path.relative_to(bundle)
        lowered_name = path.name.lower()
        if lowered_name in FORBIDDEN_EXECUTABLES:
            problems.append(f"standalone media tool: {relative}")
        if any(fragment in lowered_name for fragment in FORBIDDEN_FILE_FRAGMENTS):
            problems.append(f"GPL codec library name: {relative}")
        if "av" in {part.lower() for part in relative.parts[:-1]}:
            problems.append(f"PyAV package path: {relative}")
        if path.suffix.lower() not in NATIVE_SUFFIXES:
            continue
        payload = path.read_bytes().lower()
        found = [marker.decode("ascii") for marker in FORBIDDEN_NATIVE_MARKERS if marker in payload]
        if found:
            problems.append(f"native GPL/nonfree marker {', '.join(found)}: {relative}")
    return len(files), problems


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: audit_external_bundle.py <frozen-backend-directory>", file=sys.stderr)
        return 2
    bundle = Path(sys.argv[1]).resolve()
    if not bundle.is_dir():
        print(f"bundle is not a directory: {bundle}", file=sys.stderr)
        return 2
    count, problems = audit(bundle)
    if problems:
        print("External-tools binary audit failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"External-tools binary audit passed ({count} files inspected).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
