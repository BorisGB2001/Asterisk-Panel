"""Administrator sign-in: password (scrypt) + TOTP, brute-force protection, CSRF (taken from awg-panel)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque

import pyotp

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    # no "$": shells and env-file loaders would expand it and break the hash
    return f"scrypt:{SCRYPT_N}:{SCRYPT_R}:{SCRYPT_P}:{b64(salt)}:{b64(dk)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, want = stored.replace("$", ":").split(":")
        if algo != "scrypt":
            return False
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                            maxmem=64 * 1024 * 1024, dklen=len(base64.b64decode(want)))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(want))


# hash of a random password: checking an unknown login takes as long as a real one
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def totp_step(secret: str, code: str, last_step: int, now: float | None = None) -> int | None:
    """TOTP step matching the code (window ±1 step) if it is newer than last_step; otherwise None."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != 6:
        return None
    totp = pyotp.TOTP(secret)
    now = time.time() if now is None else now
    step = int(now // totp.interval)
    for s in (step - 1, step, step + 1):
        if s > last_step and hmac.compare_digest(totp.at(s * totp.interval), code):
            return s
    return None


class TOTPGuard:
    """TOTP check with a ±1 step window; the same code cannot be used twice."""

    def __init__(self, secret: str):
        self.secret = secret
        self._last_step = -1
        self._lock = threading.Lock()

    def verify(self, code: str, now: float | None = None) -> bool:
        with self._lock:
            step = totp_step(self.secret, code, self._last_step, now)
            if step is None:
                return False
            self._last_step = step
            return True


class RateLimiter:
    """5 failures from one IP in 15 min → that IP blocked for 15 min; >30 failures per hour from all IPs → sign-in blocked for everyone for 15 min."""

    def __init__(self, per_ip: int = 5, window: int = 900, block: int = 900, global_limit: int = 30, global_window: int = 3600):
        self.per_ip, self.window, self.block = per_ip, window, block
        self.global_limit, self.global_window = global_limit, global_window
        self._fails: dict[str, deque[float]] = defaultdict(deque)
        self._blocked: dict[str, float] = {}
        self._global: deque[float] = deque()
        self._global_until = 0.0
        self._lock = threading.Lock()

    def blocked(self, ip: str, now: float | None = None) -> int:
        """Seconds the block still lasts (0 — may try)."""
        now = time.time() if now is None else now
        with self._lock:
            until = max(self._blocked.get(ip, 0.0), self._global_until)
            return max(0, int(until - now))

    def fail(self, ip: str, now: float | None = None) -> bool:
        """Counts a failure; True if the global block has just been triggered."""
        now = time.time() if now is None else now
        with self._lock:
            q = self._fails[ip]
            q.append(now)
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.per_ip:
                self._blocked[ip] = now + self.block
                q.clear()
            self._global.append(now)
            while self._global and self._global[0] < now - self.global_window:
                self._global.popleft()
            if len(self._global) > self.global_limit and self._global_until <= now:
                self._global_until = now + self.block
                self._global.clear()
                return True
            return False

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def new_csrf() -> str:
    return secrets.token_urlsafe(32)


def ua_fingerprint(user_agent: str) -> str:
    return hashlib.sha256(user_agent.encode()).hexdigest()[:16]
