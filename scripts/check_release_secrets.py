"""Fail a release build if a credential or local-settings file entered source control."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BLOCKED_PATHS = (
    ".claude/",
    ".env",
    "data/",
    "直播控制系统/",
)
TOKEN_PATTERNS = {
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "OpenAI-style API key": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    "Volcengine access key": re.compile(rb"\bAKLT[A-Za-z0-9]{12,}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        capture_output=True,
        check=True,
    )
    return [part.decode("utf-8") for part in result.stdout.split(b"\0") if part]


def main() -> int:
    problems: list[str] = []
    for relative in tracked_files():
        normalized = relative.replace("\\", "/")
        if normalized == ".env" or any(normalized.startswith(prefix) for prefix in BLOCKED_PATHS):
            problems.append(f"blocked local path is tracked: {relative}")
            continue

        path = ROOT / relative
        try:
            payload = path.read_bytes()
        except OSError as exc:
            problems.append(f"could not inspect {relative}: {exc}")
            continue
        # Binary dependencies are generated in CI and are never tracked. Avoid decoding or
        # printing file contents here; the scanner reports only the safe filename and rule.
        for label, pattern in TOKEN_PATTERNS.items():
            if pattern.search(payload):
                problems.append(f"{label} detected in {relative}")

    if problems:
        print("Release secret check failed:")
        for problem in problems:
            print(f"- {problem}")
        return 1
    print(f"Release secret check passed ({len(tracked_files())} tracked files inspected).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
