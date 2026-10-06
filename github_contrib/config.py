"""Configuration loading and application settings.

Tokens are read from environment variables (optionally populated from a local
``.env`` file).  Run with ``--user <login>`` and set one of these env vars
(first match wins):

    GITHUB_TOKEN_<LOGIN>   e.g. GITHUB_TOKEN_OCTOCAT for login "octocat"
    GITHUB_TOKEN           single-user convenience fallback
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone, tzinfo
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE_URL: str = "https://api.github.com"
API_VERSION_HEADER: str = "2022-11-28"

#: Default mapping of GitHub login -> environment variable holding its token.
#: Empty by default — specify users with ``--user LOGIN`` on the command line
#: and set GITHUB_TOKEN_<LOGIN> (or GITHUB_TOKEN) in your environment.
DEFAULT_USER_TOKEN_ENV: dict[str, str] = {}

DEFAULT_OUTPUT_DIR: Path = Path("output")
DEFAULT_CONCURRENCY: int = 8
DEFAULT_PER_PAGE: int = 100
DEFAULT_MAX_RETRIES: int = 5
DEFAULT_REQUEST_TIMEOUT: float = 60.0


class ConfigError(RuntimeError):
    """Raised when the application cannot be configured (e.g. missing token)."""


@dataclass(slots=True)
class Account:
    """An authenticated GitHub account used to access the API."""

    login: str
    token: str
    token_env: str

    def masked_token(self) -> str:
        """A safe-to-log representation of the token."""
        if not self.token:
            return "<empty>"
        if len(self.token) <= 8:
            return "****"
        return f"{self.token[:4]}…{self.token[-4:]}"


@dataclass(slots=True)
class AppConfig:
    """Fully resolved application configuration."""

    accounts: list[Account]
    target_logins: list[str]
    output_dir: Path = DEFAULT_OUTPUT_DIR
    concurrency: int = DEFAULT_CONCURRENCY
    per_page: int = DEFAULT_PER_PAGE
    max_retries: int = DEFAULT_MAX_RETRIES
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT
    scan_all_branches: bool = True  # scan every branch by default (complete coverage)
    skip_forks: bool = False
    collect_commits: bool = True
    collect_prs: bool = True
    make_charts: bool = True
    max_repos: int | None = None
    log_level: str = "INFO"
    # Discovery augmentation
    use_search_discovery: bool = True
    enumerate_org_repos: bool = True
    extra_repos: list[str] = field(default_factory=list)
    extra_orgs: list[str] = field(default_factory=list)
    user_token_env: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_USER_TOKEN_ENV))
    # Additional author emails for commits not attributed to a GitHub login
    author_emails: list[str] = field(default_factory=list)
    # Report filtering: optionally drop repositories OWNED by the tracked logins
    # themselves (personal repos) so the outputs show only work done in other
    # accounts / organizations (typically company repos). Disabled by default so
    # reports include ALL of the tracked login's commits. Extra owners can still
    # be excluded explicitly with ``exclude_owners`` (--exclude-owner / EXCLUDE_OWNERS).
    exclude_own_repos: bool = False
    exclude_owners: list[str] = field(default_factory=list)
    # Fetch per-commit line stats (additions/deletions/files_changed).
    # Adds one API request per commit — disable with --no-commit-stats to save quota.
    fetch_commit_stats: bool = True
    # Reporting period (inclusive, timezone aware; None = unbounded) and the
    # time zone used for period boundaries and for days / weeks / months.
    since: datetime | None = None
    until: datetime | None = None
    tz: tzinfo = timezone.utc
    timezone_name: str = "UTC"

    @property
    def charts_dir(self) -> Path:
        return self.output_dir / "charts"

    @property
    def log_file(self) -> Path:
        return self.output_dir / "run.log"


#: Set to 1 to ignore the ``.env`` file (the hosted web UI does this so a
#: run only ever sees the token its user typed in).
NO_DOTENV_ENV = "GITHUB_CONTRIB_NO_DOTENV"


def _maybe_load_dotenv() -> None:
    """Populate ``os.environ`` from a local ``.env`` file if python-dotenv is
    installed.  This is entirely optional - real environment variables always
    take precedence and the tool works fine without the package."""
    if os.environ.get(NO_DOTENV_ENV) == "1":
        return
    try:
        from dotenv import load_dotenv  # type: ignore import-not-found
    except Exception:  # pragma: no cover - dotenv is an optional dependency
        return
    load_dotenv(override=False)


# ---------------------------------------------------------------------------
# Reporting period and time zone
# ---------------------------------------------------------------------------

_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_TZ_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_+-]{0,30}(?:/[A-Za-z0-9_+-]{1,30}){0,2}$")


def resolve_timezone(name: str | None) -> tuple[tzinfo, str]:
    """``(tzinfo, display name)`` for an IANA name ("Asia/Kolkata"), "UTC" or
    a fixed offset ("+05:30"). Raises :class:`ConfigError` for anything else."""
    text = (name or "").strip()
    if not text or text.upper() in ("UTC", "Z", "GMT"):
        return timezone.utc, "UTC"
    match = _OFFSET_RE.match(text)
    if match:
        sign, hours, minutes = match.group(1), int(match.group(2)), int(match.group(3) or 0)
        if hours > 14 or minutes > 59:
            raise ConfigError(f"Invalid UTC offset: {text!r}")
        delta = timedelta(hours=hours, minutes=minutes) * (-1 if sign == "-" else 1)
        return timezone(delta), f"UTC{sign}{hours:02d}:{minutes:02d}"
    # Check the shape before touching the tz database: names become file
    # paths there, and this text may come from a web form.
    if not _TZ_NAME_RE.match(text):
        raise ConfigError(f"Unknown time zone: {text!r}")
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(text), text
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ConfigError(
            f"Unknown time zone: {text!r} (use a name like Asia/Kolkata or an offset like +05:30)"
        ) from exc


def parse_period_bound(text: str, tz: tzinfo, *, end_of_day: bool) -> datetime:
    """Parse ``--since`` / ``--until``.

    ``YYYY-MM-DD`` means the whole day in ``tz``: midnight for the start of a
    period, the last microsecond of the day for its end. A full ISO 8601
    timestamp is taken as-is (``tz`` applies when it has no offset).
    """
    raw = text.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            day = datetime.strptime(raw, "%Y-%m-%d").date()
            moment = datetime.combine(day, time.max if end_of_day else time.min, tzinfo=tz)
        else:
            moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=tz)
    except ValueError as exc:
        raise ConfigError(f"Invalid date {text!r}: use YYYY-MM-DD or an ISO 8601 timestamp") from exc
    if not 1970 <= moment.year <= 2100:
        raise ConfigError(f"Date out of range: {text!r}")
    return moment


def resolve_period(
    since: str | None, until: str | None, timezone_name: str | None
) -> tuple[datetime | None, datetime | None, tzinfo, str]:
    """Validate the reporting period options together."""
    tz, name = resolve_timezone(timezone_name)
    start = parse_period_bound(since, tz, end_of_day=False) if since else None
    end = parse_period_bound(until, tz, end_of_day=True) if until else None
    if start is not None and end is not None and start > end:
        raise ConfigError(f"The period starts ({since}) after it ends ({until}).")
    return start, end, tz, name


def _sanitize_login_for_env(login: str) -> str:
    """Turn a GitHub login into an environment-variable-safe suffix."""
    return re.sub(r"[^A-Za-z0-9]", "_", login).upper()


def token_env_candidates(login: str, mapping: dict[str, str]) -> list[str]:
    """Ordered list of environment variable names that may hold ``login``'s token.

    Resolution order (first one that is set wins):
      1. An explicit mapping entry (e.g. alice -> GITHUB_TOKEN_ALICE).
      2. ``GITHUB_TOKEN_<SANITIZED_LOGIN>`` (e.g. octocat -> GITHUB_TOKEN_OCTOCAT).
      3. ``GITHUB_TOKEN`` (single-user convenience).

    This lets the tool work for *any* GitHub user, not just the built-in two.
    """
    candidates: list[str] = []
    if login in mapping:
        candidates.append(mapping[login])
    candidates.append(f"GITHUB_TOKEN_{_sanitize_login_for_env(login)}")
    candidates.append("GITHUB_TOKEN")
    seen: set[str] = set()
    ordered: list[str] = []
    for name in candidates:
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered


def load_accounts(
    selected_logins: list[str],
    user_token_env: dict[str, str] | None = None,
) -> list[Account]:
    """Resolve the tokens for ``selected_logins`` from the environment.

    Raises :class:`ConfigError` if a required token is missing or empty.
    """
    _maybe_load_dotenv()
    mapping = user_token_env or DEFAULT_USER_TOKEN_ENV
    accounts: list[Account] = []
    missing: list[str] = []
    for login in selected_logins:
        candidates = token_env_candidates(login, mapping)
        chosen_env: str | None = None
        token = ""
        for env_name in candidates:
            value = (os.environ.get(env_name) or "").strip()
            if value:
                chosen_env, token = env_name, value
                break
        if not token:
            missing.append(f"{login} (looked in: {', '.join('$' + c for c in candidates)})")
            continue
        accounts.append(Account(login=login, token=token, token_env=chosen_env or candidates[0]))
    if missing:
        raise ConfigError(
            "Missing GitHub token(s) for: "
            + "; ".join(missing)
            + ".\nSet a token as an environment variable or in a .env file. "
            "See .env.example for the expected names."
        )
    if not accounts:
        raise ConfigError("No accounts could be configured - nothing to do.")
    return accounts


def _parse_env_list(name: str) -> list[str]:
    """Parse a comma/whitespace-separated environment variable into a list."""
    raw = os.environ.get(name) or ""
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


def build_offline_config(
    *,
    selected_logins: list[str],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    make_charts: bool = True,
    log_level: str = "INFO",
    exclude_own_repos: bool = False,
    exclude_owners: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    timezone_name: str | None = None,
) -> AppConfig:
    """An :class:`AppConfig` for ``--regen`` runs.

    Rebuilding reports from previously collected CSVs needs no token, so
    account resolution is skipped entirely; only the report-shaping options
    matter here.
    """
    _maybe_load_dotenv()
    excluded = list(dict.fromkeys([*(exclude_owners or []), *_parse_env_list("EXCLUDE_OWNERS")]))
    start, end, tz, tz_name = resolve_period(since, until, timezone_name)
    return AppConfig(
        accounts=[],
        target_logins=list(selected_logins),
        output_dir=Path(output_dir),
        make_charts=make_charts,
        log_level=log_level,
        exclude_own_repos=exclude_own_repos,
        exclude_owners=excluded,
        since=start,
        until=end,
        tz=tz,
        timezone_name=tz_name,
    )


def build_config(
    *,
    selected_logins: list[str],
    output_dir: Path | str = DEFAULT_OUTPUT_DIR,
    concurrency: int = DEFAULT_CONCURRENCY,
    scan_all_branches: bool = True,
    skip_forks: bool = False,
    collect_commits: bool = True,
    collect_prs: bool = True,
    make_charts: bool = True,
    max_repos: int | None = None,
    log_level: str = "INFO",
    use_search_discovery: bool = True,
    enumerate_org_repos: bool = True,
    extra_repos: list[str] | None = None,
    extra_orgs: list[str] | None = None,
    author_emails: list[str] | None = None,
    exclude_own_repos: bool = False,
    exclude_owners: list[str] | None = None,
    fetch_commit_stats: bool = True,
    max_retries: int = DEFAULT_MAX_RETRIES,
    request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
    user_token_env: dict[str, str] | None = None,
    since: str | None = None,
    until: str | None = None,
    timezone_name: str | None = None,
) -> AppConfig:
    """Build a fully validated :class:`AppConfig`.

    ``extra_repos`` / ``extra_orgs`` from the caller are unioned with the
    ``EXTRA_REPOS`` / ``EXTRA_ORGS`` environment variables (loaded from .env).
    """
    mapping = user_token_env or dict(DEFAULT_USER_TOKEN_ENV)
    start, end, tz, tz_name = resolve_period(since, until, timezone_name)
    accounts = load_accounts(selected_logins, mapping)  # also loads .env

    repos = list(dict.fromkeys([*(extra_repos or []), *_parse_env_list("EXTRA_REPOS")]))
    orgs = list(dict.fromkeys([*(extra_orgs or []), *_parse_env_list("EXTRA_ORGS")]))
    emails = list(dict.fromkeys([*(author_emails or []), *_parse_env_list("AUTHOR_EMAILS")]))
    excluded = list(dict.fromkeys([*(exclude_owners or []), *_parse_env_list("EXCLUDE_OWNERS")]))

    return AppConfig(
        accounts=accounts,
        target_logins=list(selected_logins),
        output_dir=Path(output_dir),
        concurrency=max(1, concurrency),
        scan_all_branches=scan_all_branches,
        skip_forks=skip_forks,
        collect_commits=collect_commits,
        collect_prs=collect_prs,
        make_charts=make_charts,
        max_repos=max_repos,
        log_level=log_level,
        use_search_discovery=use_search_discovery,
        enumerate_org_repos=enumerate_org_repos,
        extra_repos=repos,
        extra_orgs=orgs,
        author_emails=emails,
        exclude_own_repos=exclude_own_repos,
        exclude_owners=excluded,
        fetch_commit_stats=fetch_commit_stats,
        max_retries=max_retries,
        request_timeout=request_timeout,
        user_token_env=mapping,
        since=start,
        until=end,
        tz=tz,
        timezone_name=tz_name,
    )
