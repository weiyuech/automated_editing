"""Catch mixed-version installers before spending time freezing the backend."""

import json
import tomllib
from pathlib import Path

from automated_video_editing_backend import __version__


def test_runtime_and_both_installers_match_backend_distribution():
    root = Path(__file__).resolve().parents[2]
    version = tomllib.loads((root / "backend/pyproject.toml").read_text())["project"]["version"]
    frontend = json.loads((root / "frontend/package.json").read_text())
    external = json.loads((root / "frontend/electron-builder.external.json").read_text())
    assert __version__ == frontend["version"] == external["extraMetadata"]["version"] == version
