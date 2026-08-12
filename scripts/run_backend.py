from __future__ import annotations

import os
import secrets
import subprocess
import sys

from safety import APP_ROOT, ensure_runtime_dirs


def main() -> int:
    ensure_runtime_dirs()
    env = os.environ.copy()
    env.setdefault("APP_ROOT", str(APP_ROOT))
    env.setdefault("APP_BACKEND_HOST", "127.0.0.1")
    env.setdefault("APP_BACKEND_PORT", "4817")
    env.setdefault("APP_BRIDGE_TOKEN", secrets.token_urlsafe(32))
    env.setdefault("NUMBA_CACHE_DIR", str(APP_ROOT / ".cache" / "numba"))
    env["PYTHONPATH"] = str(APP_ROOT / "backend" / "src") + os.pathsep + env.get("PYTHONPATH", "")
    cmd = [sys.executable, "-m", "automated_video_editing_backend.main"]
    return subprocess.call(cmd, cwd=str(APP_ROOT), env=env)


if __name__ == "__main__":
    raise SystemExit(main())
