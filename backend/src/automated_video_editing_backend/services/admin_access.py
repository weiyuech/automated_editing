from __future__ import annotations

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException

# This is a local operator gate, not remote identity management. Keep the password itself out of
# both the renderer bundle and the frozen Python strings; only its deliberately slow verifier is
# shipped. A determined owner of the installer can still patch the application, so this protects
# credentials from casual access at the workstation, not from reverse engineering.
ADMIN_USERNAME = "user777"
PASSWORD_SALT = b"ave-settings-admin-v1"
PASSWORD_ROUNDS = 310_000
PASSWORD_DIGEST = bytes.fromhex(
    "e30c18f1f473582920c3d9beaacf2458f599ee1fdb7b220c23c68af9af9a9f49"
)
SESSION_SECONDS = 8 * 60 * 60
MAX_FAILURES = 5
LOCKOUT_SECONDS = 30


class AdminAccessService:
    """Issue process-local tokens after the packaged administrator check succeeds."""

    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._failures = 0
        self._locked_until = 0.0

    def unlock(self, username: str, password: str) -> dict[str, int | str]:
        now = time.monotonic()
        self._prune(now)
        if now < self._locked_until:
            wait = max(1, int(self._locked_until - now + 0.999))
            raise HTTPException(
                status_code=429,
                detail=f"管理员验证尝试过多，请在 {wait} 秒后重试",
            )

        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            PASSWORD_SALT,
            PASSWORD_ROUNDS,
        )
        username_ok = hmac.compare_digest(username, ADMIN_USERNAME)
        password_ok = hmac.compare_digest(candidate, PASSWORD_DIGEST)
        if not (username_ok and password_ok):
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                self._locked_until = now + LOCKOUT_SECONDS
                self._failures = 0
            raise HTTPException(status_code=401, detail="管理员账号或密码不正确")

        self._failures = 0
        self._locked_until = 0.0
        token = secrets.token_urlsafe(32)
        self._sessions[token] = now + SESSION_SECONDS
        return {"token": token, "expires_in_seconds": SESSION_SECONDS}

    def require(self, token: str | None) -> None:
        now = time.monotonic()
        self._prune(now)
        if not token or token not in self._sessions:
            raise HTTPException(status_code=403, detail="请先通过管理员验证")

    def status(self, token: str | None) -> dict[str, bool]:
        try:
            self.require(token)
        except HTTPException:
            return {"unlocked": False}
        return {"unlocked": True}

    def lock(self, token: str | None) -> None:
        if token:
            self._sessions.pop(token, None)

    def _prune(self, now: float) -> None:
        expired = [token for token, expiry in self._sessions.items() if expiry <= now]
        for token in expired:
            self._sessions.pop(token, None)
