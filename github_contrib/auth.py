"""GitHub sign-in for the hosted web app (OAuth App web flow).

* :class:`SessionCodec` seals the signed-in session — GitHub user id, login,
  OAuth token and expiry — into a Fernet-encrypted cookie value. The token
  lives only there and in memory while a report run uses it.
* :func:`exchange_code`, :func:`fetch_user` and :func:`revoke_token` are the
  three GitHub calls the flow makes. They are module-level functions so tests
  can replace them.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

import aiohttp

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
API_URL = "https://api.github.com"
DEFAULT_SCOPES = "repo read:org"
SESSION_TTL = 14 * 24 * 3600  # seconds
STATE_TTL = 10 * 60
MIN_SECRET_LENGTH = 16

_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")
_TIMEOUT = aiohttp.ClientTimeout(total=15)
_HEADERS = {"User-Agent": "CommitsTracker"}


class AuthError(Exception):
    """A step of the sign-in flow failed. ``code`` is short and safe to show."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def avatar_url(user_id: int) -> str:
    return f"https://avatars.githubusercontent.com/u/{int(user_id)}?v=4"


@dataclass(frozen=True, slots=True)
class AuthSession:
    uid: int
    login: str
    token: str = field(repr=False)
    exp: int = 0


def fernet_key(secret: str) -> bytes:
    """A Fernet key derived from SESSION_SECRET (sha256 -> urlsafe base64)."""
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


class SessionCodec:
    """Encrypts and authenticates session cookies with a key from ``secret``."""

    def __init__(self, secret: str, ttl: int = SESSION_TTL) -> None:
        if len(secret or "") < MIN_SECRET_LENGTH:
            raise ValueError(
                f"SESSION_SECRET must be set to a random string of at least {MIN_SECRET_LENGTH} "
                "characters to use GitHub sign-in."
            )
        # Imported here so local mode (no sign-in) doesn't need the package.
        from cryptography.fernet import Fernet, InvalidToken

        self._fernet = Fernet(fernet_key(secret))
        self._invalid = InvalidToken
        self.ttl = ttl

    def __repr__(self) -> str:
        return "SessionCodec()"

    def encode(self, uid: int, login: str, token: str, *, now: float | None = None) -> str:
        moment = int(time.time() if now is None else now)
        payload = json.dumps(
            {"uid": int(uid), "login": login, "token": token, "exp": moment + self.ttl},
            separators=(",", ":"),
        ).encode("utf-8")
        sealed = self._fernet.encrypt_at_time(payload, moment)
        # Fernet tokens are base64url; drop the '=' padding so the cookie value
        # never needs quoting.
        return sealed.decode("ascii").rstrip("=")

    def decode(self, value: str, *, now: float | None = None) -> AuthSession | None:
        """The session in ``value``, or ``None`` if it is missing, forged or expired."""
        if not value or len(value) > 4096:
            return None
        moment = int(time.time() if now is None else now)
        try:
            raw = (value + "=" * (-len(value) % 4)).encode("ascii")
            payload = self._fernet.decrypt_at_time(raw, ttl=self.ttl + 60, current_time=moment)
            data = json.loads(payload)
            uid, login, token, exp = data["uid"], data["login"], data["token"], data["exp"]
        except (self._invalid, ValueError, KeyError, TypeError, UnicodeError):
            return None
        if not (isinstance(uid, int) and not isinstance(uid, bool) and uid > 0):
            return None
        if not (isinstance(login, str) and _LOGIN_RE.match(login)):
            return None
        if not (isinstance(token, str) and _TOKEN_RE.match(token)):
            return None
        if not isinstance(exp, int) or exp <= moment:
            return None
        return AuthSession(uid=uid, login=login, token=token, exp=exp)


def authorize_url(client_id: str, redirect_uri: str, scopes: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "allow_signup": "true",
        }
    )
    return f"{AUTHORIZE_URL}?{query}"


async def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    """Trade the callback's ``code`` for an access token."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS, trust_env=True) as http:
        async with http.post(
            TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
            headers={"Accept": "application/json"},
        ) as response:
            if response.status != 200:
                raise AuthError("exchange", f"HTTP {response.status}")
            data = await response.json(content_type=None)
    token = data.get("access_token") if isinstance(data, dict) else None
    if not isinstance(token, str) or not _TOKEN_RE.match(token):
        # GitHub answers 200 with {"error": "bad_verification_code", ...}.
        error = data.get("error") if isinstance(data, dict) else None
        raise AuthError("exchange", str(error or "no access token")[:60])
    return token


async def fetch_user(token: str) -> tuple[int, str]:
    """``(numeric id, login)`` of the account that owns ``token``."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS, trust_env=True) as http:
        async with http.get(
            f"{API_URL}/user",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as response:
            if response.status != 200:
                raise AuthError("user", f"HTTP {response.status}")
            data = await response.json(content_type=None)
    uid = data.get("id") if isinstance(data, dict) else None
    login = data.get("login") if isinstance(data, dict) else None
    if not isinstance(uid, int) or isinstance(uid, bool) or uid <= 0:
        raise AuthError("user", "no user id")
    if not isinstance(login, str) or not _LOGIN_RE.match(login):
        raise AuthError("user", "no login")
    return uid, login


async def revoke_token(client_id: str, client_secret: str, token: str) -> None:
    """Revoke ``token`` (DELETE /applications/{client_id}/token)."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT, headers=_HEADERS, trust_env=True) as http:
        async with http.delete(
            f"{API_URL}/applications/{quote(client_id, safe='')}/token",
            auth=aiohttp.BasicAuth(client_id, client_secret),
            json={"access_token": token},
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        ) as response:
            # 204 = revoked; 404/422 = already gone. Anything else is logged by the caller.
            if response.status not in (204, 404, 422):
                raise AuthError("revoke", f"HTTP {response.status}")
