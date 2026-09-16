"""Create the external-tools source delivery from an explicit source allowlist."""

from __future__ import annotations

import hashlib
import re
import shutil
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION = tomllib.loads((ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
DESTINATION = ROOT / f"Automated-Video-Editing-{VERSION}-External-Media-Tools-Source"

FILES = {
    "SOURCE_DELIVERY_README.md": "README.md",
    "THIRD_PARTY_NOTICES_EXTERNAL.md": "THIRD_PARTY_NOTICES.md",
    "backend/pyproject.toml": "backend/pyproject.toml",
    "backend/uv.lock": "backend/uv.lock",
    "backend/windows_backend.spec": "backend/windows_backend.spec",
    "frontend/electron.vite.config.mjs": "frontend/electron.vite.config.mjs",
    "frontend/electron-builder.external.json": "frontend/electron-builder.external.json",
    "frontend/package.json": "frontend/package.json",
    "frontend/pnpm-lock.yaml": "frontend/pnpm-lock.yaml",
    "frontend/pnpm-workspace.yaml": "frontend/pnpm-workspace.yaml",
    "frontend/build/icon.png": "frontend/build/icon.png",
    "frontend/build/icon.svg": "frontend/build/icon.svg",
    "frontend/build/external-media-tools.json": "frontend/build/external-media-tools.json",
    "tools/robot-control-console.html": "tools/robot-control-console.html",
    "tools/tests/robot-control-console.test.mjs": "tools/tests/robot-control-console.test.mjs",
}

TREES = (
    ("backend/src", "backend/src"),
    ("backend/tests", "backend/tests"),
    ("frontend/src", "frontend/src"),
    ("frontend/tests", "frontend/tests"),
    ("docs", "docs"),
)

SCRIPT_FILES = (
    "Collect-AVE-RobotDiagnostics.ps1",
    "Probe-AVE-RobotRecording.ps1",
    "audit_external_bundle.py",
    "prepare_assets.py",
    "robot_bringup.py",
    "robot_probe_standalone.py",
    "run_backend.py",
    "safety.py",
    "smoke_check.py",
    "smoke_frozen_backend.py",
)

EXCLUDED_PARTS = {
    ".git",
    ".github",
    ".claude",
    ".codex",
    ".agents",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "node_modules",
    "vendor",
    "fonts",
    "semantic",
    "font_previews",
}

SECRET_PATTERNS = {
    "GitHub token": re.compile(rb"gh[pousr]_[A-Za-z0-9_]{20,}"),
    "API token": re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}"),
    "Volcengine access key": re.compile(rb"\bAKLT[A-Za-z0-9]{12,}"),
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}

# Product features such as semantic matching remain in source.  These terms identify development
# assistant metadata or authorship claims, which do not belong in a neutral source delivery.
ASSISTANT_TRACE_PATTERNS = {
    "Codex metadata": re.compile(rb"\bcodex\b", re.IGNORECASE),
    "Claude metadata": re.compile(rb"\bclaude\b", re.IGNORECASE),
    "ChatGPT metadata": re.compile(rb"\bchatgpt\b", re.IGNORECASE),
    "Copilot metadata": re.compile(rb"\bcopilot\b", re.IGNORECASE),
    "assistant authorship claim": re.compile(
        rb"(?:generated|written|created)[ -]by[ -](?:an?[ -])?(?:ai|llm|assistant)",
        re.IGNORECASE,
    ),
}


def should_copy(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    if any(part in EXCLUDED_PARTS or part.endswith(".egg-info") for part in relative.parts):
        return False
    return path.is_file() and path.suffix.lower() not in {".pyc", ".pyo", ".onnx", ".ttf", ".woff2"}


def copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def build_delivery() -> None:
    if DESTINATION.exists():
        raise SystemExit(f"Refusing to overwrite existing delivery: {DESTINATION}")
    DESTINATION.mkdir()

    for source_name, destination_name in FILES.items():
        source = ROOT / source_name
        if not source.is_file():
            raise SystemExit(f"Required source file is missing: {source_name}")
        copy_file(source, DESTINATION / destination_name)

    for source_name, destination_name in TREES:
        source_root = ROOT / source_name
        if not source_root.is_dir():
            raise SystemExit(f"Required source tree is missing: {source_name}")
        sources = [source for source in sorted(source_root.rglob("*")) if should_copy(source)]
        if not sources:
            raise SystemExit(f"Required source tree is empty: {source_name}")
        for source in sources:
            copy_file(source, DESTINATION / destination_name / source.relative_to(source_root))

    for name in SCRIPT_FILES:
        copy_file(ROOT / "scripts" / name, DESTINATION / "scripts" / name)

    # Electron Builder resolves this original filename from its configuration. Retain it as
    # well as the reader-facing THIRD_PARTY_NOTICES.md so the delivery can actually rebuild.
    copy_file(ROOT / "THIRD_PARTY_NOTICES_EXTERNAL.md", DESTINATION / "THIRD_PARTY_NOTICES_EXTERNAL.md")


def audit_delivery() -> list[Path]:
    files = sorted(path for path in DESTINATION.rglob("*") if path.is_file())
    problems: list[str] = []
    for path in files:
        relative = path.relative_to(DESTINATION)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            problems.append(f"blocked path: {relative}")
            continue
        payload = path.read_bytes()
        for label, pattern in {**SECRET_PATTERNS, **ASSISTANT_TRACE_PATTERNS}.items():
            if pattern.search(payload):
                problems.append(f"{label}: {relative}")
    if problems:
        raise SystemExit("Source delivery audit failed:\n- " + "\n- ".join(problems))
    return files


def write_manifest(files: list[Path]) -> None:
    lines = []
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.relative_to(DESTINATION).as_posix()}")
    (DESTINATION / "SOURCE_MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    build_delivery()
    files = audit_delivery()
    write_manifest(files)
    print(f"Created {DESTINATION} ({len(files)} source files plus manifest)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
