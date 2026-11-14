"""Single-password gate: HMAC-signed, HttpOnly session cookie.

- Constant-time password comparison.
- Per-IP login throttling against brute force.
- Mutating requests must carry the custom `X-LeadHub` header; browsers cannot
  send custom headers cross-site without a CORS preflight (which we never
  allow), so together with SameSite=Strict this blocks CSRF.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response, status

from .config import Settings

COOKIE_NAME = "leadhub_session"
HINT_COOKIE_NAME = "leadhub_hint"
CSRF_HEADER = "x-leadhub"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_FAILURES = 5
FAILURE_WINDOW_S = 300


class LoginThrottle:
    def __init__(self, max_failures: int = MAX_FAILURES, window_s: int = FAILURE_WINDOW_S) -> None:
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        attempts = self._failures[key]
        while attempts and now - attempts[0] > self.window_s:
            attempts.popleft()
        return attempts

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        attempts = self._prune(key, now)
        if not attempts:
            self._failures.pop(key, None)  # don't keep an empty bucket per probing IP
            return 0
        if len(attempts) < self.max_failures:
            return 0
        return int(self.window_s - (now - attempts[0])) + 1

    def record_failure(self, key: str) -> None:
        self._failures[key].append(time.monotonic())

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


def _signing_key(settings: Settings) -> bytes:
    # Binding the key to the password means changing LEADHUB_PASSWORD (or rotating
    # LEADHUB_SECRET_KEY) invalidates every existing session — stateless revocation.
    password_digest = hashlib.sha256(settings.password.encode("utf-8")).hexdigest()
    return f"{settings.secret_key}:{password_digest}".encode("utf-8")


def _sign(settings: Settings, payload: str) -> str:
    digest = hmac.new(_signing_key(settings), payload.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def issue_token(settings: Settings, now: float | None = None) -> str:
    expires = int((now or time.time()) + settings.session_hours * 3600)
    payload = f"{expires}.{secrets.token_urlsafe(16)}"
    return f"{payload}.{_sign(settings, payload)}"


def verify_token(settings: Settings, token: str | None, now: float | None = None) -> bool:
    if not token or token.count(".") != 2:
        return False
    expires_raw, nonce, signature = token.split(".")
    if not hmac.compare_digest(signature, _sign(settings, f"{expires_raw}.{nonce}")):
        return False
    return expires_raw.isascii() and expires_raw.isdigit() and int(expires_raw) > (now or time.time())


def password_matches(settings: Settings, candidate: str) -> bool:
    return hmac.compare_digest(candidate.encode("utf-8"), settings.password.encode("utf-8"))


def set_session_cookie(response: Response, settings: Settings) -> None:
    max_age = settings.session_hours * 3600
    response.set_cookie(
        COOKIE_NAME, issue_token(settings), max_age=max_age,
        httponly=True, secure=settings.cookie_secure, samesite="strict", path="/",
    )
    # Non-secret UI hint readable by JS ("probably logged in"), so the page can skip
    # straight to the login form without firing doomed API calls. Grants nothing.
    response.set_cookie(
        HINT_COOKIE_NAME, "1", max_age=max_age,
        httponly=False, secure=settings.cookie_secure, samesite="strict", path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    for name, http_only in ((COOKIE_NAME, True), (HINT_COOKIE_NAME, False)):
        response.delete_cookie(name, path="/", secure=settings.cookie_secure, httponly=http_only, samesite="strict")


def is_authenticated(request: Request, settings: Settings) -> bool:
    return verify_token(settings, request.cookies.get(COOKIE_NAME))


def make_auth_dependency(settings: Settings):
    """Router-level dependency: applied to *every* protected API route."""

    async def require_auth(request: Request) -> None:
        if not is_authenticated(request, settings):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Требуется вход")
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Отсутствует заголовок X-LeadHub")

    return require_auth
