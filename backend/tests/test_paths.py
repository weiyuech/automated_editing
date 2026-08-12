from pathlib import Path

import pytest

from automated_video_editing_backend.core.paths import APP_ROOT, RootPathError, ensure_inside_root


def test_allows_inside_root():
    assert ensure_inside_root(APP_ROOT / "exports" / "demo.mp4")


def test_rejects_outside_root():
    with pytest.raises(RootPathError):
        ensure_inside_root(Path("/tmp/outside-demo.mp4"))
