"""Launch a release backend in an empty profile and verify its local HTTP service."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import tomllib


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("provide the backend executable or Python module command")
    root = Path(__file__).resolve().parent.parent
    version = tomllib.loads(
        (root / "backend/pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = os.environ.copy()
    env.update(
        {
            "APP_BACKEND_HOST": "127.0.0.1",
            "APP_BACKEND_PORT": str(port),
            "APP_BRIDGE_TOKEN": token,
            "AVE_ROBOT_WEBSOCKET_URL": "",
            "ROBOT_WEBSOCKET_URL": "",
            "APP_MANAGED_BY_ELECTRON": "1",
        }
    )
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="ave-release-smoke-") as profile:
        env["APP_ROOT"] = profile
        env["NUMBA_CACHE_DIR"] = str(Path(profile) / "numba")
        with tempfile.TemporaryFile(mode="w+b") as log:
            process = subprocess.Popen(
                args.command,
                cwd=root,
                env=env,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 90
                while True:
                    if process.poll() is not None:
                        raise RuntimeError(
                            f"Backend exited during startup: {process.returncode}"
                        )
                    try:
                        request = Request(
                            f"{base}/api/health", headers={"x-bridge-token": token}
                        )
                        with urlopen(request, timeout=2) as response:
                            health = json.load(response)
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError(
                                "Backend did not become healthy within 90 seconds"
                            )
                        time.sleep(0.25)
                assert health == {
                    "ok": "true",
                    "service": "automated-video-editing-backend",
                }, health
                with urlopen(f"{base}/openapi.json", timeout=5) as response:
                    actual_version = json.load(response)["info"]["version"]
                assert actual_version == version, (actual_version, version)
                try:
                    with urlopen(f"{base}/api/health", timeout=5):
                        raise AssertionError(
                            "Backend accepted an unauthenticated API request"
                        )
                except HTTPError as exc:
                    assert exc.code == 401, exc.code
                assert process.stdin is not None
                process.stdin.write(b"shutdown\n")
                process.stdin.flush()
                process.stdin.close()
                returncode = process.wait(timeout=60)
                assert returncode == 0, returncode
                print(
                    f"Backend {version} starts, serves health, and rejects requests "
                    "without its bridge token."
                )
            except Exception:
                log.seek(0)
                print(log.read().decode("utf-8", errors="replace")[-8000:])
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
