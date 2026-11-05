"""Portal authentication: first-visitor password setup, argon2 hashes,
server-side sessions with CSRF tokens, login rate limiting, and the
``PORTAL_RESET`` password-reset flow (with a one-time console token)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

from ..store import Store

SESSION_TTL = 30 * 86400
COOKIE = "markstile_session"
MIN_PASSWORD = 10

_ph = PasswordHasher()


def _h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Auth:
    def __init__(self, store: Store) -> None:
        self.store = store
        self._fails: dict[str, deque] = defaultdict(deque)

    # -- state ------------------------------------------------------------------
    def has_password(self) -> bool:
        return bool(self.store.get("auth.password_hash"))

    def reset_pending(self) -> bool:
        return bool(self.store.get("auth.reset_token"))

    def needs_setup(self) -> bool:
        return not self.has_password() or self.reset_pending()

    def check_reset_env(self, value: str | None, require_token: bool = True) -> str | None:
        """Call at startup with $PORTAL_RESET.  If its value changed since the
        last start, enter reset mode.  Returns the one-time token to print to
        the container log (or "" if no token is required)."""
        if not value:
            return None
        seen = self.store.get("auth.reset_seen")
        if seen == _h(value):
            return None
        self.store.set("auth.reset_seen", _h(value))
        if not self.has_password():
            return None
        token = secrets.token_hex(4).upper() if require_token else ""
        self.store.set("auth.reset_token", _h(token) if token else "none")
        return token

    def set_password(self, password: str, token: str | None = None) -> str | None:
        """Returns an error message, or None on success."""
        if len(password) < MIN_PASSWORD:
            return f"Use at least {MIN_PASSWORD} characters."
        if self.has_password():
            expected = self.store.get("auth.reset_token")
            if not expected:
                return "A password is already set."
            if expected != "none" and not hmac.compare_digest(_h((token or "").strip().upper()), expected):
                return "Wrong reset token. It is printed in the container log."
        self.store.set("auth.password_hash", _ph.hash(password))
        self.store.delete("auth.reset_token")
        self.store.set("auth.sessions", {})
        return None

    def change_password(self, current: str, new: str, ip: str) -> str | None:
        """Change the password of a logged-in user, who must know the current one.
        Ends every session; the caller starts a new one for the user.  Returns
        an error message, or None on success."""
        if self.rate_limited(ip):
            return "Too many attempts. Wait a few minutes."
        if not self._verify(current):
            self._fails[ip].append(time.monotonic())
            return "The current password is wrong."
        if len(new) < MIN_PASSWORD:
            return f"Use at least {MIN_PASSWORD} characters."
        self.store.set("auth.password_hash", _ph.hash(new))
        self.store.set("auth.sessions", {})
        return None

    def _verify(self, password: str) -> bool:
        h = self.store.get("auth.password_hash")
        try:
            return bool(h) and _ph.verify(h, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    # -- login --------------------------------------------------------------------
    def rate_limited(self, ip: str) -> bool:
        q = self._fails[ip]
        now = time.monotonic()
        while q and now - q[0] > 300:
            q.popleft()
        return len(q) >= 5

    def login(self, password: str, ip: str) -> str | None:
        """Returns a session token, or None."""
        if self.rate_limited(ip):
            return None
        if not self._verify(password):
            self._fails[ip].append(time.monotonic())
            return None
        self._fails.pop(ip, None)
        return self.new_session()

    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        sessions = self._sessions()
        sessions[_h(token)] = {"exp": time.time() + SESSION_TTL, "csrf": secrets.token_urlsafe(24)}
        self.store.set("auth.sessions", sessions)
        return token

    def _sessions(self) -> dict:
        now = time.time()
        return {k: v for k, v in (self.store.get("auth.sessions") or {}).items() if v["exp"] > now}

    def session(self, token: str | None) -> dict | None:
        if not token or self.reset_pending():
            return None
        return self._sessions().get(_h(token))

    def logout(self, token: str | None) -> None:
        if token:
            s = self._sessions()
            s.pop(_h(token), None)
            self.store.set("auth.sessions", s)
