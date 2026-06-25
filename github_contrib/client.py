"""Asynchronous GitHub REST API client.

Features
--------
* Bearer-token authentication.
* Automatic pagination (driven by the ``Link`` header).
* Primary and secondary (abuse) rate-limit handling.
* Exponential-backoff retries for transient failures.
* A shared concurrency limiter (semaphore) so many clients can share one budget.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import aiohttp

from .config import API_BASE_URL, API_VERSION_HEADER
from .logging_config import get_logger

log = get_logger("client")

# HTTP statuses that simply mean "no data here" - callers treat the result as
# empty rather than an error.
_EMPTY_STATUSES = frozenset({404, 409, 451})
# Statuses worth retrying after a backoff.
_RETRY_STATUSES = frozenset({500, 502, 503, 504})


class GitHubError(RuntimeError):
    """A non-recoverable error talking to the GitHub API."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class GitHubClient:
    """A thin async wrapper around a single authenticated GitHub session."""

    token: str
    session: aiohttp.ClientSession
    semaphore: asyncio.Semaphore
    login: str = ""
    max_retries: int = 5
    per_page: int = 100
    request_timeout: float = 60.0
    # Telemetry / discovered metadata
    request_count: int = field(default=0, init=False)
    rate_limit_waits: int = field(default=0, init=False)
    scopes: str | None = field(default=None, init=False)

    @property
    def token_kind(self) -> str:
        """Best-effort classification of the token from its prefix."""
        t = self.token
        if t.startswith("github_pat_"):
            return "fine-grained"
        if t.startswith("ghp_"):
            return "classic"
        if t.startswith(("gho_", "ghu_", "ghs_")):
            return "oauth"
        return "unknown"

    # -- internal helpers --------------------------------------------------

    def _headers(self, accept: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": accept,
            "X-GitHub-Api-Version": API_VERSION_HEADER,
            "User-Agent": "github-contrib-report/1.0",
        }

    @staticmethod
    def _full_url(path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{API_BASE_URL}/{path.lstrip('/')}"

    @staticmethod
    def _parse_next_link(link_header: str | None) -> str | None:
        """Extract the rel="next" URL from a ``Link`` header, if present."""
        if not link_header:
            return None
        for part in link_header.split(","):
            section = part.split(";")
            if len(section) < 2:
                continue
            url_part = section[0].strip()
            if not (url_part.startswith("<") and url_part.endswith(">")):
                continue
            rels = [s.strip() for s in section[1:]]
            if any(rel == 'rel="next"' for rel in rels):
                return url_part[1:-1]
        return None

    @staticmethod
    def _rate_limit_wait(headers: "aiohttp.typedefs.CIMultiDictProxy[str]") -> float | None:
        """Compute how long to wait from rate-limit headers, without sleeping.

        Returns the number of seconds to wait, or ``None`` if the headers do
        not indicate a (computable) rate-limit wait.
        """
        retry_after = headers.get("Retry-After")
        remaining = headers.get("X-RateLimit-Remaining")
        reset = headers.get("X-RateLimit-Reset")

        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        if remaining is not None and reset is not None:
            try:
                if int(remaining) <= 0:
                    return max(0.0, float(reset) - time.time())
            except ValueError:
                pass
        return None

    def _backoff_delay(self, attempt: int) -> float:
        return min(60.0, 2.0 ** attempt) + random.uniform(0, 0.5)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> tuple[Any, aiohttp.typedefs.CIMultiDictProxy[str] | None, int]:
        """Perform a single API request with retries and rate-limit handling.

        Returns ``(data, headers, status)``.  For "empty" statuses (404/409/451)
        and for genuine permission 403s, ``data`` is ``None`` and no exception is
        raised.  Sleeps (rate-limit and backoff) happen *outside* the concurrency
        semaphore so a waiting request never occupies a slot.
        """
        url = self._full_url(path)
        timeout = aiohttp.ClientTimeout(total=self.request_timeout)
        attempt = 0

        while True:
            attempt += 1
            # Decided inside the semaphore, performed after it is released.
            sleep_seconds: float | None = None
            try:
                async with self.semaphore:
                    self.request_count += 1
                    async with self.session.request(
                        method,
                        url,
                        params=params,
                        headers=self._headers(accept),
                        timeout=timeout,
                    ) as response:
                        status = response.status

                        if status in (200, 201):
                            data = await response.json()
                            return data, response.headers, status

                        if status in _EMPTY_STATUSES:
                            return None, response.headers, status

                        if status in (403, 429):
                            body = await _safe_text(response)
                            wait = self._rate_limit_wait(response.headers)
                            is_rate_limit = (
                                status == 429
                                or wait is not None
                                or _is_secondary_rate_limit(body)
                            )
                            if not is_rate_limit:
                                # Genuine permission / SSO problem - surface it
                                # at WARNING so missing data is not mistaken for
                                # an absence of contributions.
                                log.warning(
                                    "[%s] Access denied (%d) for %s - token may lack "
                                    "scope or org SSO authorization: %s",
                                    self.login or "client",
                                    status,
                                    url,
                                    body[:200],
                                )
                                return None, response.headers, status
                            if attempt > self.max_retries:
                                raise GitHubError(
                                    f"Rate limited and out of retries for {url}: {body[:200]}",
                                    status=status,
                                )
                            self.rate_limit_waits += 1
                            sleep_seconds = wait if wait is not None else self._backoff_delay(attempt)
                            log.warning(
                                "[%s] Rate limited (%d); sleeping %.0fs before retry %d/%d.",
                                self.login or "client",
                                status,
                                min(sleep_seconds + 1.0, 3600.0),
                                attempt,
                                self.max_retries,
                            )

                        elif status == 401:
                            raise GitHubError(
                                f"Authentication failed (401) for {url}. "
                                "Check that the token is valid and not expired.",
                                status=status,
                            )

                        elif status in _RETRY_STATUSES:
                            if attempt > self.max_retries:
                                body = await _safe_text(response)
                                raise GitHubError(
                                    f"GitHub API error {status} for {url}: {body[:200]}",
                                    status=status,
                                )
                            sleep_seconds = self._backoff_delay(attempt)

                        else:
                            body = await _safe_text(response)
                            raise GitHubError(
                                f"GitHub API error {status} for {url}: {body[:200]}",
                                status=status,
                            )

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if attempt > self.max_retries:
                    raise GitHubError(f"Network error for {url}: {exc}") from exc
                log.debug(
                    "Transient error on %s (attempt %d/%d): %s",
                    url,
                    attempt,
                    self.max_retries,
                    exc,
                )
                sleep_seconds = self._backoff_delay(attempt)

            # Perform the wait outside the semaphore so we free the slot.
            if sleep_seconds is not None:
                await asyncio.sleep(min(sleep_seconds + 1.0, 3600.0))
            # Loop again to retry.

    # -- public helpers ----------------------------------------------------

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        """GET a single JSON document (no pagination). Returns ``None`` if absent."""
        data, _headers, _status = await self.request("GET", path, params=params, accept=accept)
        return data

    async def paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        accept: str = "application/vnd.github+json",
        max_items: int | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield every item of a paginated list endpoint.

        Pagination follows the ``Link: rel="next"`` header.  Handles endpoints
        that return a bare JSON array.
        """
        query = dict(params or {})
        query.setdefault("per_page", self.per_page)
        next_url: str | None = self._full_url(path)
        # Only attach params on the first request; subsequent ``next`` URLs
        # already include them.
        page_index = 0
        yielded = 0
        while next_url is not None:
            data, headers, status = await self.request(
                "GET",
                next_url,
                params=query if page_index == 0 else None,
                accept=accept,
            )
            if data is None:
                if page_index == 0:
                    # Legitimately empty / not found / no access.
                    return
                # We already yielded items but a later page came back empty.
                # Raising (instead of silently returning) prevents the caller
                # from treating a truncated list as complete.
                raise GitHubError(
                    f"Pagination interrupted mid-stream at {next_url} "
                    f"(status {status}) after {yielded} item(s); "
                    "refusing to return a silently-truncated result.",
                    status=status,
                )
            if isinstance(data, dict):
                # Defensive: some endpoints wrap results (e.g. search).
                items = data.get("items")
                if items is None:
                    return
            else:
                items = data
            for item in items:
                yield item
                yielded += 1
                if max_items is not None and yielded >= max_items:
                    return
            page_index += 1
            next_url = self._parse_next_link(headers.get("Link") if headers else None)

    async def get_authenticated_login(self) -> str:
        """Return the login of the account the token belongs to."""
        login, _scopes = await self.get_viewer()
        return login

    async def get_viewer(self) -> tuple[str, str | None]:
        """Return ``(login, scopes)`` for the authenticated token.

        ``scopes`` is the value of the ``X-OAuth-Scopes`` response header,
        which is only present for classic tokens (``None`` for fine-grained).
        """
        data, headers, _status = await self.request("GET", "/user")
        if isinstance(data, dict) and data.get("login"):
            self.login = str(data["login"])
        self.scopes = headers.get("X-OAuth-Scopes") if headers else None
        return self.login, self.scopes


def _is_secondary_rate_limit(body: str) -> bool:
    """Detect GitHub's secondary (abuse) rate limit from the response body.

    Secondary-limit 403s frequently arrive with no Retry-After header and with
    the primary budget (X-RateLimit-Remaining) untouched, so the body phrase is
    the reliable signal (per GitHub's documentation).
    """
    if not body:
        return False
    text = body.lower()
    return "secondary rate limit" in text or "abuse" in text


async def _safe_text(response: aiohttp.ClientResponse) -> str:
    try:
        return await response.text()
    except Exception:  # pragma: no cover - best effort only
        return "<unreadable response body>"
