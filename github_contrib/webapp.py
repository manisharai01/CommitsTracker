"""Web UI: enter accounts and tokens in a browser, download the PDF.

``python webui.py`` serves a single page (``github_contrib/web/``) where you
enter one or more GitHub logins with their tokens, pick the report options and
the time range, and download the finished report. It runs in one of two modes:

* **Local** (default) — on your own computer, bound to 127.0.0.1. Tokens may be
  left blank to use the ones in ``.env``; every report is listed.
* **Public** (``--public-url https://reports.example.com``) — a hosted service
  for many users behind an HTTPS reverse proxy. Every browser session sees only
  its own reports, tokens must be typed in (``.env`` is never read), runs are
  limited per user and per address, and reports are deleted after
  ``--retention-hours``.
* **Public with GitHub sign-in** — public mode plus ``GITHUB_CLIENT_ID`` /
  ``GITHUB_CLIENT_SECRET`` / ``SESSION_SECRET``: people sign in with GitHub
  (see :mod:`.auth`) and their report history follows their account across
  browsers (see :mod:`.store`). Signing in only identifies them: a report can
  cover any accounts, each with its own token, as in public mode.

Each submitted form becomes a *job* that runs ``github_report.py --pdf`` in a
subprocess:

* tokens reach the child only through its environment (never argv, never
  disk) and are dropped from memory as soon as the run ends;
* at most ``--parallel`` runs execute at once; the rest queue;
* each run writes to ``output-web/<job id>/`` next to a token-free
  ``job.json``, so the report history survives a server restart.
"""

from __future__ import annotations

import asyncio
import codecs
import collections
import csv
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from . import __version__
from . import auth as gh_auth
from .config import (
    NO_DOTENV_ENV,
    ConfigError,
    _sanitize_login_for_env,
    resolve_period,
    resolve_timezone,
    token_env_candidates,
)
from .htmlreport import REPORT_CSP
from .linkedin import LINKEDIN_LIMIT, build_linkedin_post
from .logging_config import get_logger
from .pdfexport import find_browser, pdf_timeout
from .store import ReportRow, Store, make_store

log = get_logger("webapp")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLI_SCRIPT = PROJECT_ROOT / "github_report.py"
WEB_DIR = Path(__file__).resolve().parent / "web"
DEFAULT_JOBS_DIR = PROJECT_ROOT / "output-web"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

#: Downloadable files of a finished run, keyed by the kind used in URLs.
REPORT_FILES: dict[str, str] = {
    "pdf": "report.pdf",
    "html": "report.html",
    "xlsx": "github_contributions.xlsx",
    "md": "report.md",
}

#: Metrics from contribution_summary.csv shown on a finished report card.
SUMMARY_KEYS: tuple[str, ...] = (
    "total_lifetime_commits",
    "total_pull_requests",
    "merged_pull_requests",
    "repositories_contributed_to",
    "organizations_contributed_to",
    "active_days",
    "first_contribution_date",
    "latest_contribution_date",
    "total_lines_added",
    "total_lines_deleted",
    "merge_commits",
    "report_period",
    "report_timezone",
    "data_completeness_warnings",
)

#: Environment variables the form replaces, keyed by form field name.
FORM_ENV_VARS: dict[str, str] = {
    "author_emails": "AUTHOR_EMAILS",
    "extra_repos": "EXTRA_REPOS",
    "extra_orgs": "EXTRA_ORGS",
    "exclude_owners": "EXCLUDE_OWNERS",
}

#: In public mode a run inherits only these variables (operating system,
#: Python, proxy and certificate settings) — never the server's secrets.
CHILD_ENV_ALLOWLIST = frozenset(
    name.upper()
    for name in (
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "OS",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
        "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
        "PYTHONHOME", "PYTHONPATH", "MPLCONFIGDIR",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
        "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "FONTCONFIG_PATH",
        "FONTCONFIG_FILE",
        # Chromium in a container needs --no-sandbox; slow hosts may need more
        # time for the PDF (see pdfexport).
        "PDF_NO_SANDBOX", "PDF_TIMEOUT",
    )
)

#: What a report card says when its PDF couldn't be made.
PDF_NO_BROWSER = (
    "The PDF could not be rendered (it needs Microsoft Edge or Google Chrome). "
    "Open the HTML report and print it to PDF instead."
)
PDF_FAILED = (
    "The PDF could not be rendered on this server. Open the HTML report and print it "
    "to PDF instead (the report itself is complete)."
)
PDF_STOPPED = "The server stopped before the PDF was ready. Open the HTML report and print it to PDF instead."

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
MAX_ACCOUNTS = 20
MAX_LOG_LINES = 5000
MAX_WARNINGS = 10
SESSION_MAX_AGE = 30 * 24 * 3600
HISTORY_CACHE_SECONDS = 20.0

_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_SHARE_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NEWLINES = re.compile(r"\r\n|\r|\n")
# tqdm progress line: "Commits:  45%|████▌     | 90/200 [00:30<00:40,  2.70item/s]"
_PROGRESS_RE = re.compile(r"^(?P<label>[^:|]+):\s*\d+%\|.*?\|\s*(?P<n>\d+)/(?P<total>\d+)")
# Package log line: "2026-10-03 12:13:33 | INFO    | github_contrib.report | message"
_LOG_LINE_RE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| (?P<level>[A-Z]+)\s*\| \S+ \| (?P<msg>.*)$"
)

#: Progress-bar labels (see report._gather_with_progress) -> phase shown in the UI.
_PROGRESS_PHASES: dict[str, str] = {
    "Branches": "Listing branches",
    "Commits": "Collecting commits",
    "Pull requests": "Collecting pull requests",
    "PR commits": "Reading pull-request commits",
    "Line stats": "Fetching line stats",
}
#: Log message fragments that start a new phase, in pipeline order.
_LOG_PHASES: tuple[tuple[str, str], ...] = (
    ("starting for", "Authenticating"),
    ("discovering repositories", "Discovering repositories"),
    ("search found", "Searching GitHub"),
    ("Fetching line stats", "Fetching line stats"),
    ("Finished collection", "Building report"),
    ("wrote report.md and report.html", "Rendering PDF"),
)

#: Boolean form options and their defaults (mirroring the CLI defaults).
_FLAG_DEFAULTS: dict[str, bool] = {
    "exclude_own_repos": False,
    "default_branch_only": False,
    "skip_forks": False,
    "commit_stats": True,
    "pull_requests": True,
}
#: List form options -> (item pattern, item description for error messages).
_LIST_OPTIONS: dict[str, tuple[re.Pattern[str], str]] = {
    "extra_repos": (_REPO_RE, "repository (use owner/name)"),
    "extra_orgs": (_LOGIN_RE, "organization"),
    "exclude_owners": (_LOGIN_RE, "owner"),
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Settings:
    """How the server runs. ``public_url`` switches on the hosted mode."""

    jobs_dir: Path = DEFAULT_JOBS_DIR
    public_url: str = ""
    parallel: int = 1
    job_timeout: float = 0.0  # seconds; 0 = no limit
    retention_hours: float = 0.0  # 0 = keep reports until deleted
    max_active_per_user: int = 5
    max_queue: int = 100
    max_jobs_per_hour: int = 0  # per client address (per account when signed in); 0 = unlimited
    trust_proxy: bool = False  # take the client address from X-Forwarded-For
    python: str = sys.executable
    script: Path = CLI_SCRIPT
    # GitHub sign-in (public mode only): an OAuth App's credentials, the secret
    # that encrypts session cookies, and the report-history database.
    github_client_id: str = ""
    github_client_secret: str = field(default="", repr=False)
    session_secret: str = field(default="", repr=False)
    database_url: str = field(default="", repr=False)

    @property
    def public(self) -> bool:
        return bool(self.public_url)

    @property
    def auth(self) -> bool:
        """Sign in with GitHub: public mode with an OAuth App configured."""
        return self.public and bool(self.github_client_id and self.github_client_secret)

    @property
    def auth_cookie(self) -> str:
        return "__Host-ct_auth" if self.secure else "ct_auth"

    @property
    def state_cookie(self) -> str:
        return "__Host-ct_oauth_state" if self.secure else "ct_oauth_state"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}".lower()

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def session_cookie(self) -> str:
        # The __Host- prefix pins the cookie to this exact host over HTTPS.
        return "__Host-ct_session" if self.secure else "ct_session"


def public_settings(public_url: str, **overrides) -> Settings:
    """Settings for a hosted deployment, with production defaults."""
    parts = urlsplit(public_url.strip())
    local_test = parts.hostname in LOOPBACK_HOSTS
    if parts.scheme != "https" and not (parts.scheme == "http" and local_test):
        raise ValueError("--public-url must start with https:// (tokens are sent to this server).")
    if not parts.netloc or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("--public-url must be the site root, like https://reports.example.com")
    defaults = {
        "public_url": f"{parts.scheme}://{parts.netloc}",
        "parallel": 2,
        "job_timeout": 2 * 3600.0,
        "retention_hours": 24.0,
        "max_active_per_user": 2,
        "max_queue": 50,
        "max_jobs_per_hour": 20,
    }
    defaults.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**defaults)


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------


class RequestError(ValueError):
    """A problem with a submitted form, worded for the person who filled it in."""


@dataclass(slots=True)
class AccountInput:
    login: str
    token: str  # "" -> the CLI falls back to GITHUB_TOKEN_<LOGIN> / GITHUB_TOKEN
    emails: list[str]


@dataclass(slots=True)
class JobRequest:
    """A validated form submission: everything one report run needs."""

    accounts: list[AccountInput]
    extra_repos: list[str] = field(default_factory=list)
    extra_orgs: list[str] = field(default_factory=list)
    exclude_owners: list[str] = field(default_factory=list)
    exclude_own_repos: bool = False
    default_branch_only: bool = False
    skip_forks: bool = False
    commit_stats: bool = True
    pull_requests: bool = True
    since: str = ""  # YYYY-MM-DD, inclusive
    until: str = ""
    timezone: str = "UTC"

    @property
    def logins(self) -> list[str]:
        return [a.login for a in self.accounts]

    @property
    def author_emails(self) -> list[str]:
        return list(dict.fromkeys(e for a in self.accounts for e in a.emails))

    def options(self) -> dict:
        """The token-free form options, as stored in job.json."""
        options: dict[str, object] = {
            name: getattr(self, name) for name in (*_LIST_OPTIONS, *_FLAG_DEFAULTS)
        }
        options.update(since=self.since, until=self.until, timezone=self.timezone)
        return options


def _as_list(value: object) -> list[str]:
    """Accept a list or a comma/whitespace-separated string; return clean items."""
    if value is None:
        return []
    if isinstance(value, str):
        chunks = [value]
    elif isinstance(value, list) and all(isinstance(v, str) for v in value):
        chunks = value
    else:
        raise RequestError("Lists must be text or an array of text.")
    items = [p.strip() for chunk in chunks for part in chunk.split(",") for p in part.split()]
    return list(dict.fromkeys(p for p in items if p))


def _checked(items: list[str], pattern: re.Pattern[str], what: str) -> list[str]:
    bad = [item for item in items if not pattern.match(item)]
    if bad:
        raise RequestError(f"Invalid {what}: {', '.join(bad[:3])}")
    return items


def has_env_token(login: str, env: Mapping[str, str]) -> bool:
    """Whether the CLI would find a token for ``login`` in ``env``."""
    return any((env.get(name) or "").strip() for name in token_env_candidates(login, {}))


def _parse_period(raw: object) -> tuple[str, str, str]:
    """``(since, until, time zone)`` from the form's ``period`` object."""
    if raw is None:
        return "", "", "UTC"
    if not isinstance(raw, dict):
        raise RequestError("The time range must be an object.")
    since = str(raw.get("since") or "").strip()
    until = str(raw.get("until") or "").strip()
    for value in (since, until):
        if value and not _DATE_RE.match(value):
            raise RequestError("Dates must look like YYYY-MM-DD.")
    zone = str(raw.get("timezone") or "").strip()[:64]
    offset = str(raw.get("timezone_offset") or "").strip()[:10]
    try:
        _tz, name = resolve_timezone(zone)
    except ConfigError as zone_error:
        # The browser's zone may be unknown to this server's tz database:
        # its current UTC offset is the next best thing.
        if not offset:
            raise RequestError(str(zone_error)) from zone_error
        try:
            _tz, name = resolve_timezone(offset)
        except ConfigError as exc:
            raise RequestError(str(exc)) from exc
    try:
        resolve_period(since or None, until or None, name)
    except ConfigError as exc:
        raise RequestError(str(exc)) from exc
    return since, until, name


def parse_job_request(
    payload: object,
    env: Mapping[str, str],
    *,
    require_tokens: bool = False,
) -> JobRequest:
    """Validate a form submission.

    ``env`` is what the CLI child will see (see :func:`effective_env`): an
    account may leave its token blank when ``env`` already holds one — unless
    ``require_tokens`` (public mode), where every account brings its own.
    Signing in with GitHub never stands in for a token: a report reads each
    account with that account's own access, or not at all.
    """
    if not isinstance(payload, dict):
        raise RequestError("The request must be a JSON object.")
    raw_accounts = payload.get("accounts")
    if not isinstance(raw_accounts, list) or not raw_accounts:
        raise RequestError("Add at least one GitHub account.")
    if len(raw_accounts) > MAX_ACCOUNTS:
        raise RequestError(f"Use at most {MAX_ACCOUNTS} accounts per report.")

    accounts: list[AccountInput] = []
    seen: set[str] = set()
    for raw in raw_accounts:
        if not isinstance(raw, dict):
            raise RequestError("Each account must be an object.")
        login = str(raw.get("login") or "").strip().lstrip("@")
        if not _LOGIN_RE.match(login):
            raise RequestError(f"'{login[:40] or '(blank)'}' is not a valid GitHub username.")
        if login.lower() in seen:
            raise RequestError(f"{login} is listed twice.")
        seen.add(login.lower())
        token = str(raw.get("token") or "").strip()
        if token and not _TOKEN_RE.match(token):
            raise RequestError(f"The token for {login} doesn't look like a GitHub token.")
        if not token and require_tokens:
            raise RequestError(f"Enter a personal access token for {login}.")
        if not token and not has_env_token(login, env):
            raise RequestError(
                f"Enter a token for {login} — .env has no "
                f"GITHUB_TOKEN_{_sanitize_login_for_env(login)} or GITHUB_TOKEN."
            )
        emails = _checked(_as_list(raw.get("emails")), _EMAIL_RE, "email address")
        accounts.append(AccountInput(login=login, token=token, emails=emails))

    options = payload.get("options") or {}
    if not isinstance(options, dict):
        raise RequestError("Options must be an object.")
    flags: dict[str, bool] = {}
    for name, default in _FLAG_DEFAULTS.items():
        value = options.get(name, default)
        if not isinstance(value, bool):
            raise RequestError(f"Option {name} must be true or false.")
        flags[name] = value
    lists = {
        name: _checked(_as_list(options.get(name)), pattern, what)
        for name, (pattern, what) in _LIST_OPTIONS.items()
    }
    since, until, zone = _parse_period(payload.get("period"))
    return JobRequest(accounts=accounts, **lists, **flags, since=since, until=until, timezone=zone)


def build_command(
    request: JobRequest, out_dir: Path, python: str = sys.executable, script: Path = CLI_SCRIPT
) -> list[str]:
    """The ``github_report.py`` invocation for ``request`` (no secrets in it)."""
    cmd = [python, str(script)]
    for login in request.logins:
        cmd += ["--user", login]
    # No --pdf: the PDF is printed once this run has exited (JobManager._render_pdf).
    cmd += ["--output", str(out_dir), "--timezone", request.timezone]
    if request.since:
        cmd += ["--since", request.since]
    if request.until:
        cmd += ["--until", request.until]
    if request.exclude_own_repos:
        cmd.append("--exclude-own-repos")
    if request.default_branch_only:
        cmd.append("--default-branch-only")
    if request.skip_forks:
        cmd.append("--skip-forks")
    if not request.commit_stats:
        cmd.append("--no-commit-stats")
    if not request.pull_requests:
        cmd.append("--no-prs")
    return cmd


def build_env(
    request: JobRequest, base: Mapping[str, str], *, isolated: bool = False
) -> dict[str, str]:
    """The child's environment: ``base`` plus this run's tokens and form lists.

    ``isolated`` (public mode) keeps only operating-system variables from
    ``base`` and stops the child from reading ``.env``, so a run can only ever
    use the token its user typed in.
    """
    if isolated:
        env = {k: v for k, v in base.items() if k.upper() in CHILD_ENV_ALLOWLIST}
        env[NO_DOTENV_ENV] = "1"
    else:
        env = dict(base)
    env["PYTHONUNBUFFERED"] = "1"  # stream log lines as they happen
    env["PYTHONIOENCODING"] = "utf-8"  # progress bars and "…" in log messages
    for account in request.accounts:
        if account.token:
            env[f"GITHUB_TOKEN_{_sanitize_login_for_env(account.login)}"] = account.token
    # Set even when empty: python-dotenv never overrides an existing variable,
    # so this keeps the .env copies out of the run. The form — pre-filled from
    # .env — is the single source of truth for these lists.
    values = {
        "author_emails": request.author_emails,
        "extra_repos": request.extra_repos,
        "extra_orgs": request.extra_orgs,
        "exclude_owners": request.exclude_owners,
    }
    for name, var in FORM_ENV_VARS.items():
        env[var] = ",".join(values[name])
    return env


def pdf_command(python: str, html_path: Path) -> list[str]:
    """The PDF step: ``python -m github_contrib.pdfexport <report.html>``."""
    return [python, "-m", "github_contrib.pdfexport", str(html_path)]


def pdf_env(base: Mapping[str, str]) -> dict[str, str]:
    """The PDF step's environment: operating-system variables only (it needs
    no token, and never reads .env)."""
    env = {k: v for k, v in base.items() if k.upper() in CHILD_ENV_ALLOWLIST}
    env.update({NO_DOTENV_ENV: "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def effective_env() -> dict[str, str]:
    """What the CLI child will see: ``.env`` values overlaid by the real environment."""
    values: dict[str, str] = {}
    try:
        from dotenv import dotenv_values  # type: ignore import-not-found

        values = {k: v for k, v in dotenv_values(PROJECT_ROOT / ".env").items() if v is not None}
    except Exception:  # pragma: no cover - python-dotenv is optional
        pass
    values.update(os.environ)
    return values


def env_token_logins(env: Mapping[str, str]) -> list[str]:
    """Logins that have a ``GITHUB_TOKEN_<LOGIN>`` token in ``env``.

    GitHub logins only contain letters, digits and hyphens, so the ``_`` in a
    variable suffix maps back to ``-`` (the same guess run.ps1 makes).
    """
    prefix = "GITHUB_TOKEN_"
    logins = {
        name[len(prefix):].lower().replace("_", "-")
        for name, value in env.items()
        if name.upper().startswith(prefix) and (value or "").strip()
    }
    return sorted(login for login in logins if _LOGIN_RE.match(login) and not login.isdigit())


# ---------------------------------------------------------------------------
# Token check (before a run)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TokenInfo:
    """What GitHub says about a token: ``GET /user``'s status, the account that
    owns it, and its scopes (``None`` for fine-grained tokens)."""

    status: int
    login: str = ""
    scopes: str | None = None


async def fetch_token_info(token: str) -> TokenInfo | None:
    """Ask GitHub who owns ``token`` (``None`` when GitHub can't be reached)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "CommitsTracker",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10), trust_env=True) as http:
            async with http.get("https://api.github.com/user", headers=headers) as response:
                data = await response.json(content_type=None) if response.status == 200 else {}
                login = str(data.get("login") or "") if isinstance(data, dict) else ""
                return TokenInfo(response.status, login, response.headers.get("X-OAuth-Scopes"))
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


def token_problem(login: str, token: str, info: TokenInfo | None) -> tuple[str, str] | None:
    """``(severity, message)`` when ``token`` would leave @login's report
    incomplete: "error" can't run, "warning" runs only if the user insists."""
    if info is None or info.status not in (200, 401):
        return None  # GitHub unreachable: the run itself reports any gap
    if info.status == 401:
        return "error", (
            f"This token doesn't work (expired or revoked). Create a new one while "
            f"signed in to GitHub as @{login}."
        )
    if info.login and info.login.lower() != login.lower():
        return "warning", (
            f"This token belongs to @{info.login}, not @{login}, so only repositories "
            f"@{info.login} can open would be scanned: @{login}'s private and organization "
            f"work would be missing. Create the token while signed in to GitHub as @{login}."
        )
    if token.startswith("github_pat_"):
        return "warning", (
            "Fine-grained tokens can't read repositories owned by other accounts or "
            "organizations. Use a classic token (ghp_…) with repo and read:org."
        )
    granted = {scope.strip() for scope in (info.scopes or "").split(",")}
    if info.scopes is not None and "repo" not in granted:
        return "warning", (
            "This token lacks the repo scope, so private repositories wouldn't be "
            "counted. Use a classic token with repo and read:org."
        )
    return None


def read_metrics(out_dir: Path) -> dict[str, str]:
    """Every metric of a finished run's contribution_summary.csv (empty when unavailable)."""
    try:
        with (out_dir / "contribution_summary.csv").open(encoding="utf-8-sig", newline="") as fh:
            return {row["metric"]: row["value"] for row in csv.DictReader(fh)}
    except (OSError, KeyError, csv.Error):
        return {}


def read_summary(out_dir: Path) -> dict[str, str]:
    """The headline metrics shown on a report card."""
    rows = read_metrics(out_dir)
    return {key: rows[key] for key in SUMMARY_KEYS if rows.get(key)}


async def pump_lines(stream: asyncio.StreamReader, on_line: Callable[[str], None]) -> None:
    """Feed ``stream`` to ``on_line`` one line at a time.

    Splits on ``\\r`` as well as ``\\n`` so every tqdm redraw arrives as its own line.
    """
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    while chunk := await stream.read(65536):
        *lines, pending = _NEWLINES.split(pending + decoder.decode(chunk))
        for line in lines:
            on_line(line)
    tail = pending + decoder.decode(b"", final=True)
    if tail:
        on_line(tail)


def _path_redactions(*paths: Path) -> list[tuple[str, str]]:
    """``(server path, placeholder)`` pairs, longest first, both slash styles."""
    labels = ("<report>", "<reports>", "<app>", "<python>", "<python>", "<home>")
    pairs: set[tuple[str, str]] = set()
    for path, label in zip(paths, labels):
        text = str(path)
        if len(text) < 4:  # never redact "/" or "C:\"
            continue
        pairs |= {(text, label), (text.replace("\\", "/"), label)}
    return sorted(pairs, key=lambda pair: -len(pair[0]))


def _kill_tree(process: asyncio.subprocess.Process) -> None:
    """Stop a run and everything it started (the browser rendering the PDF)."""
    if process.returncode is not None:
        return
    try:
        if sys.platform.startswith("win"):
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                capture_output=True, timeout=15, check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        pass
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


#: Owner of the jobs of a signed-in GitHub user (anonymous sessions use a sha256).
OWNER_PREFIX = "gh:"


def _owner_uid(owner: str) -> int | None:
    if owner.startswith(OWNER_PREFIX) and owner[len(OWNER_PREFIX):].isdigit():
        return int(owner[len(OWNER_PREFIX):])
    return None


def _split_period(period: str) -> tuple[str, str]:
    since, _, until = period.partition("..")
    since, until = since.strip(), until.strip()
    return (since if _DATE_RE.match(since) else "", until if _DATE_RE.match(until) else "")


def expired_report_json(row: ReportRow, login: str) -> dict:
    """A history row whose files are gone, shaped like a job for the page."""
    since, until = _split_period(row.period)
    status = row.status if row.status not in ACTIVE_STATUSES else "failed"  # its run died elsewhere
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {
        "id": row.id,
        "status": status,
        "created_at": created.astimezone(timezone.utc).isoformat(timespec="milliseconds"),
        "finished_at": "",
        "options": {"since": since, "until": until, "timezone": "UTC"},
        "accounts": [{"login": login, "emails": []}],
        "logins": [login],
        "files": {},
        "summary": {},
        "warnings": [],
        "error": "",
        "share_url": "",
        "log_count": 0,
        "expired": True,
    }


@dataclass(eq=False)
class Job:
    """One report run, from queued to finished."""

    id: str
    out_dir: Path
    accounts: list[dict]  # [{"login": ..., "emails": [...]}] - never tokens
    options: dict
    owner: str = ""  # sha256 of the session that created it
    created_at: str = field(default_factory=_now)
    finished_at: str = ""
    status: str = "queued"  # queued | running | done | failed | cancelled
    phase: str = "Queued"
    progress: dict | None = None  # {"label", "n", "total"} while a progress bar runs
    message: str = ""  # latest log message
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    summary: dict[str, str] = field(default_factory=dict)
    log: list[str] = field(default_factory=list)
    log_dropped: int = 0
    request: JobRequest | None = None  # holds the tokens; cleared when the run ends
    process: asyncio.subprocess.Process | None = None
    cancel_requested: bool = False
    delete_requested: bool = False
    timed_out: bool = False
    client: str = ""  # address that created it (rate limiting; never stored)
    # (secret or server path, replacement) applied to everything shown
    redactions: list[tuple[str, str]] = field(default_factory=list)
    # Secret part of the read-only link the owner shared ("" = not shared).
    share_token: str = ""
    # The PDF of a finished report: "rendering" while a browser prints it (the
    # report is already usable), then "ready" or "failed".
    pdf: str = ""
    # Report-history row (GitHub sign-in mode): what was last written, and
    # whether the owner deleted the report (the row must go too).
    db_added: bool = False
    db_status: str = ""
    forgotten: bool = False
    db_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def logins(self) -> list[str]:
        return [a["login"] for a in self.accounts]

    @property
    def user_id(self) -> int | None:
        """The GitHub user id of a signed-in owner ("gh:<id>"), else ``None``."""
        return _owner_uid(self.owner)

    @property
    def period(self) -> str:
        """The time range as stored in the history: "<since>..<until>"."""
        return f"{self.options.get('since') or ''}..{self.options.get('until') or ''}"

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def files(self) -> dict[str, str]:
        return {
            kind: f"/api/jobs/{self.id}/files/{kind}"
            for kind, name in REPORT_FILES.items()
            if (self.out_dir / name).is_file() and not (kind == "pdf" and self.pdf == "rendering")
        }

    def download_name(self, kind: str) -> str:
        who = "+".join(self.logins[:3]) + ("+more" if len(self.logins) > 3 else "")
        return f"github-report-{who}-{self.created_at[:10]}{Path(REPORT_FILES[kind]).suffix}"

    def record(self) -> dict:
        """The token-free state persisted to job.json."""
        return {
            "id": self.id,
            "owner": self.owner,
            "accounts": self.accounts,
            "options": self.options,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "error": self.error,
            "warnings": self.warnings,
            "summary": self.summary,
            "share_token": self.share_token,
            "pdf": self.pdf,
        }

    def to_json(self, share_base: str = "") -> dict:
        """What the owner's page sees (``share_base`` = public URL, if links are on)."""
        data = self.record()
        del data["owner"], data["share_token"]
        return {
            **data,
            "logins": self.logins,
            "phase": self.phase,
            "progress": self.progress,
            "message": self.message,
            "files": self.files(),
            "log_count": self.log_dropped + len(self.log),
            "share_url": f"{share_base}/shared/{self.share_token}" if share_base and self.share_token else "",
        }

    @classmethod
    def from_record(cls, data: dict, out_dir: Path) -> Job:
        """A finished job restored from a previous server session's job.json."""
        job = cls(
            id=out_dir.name,
            out_dir=out_dir,
            accounts=[{"login": a["login"], "emails": list(a.get("emails", []))} for a in data["accounts"]],
            options=dict(data.get("options", {})),
            owner=str(data.get("owner") or ""),
            created_at=data["created_at"],
            finished_at=data.get("finished_at", ""),
            status=data.get("status", "failed"),
            error=data.get("error", ""),
            warnings=list(data.get("warnings", [])),
            summary=dict(data.get("summary", {})),
        )
        token = str(data.get("share_token") or "")
        job.share_token = token if _SHARE_RE.match(token) else ""
        job.pdf = str(data.get("pdf") or "")
        # Its history row was written by the server that ran it.
        job.db_added, job.db_status = job.user_id is not None, job.status
        if job.active:  # the server stopped mid-run
            job.status = "failed"
            job.error = job.error or "The server stopped before this report finished."
            job.finished_at = job.finished_at or job.created_at
        if job.pdf == "rendering":  # ... or while printing the PDF
            job.pdf = "failed"
            job.warnings.append(PDF_STOPPED)
        return job

    def redact(self, text: str) -> str:
        for secret, replacement in self.redactions:
            if secret:
                text = text.replace(secret, replacement)
        return text

    def add_output(self, line: str) -> None:
        """Record one line of the child's output and update the live status."""
        line = self.redact(line).rstrip()
        text = line.strip()
        if not text:
            return
        bar = _PROGRESS_RE.match(text)
        if bar:
            label = bar["label"].strip()
            self.progress = {"label": label, "n": int(bar["n"]), "total": int(bar["total"])}
            self.phase = _PROGRESS_PHASES.get(label, self.phase)
            return

        self.log.append(line)
        if len(self.log) > MAX_LOG_LINES:
            overflow = len(self.log) - MAX_LOG_LINES
            del self.log[:overflow]
            self.log_dropped += overflow

        match = _LOG_LINE_RE.match(text)
        level, msg = (match["level"], match["msg"]) if match else ("", text)
        if level == "WARNING" and msg not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(msg)
        elif level in ("ERROR", "CRITICAL") or msg.startswith("Configuration error") or ": error:" in msg:
            self.error = msg
        if level in ("INFO", "WARNING"):
            self.message = msg
        for fragment, phase in _LOG_PHASES:
            if fragment in msg:
                self.phase, self.progress = phase, None

    def log_since(self, since: int) -> tuple[list[str], int]:
        """Log lines from absolute index ``since`` on, plus the next index."""
        start = max(since - self.log_dropped, 0)
        return self.log[start:], self.log_dropped + len(self.log)

    def load_run_log(self) -> None:
        """Fill the log of a restored job from the run.log the CLI wrote."""
        try:
            lines = (self.out_dir / "run.log").read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        self.log = [self.redact(line) for line in lines[-MAX_LOG_LINES:]]
        self.log_dropped = 0


class JobManager:
    """Owns every job: admits and queues them, runs them, keeps history."""

    def __init__(self, settings: Settings, store: Store | None = None) -> None:
        self.settings = settings
        #: Report history (GitHub sign-in mode only); ``None`` otherwise.
        self.store = store
        self.jobs_dir = Path(settings.jobs_dir).resolve()
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(max(1, settings.parallel))
        self._tasks: set[asyncio.Task] = set()
        self._recent: dict[str, collections.deque[float]] = {}
        #: Report ids deleted while this server runs (never listed again, even
        #: if deleting the history row failed).
        self.forgotten: set[str] = set()
        # user id -> (fetched at, rows): the page polls every second or so
        # while a report runs; the history only changes on delete or expiry.
        self._history_cache: dict[int, tuple[float, list[ReportRow]]] = {}

    # -- visibility & admission -------------------------------------------

    def visible_to(self, job: Job, owner: str) -> bool:
        """Public mode: only the session that created a report can see it."""
        return not self.settings.public or (bool(job.owner) and secrets.compare_digest(job.owner, owner))

    def listing(self, owner: str) -> list[Job]:
        jobs = [job for job in self.jobs.values() if self.visible_to(job, owner)]
        return sorted(jobs, key=lambda job: job.created_at, reverse=True)

    def admit(self, owner: str, client: str) -> tuple[str, int] | None:
        """Why a new run cannot start now (message, HTTP status), or ``None``."""
        active = [job for job in self.jobs.values() if job.active]
        if len(active) >= self.settings.max_queue:
            return "The server is busy. Try again in a few minutes.", 503
        mine = sum(1 for job in active if job.owner == owner)
        if self.settings.max_active_per_user and mine >= self.settings.max_active_per_user:
            return (
                f"You already have {mine} report(s) queued or running. "
                "Wait for one to finish, or cancel it.",
                429,
            )
        if self.settings.max_jobs_per_hour:
            window = self._recent.setdefault(self._rate_key(owner, client), collections.deque())
            cutoff = time.monotonic() - 3600
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= self.settings.max_jobs_per_hour:
                who = "you" if self.settings.auth else "this address"
                return f"Too many reports from {who} in the last hour. Try again later.", 429
        return None

    def _rate_key(self, owner: str, client: str) -> str:
        """Hourly limits count per account when signed in, else per client address."""
        return owner if self.settings.auth else client

    # -- lifecycle ----------------------------------------------------------

    def load_history(self) -> None:
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = Job.from_record(json.loads(path.read_text(encoding="utf-8")), path.parent)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("skipping unreadable %s: %s", path, exc)
                continue
            job.redactions = self._server_paths(job.out_dir)
            self.jobs[job.id] = job

    def _server_paths(self, out_dir: Path) -> list[tuple[str, str]]:
        if not self.settings.public:
            return []
        return _path_redactions(
            out_dir, self.jobs_dir, PROJECT_ROOT, Path(sys.prefix), Path(sys.base_prefix), Path.home()
        )

    def submit(self, request: JobRequest, *, owner: str = "", client: str = "") -> Job:
        if self.store is not None:
            # The history row's id: a uuid, with no login in it.
            job_id = str(uuid.uuid4())
        else:
            names = "+".join(request.logins[:3])
            job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{names}-{secrets.token_hex(8)}"
        out_dir = self.jobs_dir / job_id
        secrets_ = [(a.token, "••••") for a in request.accounts if a.token]
        job = Job(
            id=job_id,
            out_dir=out_dir,
            accounts=[{"login": a.login, "emails": a.emails} for a in request.accounts],
            options=request.options(),
            owner=owner,
            request=request,
            client=client,
            redactions=secrets_ + self._server_paths(out_dir),
        )
        self.jobs[job.id] = job
        if self.settings.max_jobs_per_hour:
            key = self._rate_key(owner, client)
            self._recent.setdefault(key, collections.deque()).append(time.monotonic())
        self._sync_soon(job)  # the history row, as "queued" (never delays the run)
        self._spawn(self._run(job))
        return job

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def cancel(self, job: Job) -> None:
        if not job.active:
            return
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.request = None  # _run skips it once it gets a slot
            self._sync_soon(job)
        elif job.process is not None:
            _kill_tree(job.process)

    def delete(self, job: Job) -> None:
        """Remove a report, its files and its history row (an active run is
        cancelled first). Await :meth:`sync` afterwards to know the row is gone."""
        job.forgotten = True
        self.forgotten.add(job.id)
        self._sync_soon(job)
        if job.active:
            job.delete_requested = True
            self.cancel(job)
            if job.status == "cancelled" and job.process is None:
                self._remove(job)  # it never started
            return
        if job.pdf == "rendering":
            job.delete_requested = True  # _render_pdf removes it once the browser stops
            if job.process is not None:
                _kill_tree(job.process)
            return
        self._remove(job)

    # -- report history (GitHub sign-in mode) ------------------------------

    def _sync_soon(self, job: Job) -> None:
        if self.store is not None and job.user_id is not None:
            self._spawn(self.sync(job))

    async def sync(self, job: Job) -> None:
        """Bring the job's history row up to date: insert it, update its
        status, or delete it. Writes for one job never overlap, and each
        writes the job's state at that moment. Errors are logged, never raised."""
        store, user_id = self.store, job.user_id
        if store is None or user_id is None:
            return
        async with job.db_lock:
            try:
                if job.forgotten:
                    await store.delete_report(job.id, user_id)
                    job.db_added = False
                elif not job.db_added:
                    status = job.status
                    await store.add_report(job.id, user_id, job.period, status)
                    job.db_added, job.db_status = True, status
                elif job.db_status != job.status:
                    status = job.status
                    await store.set_status(job.id, status)
                    job.db_status = status
            except Exception as exc:  # noqa: BLE001 - history is best effort
                log.warning("report history: could not update %s (%s)", job.id, exc.__class__.__name__)

    async def history(self, user_id: int, max_age: float = HISTORY_CACHE_SECONDS) -> list[ReportRow]:
        """The user's history rows ([] when the database is unavailable)."""
        if self.store is None:
            return []
        cached = self._history_cache.get(user_id)
        if cached is not None and time.monotonic() - cached[0] < max_age:
            return cached[1]
        try:
            rows = await self.store.list_reports(user_id, limit=100)
        except Exception as exc:  # noqa: BLE001 - the page works without it
            log.warning("report history: could not list reports (%s)", exc.__class__.__name__)
            # Serve what we had (or nothing) until the next try, not on every poll.
            rows = cached[1] if cached is not None else []
        if len(self._history_cache) > 10_000:
            self._history_cache.clear()
        self._history_cache[user_id] = (time.monotonic(), rows)
        return rows

    async def forget_row(self, report_id: str, user_id: int) -> bool | None:
        """Delete a history row with no job behind it (None = database unavailable)."""
        if self.store is None:
            return False
        try:
            deleted = await self.store.delete_report(report_id, user_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("report history: could not delete a report (%s)", exc.__class__.__name__)
            return None
        if deleted:
            self.forgotten.add(report_id)
            self._history_cache.pop(user_id, None)
        return deleted

    async def start_store(self, _app: web.Application | None = None) -> None:
        """Connect, and mark rows whose runs died with the previous server as failed."""
        if self.store is None:
            return
        try:
            await self.store.start()
            stale = await self.store.fail_unfinished()
            if stale:
                log.info("report history: marked %d unfinished report(s) as failed", stale)
        except Exception as exc:  # noqa: BLE001 - the app runs without history
            log.warning("report history unavailable at startup (%s)", exc.__class__.__name__)

    async def close_store(self, _app: web.Application | None = None) -> None:
        if self.store is not None:
            try:
                await self.store.close()
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass

    def share(self, job: Job) -> str:
        """Create (or reuse) the job's read-only link token."""
        if not job.share_token:
            job.share_token = secrets.token_urlsafe(24)
            self._save(job)
        return job.share_token

    def unshare(self, job: Job) -> None:
        if job.share_token:
            job.share_token = ""
            self._save(job)

    def by_share_token(self, token: str) -> Job | None:
        """The finished job a share link points to (constant-time comparison)."""
        if not _SHARE_RE.match(token):
            return None
        for job in self.jobs.values():
            if job.share_token and secrets.compare_digest(job.share_token, token):
                return job if job.status == "done" else None
        return None

    def _remove(self, job: Job) -> None:
        self.jobs.pop(job.id, None)
        target = job.out_dir.resolve()
        # Only ever delete a direct child of the reports directory.
        if target.parent == self.jobs_dir and target.is_dir():
            shutil.rmtree(target, ignore_errors=True)

    def expire(self, now: datetime | None = None) -> int:
        """Delete reports older than the retention period; returns how many."""
        hours = self.settings.retention_hours
        if hours <= 0:
            return 0
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(hours=hours)
        expired = []
        for job in list(self.jobs.values()):
            finished = job.finished_at or job.created_at
            try:
                moment = datetime.fromisoformat(finished)
            except ValueError:
                continue
            if not job.active and moment < cutoff:
                expired.append(job)
        for job in expired:
            self._remove(job)  # the history row stays: the page lists it as expired
        if expired:
            self._history_cache.clear()
            log.info("deleted %d report(s) older than %g hour(s)", len(expired), hours)
        return len(expired)

    async def expiry_loop(self) -> None:
        while True:
            try:
                self.expire()
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("report clean-up failed")
            await asyncio.sleep(600)

    async def shutdown(self, _app: web.Application | None = None) -> None:
        for job in list(self.jobs.values()):
            self.cancel(job)
            if job.pdf == "rendering" and job.process is not None:
                _kill_tree(job.process)
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)

    # -- running ------------------------------------------------------------

    async def _run(self, job: Job) -> None:
        async with self._slots:
            try:
                if not job.cancel_requested:
                    await self._execute(job)
            except Exception as exc:  # noqa: BLE001 - one broken run must not take the server down
                log.exception("report %s crashed", job.id)
                job.status, job.error = "failed", job.redact(str(exc) or exc.__class__.__name__)
            finally:
                await self._wrap_up(job)
            # The report is done and usable now; the PDF follows. It keeps the
            # slot, so one browser at a time shares the host with report runs.
            if job.status == "done" and not job.delete_requested:
                await self._render_pdf(job)

    async def _wrap_up(self, job: Job) -> None:
        """A run has ended: settle its status, drop its tokens, save it."""
        if job.active:
            job.status = "cancelled" if job.cancel_requested else "failed"
        self._scrub_run_log(job)
        job.request = job.process = job.progress = None  # drop the tokens
        job.redactions = self._server_paths(job.out_dir)
        job.finished_at = _now()
        if job.status == "done":  # decided before anyone sees the job as done
            job.pdf = "ready" if "pdf" in job.files() else "rendering"
        if job.delete_requested:
            self._remove(job)
        else:
            self._save(job)
        await self.sync(job)

    async def _render_pdf(self, job: Job) -> None:
        """Print report.html to report.pdf, in a process of its own started
        after the report run has exited (so its memory is free again). Never
        raises: without a PDF the report is still complete."""
        if job.pdf != "rendering":
            return
        html_path = job.out_dir / REPORT_FILES["html"]
        if find_browser() is None or not html_path.is_file():
            job.pdf = "failed"
            job.warnings.append(PDF_NO_BROWSER)
            self._save(job)
            return
        job.phase = "Rendering PDF"
        try:
            process = await asyncio.create_subprocess_exec(
                *pdf_command(self.settings.python, html_path),
                cwd=PROJECT_ROOT,
                env=pdf_env(os.environ),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Its own process group, so stopping it also stops the browser.
                **({"start_new_session": True} if not sys.platform.startswith("win") else {}),
            )
            job.process = process
            assert process.stdout is not None

            async def drain() -> int:
                await pump_lines(process.stdout, job.add_output)
                return await process.wait()

            try:
                # pdfexport enforces PDF_TIMEOUT itself; this is the backstop.
                await asyncio.wait_for(drain(), timeout=pdf_timeout() + 60)
            except asyncio.TimeoutError:
                _kill_tree(process)
                await process.wait()
        except Exception:  # noqa: BLE001 - the report must survive a broken PDF step
            log.exception("PDF of report %s failed", job.id)
        finally:
            job.process = None
        job.pdf = "ready" if (job.out_dir / REPORT_FILES["pdf"]).is_file() else "failed"
        if job.pdf == "failed" and not job.delete_requested:
            job.warnings.append(PDF_FAILED)
        if job.delete_requested:
            self._remove(job)
        else:
            self._save(job)

    async def _execute(self, job: Job) -> None:
        request = job.request
        assert request is not None
        settings = self.settings
        job.status, job.phase = "running", "Starting"
        self._sync_soon(job)
        job.out_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *build_command(request, job.out_dir, settings.python, settings.script),
            cwd=PROJECT_ROOT,
            env=build_env(request, os.environ, isolated=settings.public),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Its own process group, so stopping it also stops the PDF browser.
            **({"start_new_session": True} if not sys.platform.startswith("win") else {}),
        )
        job.process = process
        if job.cancel_requested:  # cancelled while the process was starting
            _kill_tree(process)
        assert process.stdout is not None

        async def drain() -> int:
            await pump_lines(process.stdout, job.add_output)
            return await process.wait()

        try:
            code = await asyncio.wait_for(drain(), timeout=settings.job_timeout or None)
        except asyncio.TimeoutError:
            job.timed_out = True
            _kill_tree(process)
            code = await process.wait()

        if job.timed_out:
            job.status = "failed"
            job.error = (
                f"The report took longer than {settings.job_timeout / 60:.0f} minutes and was "
                "stopped. Try a shorter time range, or turn off line-level stats."
            )
        elif job.cancel_requested:
            job.status = "cancelled"
        elif code == 0:
            job.status = "done"
            job.summary = read_summary(job.out_dir)
        else:
            job.status = "failed"
            job.error = job.error or (job.log[-1].strip() if job.log else f"The run exited with code {code}.")

    def _scrub_run_log(self, job: Job) -> None:
        """Make sure run.log on disk holds no token (or server path in public mode)."""
        path = job.out_dir / "run.log"
        try:
            original = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        cleaned = job.redact(original)
        if cleaned != original:
            try:
                path.write_text(cleaned, encoding="utf-8", newline="")
            except OSError as exc:
                log.warning("could not scrub %s/run.log: %s", job.id, exc)

    def _save(self, job: Job) -> None:
        try:
            job.out_dir.mkdir(parents=True, exist_ok=True)
            (job.out_dir / "job.json").write_text(json.dumps(job.record(), indent=2), encoding="utf-8")
        except OSError as exc:
            log.warning("could not save %s/job.json: %s", job.id, exc)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

MANAGER = web.AppKey("manager", JobManager)
SETTINGS = web.AppKey("settings", Settings)
EXPIRY_TASK = web.AppKey("expiry_task", asyncio.Task)
CODEC = web.AppKey("session_codec", gh_auth.SessionCodec)
#: The session's owner id on each request: sha256 of the anonymous session
#: cookie, or "gh:<GitHub user id>" when signed in with GitHub.
OWNER = web.RequestKey("owner", str) if hasattr(web, "RequestKey") else "owner"
#: The signed-in GitHub session (GitHub sign-in mode), or None.
AUTH = web.RequestKey("auth_session", gh_auth.AuthSession) if hasattr(web, "RequestKey") else "auth_session"
#: API routes that answer without signing in.
_OPEN_API = frozenset({"/api/config", "/api/me"})
_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")

_INDEX_CSP = "; ".join(
    (
        "default-src 'self'",
        "img-src 'self' data: https://avatars.githubusercontent.com https://github.com",
        "style-src 'self' 'unsafe-inline'",
        "script-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
#: report.html opens in a sandbox: an opaque origin with no cookies, storage
#: or network access, on top of the page's own policy.
_REPORT_HTML_CSP = f"sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox; {REPORT_CSP}"


def _client_address(request: web.Request, settings: Settings) -> str:
    if settings.trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]  # the address our own proxy saw
    return request.remote or ""


def _guard(settings: Settings):
    """Middleware: host and cross-site checks, plus the session cookie."""

    @web.middleware
    async def guard(request: web.Request, handler):
        if not settings.public and urlsplit(f"//{request.host}").hostname not in LOOPBACK_HOSTS:
            # A foreign domain resolving to 127.0.0.1 (DNS rebinding).
            raise web.HTTPForbidden(text="Unexpected Host header.")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if settings.public:
                if (origin or "").lower() != settings.public_origin:
                    raise web.HTTPForbidden(text="Cross-site request refused.")
            elif origin is not None and urlsplit(origin).netloc != request.host:
                raise web.HTTPForbidden(text="Cross-site request refused.")
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType(text="Send JSON.")

        if settings.auth:
            return await _signed_in_only(request, handler, settings)

        session = request.cookies.get(settings.session_cookie, "")
        fresh = not _SESSION_RE.match(session)
        if fresh:
            session = secrets.token_urlsafe(32)
        request[OWNER] = hashlib.sha256(session.encode("ascii")).hexdigest()
        response = await handler(request)
        # People opening a shared link get no session: they own nothing here.
        if fresh and not request.path.startswith(("/shared/", "/healthz")):
            response.set_cookie(
                settings.session_cookie,
                session,
                max_age=SESSION_MAX_AGE,
                path="/",
                httponly=True,
                secure=settings.secure,
                samesite="Strict",
            )
        return response

    return guard


async def _signed_in_only(request: web.Request, handler, settings: Settings) -> web.StreamResponse:
    """GitHub sign-in mode: who is signed in, and 401 for the API without a session."""
    raw = request.cookies.get(settings.auth_cookie, "")
    session = request.app[CODEC].decode(raw) if raw else None
    request[AUTH] = session
    request[OWNER] = f"{OWNER_PREFIX}{session.uid}" if session else ""
    if session is None and request.path.startswith("/api/") and request.path not in _OPEN_API:
        response: web.StreamResponse = _error("Sign in with GitHub first.", 401)
    else:
        response = await handler(request)
    if raw and session is None and settings.auth_cookie not in response.cookies:
        _clear_cookie(response, settings.auth_cookie, settings)  # forged or expired
    return response


def _clear_cookie(response: web.StreamResponse, name: str, settings: Settings) -> None:
    response.del_cookie(name, path="/", secure=settings.secure, httponly=True, samesite="Lax")


def _security_headers(settings: Settings):
    async def on_prepare(request: web.Request, response: web.StreamResponse) -> None:
        headers = response.headers
        headers["Server"] = "CommitsTracker"
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("Referrer-Policy", "no-referrer")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        headers.setdefault("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        private = request.path.startswith(("/api/", "/shared/", "/auth/"))
        headers.setdefault("Cache-Control", "no-store" if private else "no-cache")
        if request.path.startswith("/shared/"):
            # Shared reports are for the people given the link, not search engines.
            headers.setdefault("X-Robots-Tag", "noindex, nofollow, noarchive")
        if settings.secure:
            headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    return on_prepare


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _job(request: web.Request) -> Job:
    manager = request.app[MANAGER]
    job = manager.jobs.get(request.match_info["job_id"])
    if job is None or not manager.visible_to(job, request[OWNER]):
        raise web.HTTPNotFound(text="No such report.")
    return job


def _display_path(path: Path) -> str:
    try:
        return path.relative_to(PROJECT_ROOT).as_posix() + "/"
    except ValueError:
        return str(path)


async def _index(_request: web.Request) -> web.FileResponse:
    response = web.FileResponse(WEB_DIR / "index.html")
    response.headers["Content-Security-Policy"] = _INDEX_CSP
    return response


async def _config(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    if settings.public:
        # Never reveal anything about the server's own environment.
        local: dict[str, object] = {"env_logins": [], "has_default_token": False, "output_dir": ""}
    else:
        # Which logins have a token in .env — only so the page can say "leave
        # the token blank" once that username is typed. The form itself is
        # never pre-filled, and .env emails / lists are never sent.
        env = effective_env()
        local = {
            "env_logins": env_token_logins(env),
            "has_default_token": bool((env.get("GITHUB_TOKEN") or "").strip()),
            "output_dir": _display_path(request.app[MANAGER].jobs_dir),
        }
    return web.json_response(
        {
            "version": __version__,
            "mode": "public" if settings.public else "local",
            "auth": settings.auth,
            "retention_hours": settings.retention_hours,
            "max_active_per_user": settings.max_active_per_user,
            "pdf_browser": find_browser() is not None,
            **local,
        }
    )


async def _healthz(_request: web.Request) -> web.Response:
    return web.Response(text="ok")


async def _me(request: web.Request) -> web.Response:
    session = request.get(AUTH)
    if session is None:
        return web.json_response({"signed_in": False})
    return web.json_response(
        {
            "signed_in": True,
            "login": session.login,
            "id": session.uid,
            "avatar_url": gh_auth.avatar_url(session.uid),
        }
    )


# -- GitHub sign-in (OAuth App web flow) -------------------------------------


def _redirect(location: str) -> web.Response:
    return web.Response(status=302, headers={"Location": location})


async def _auth_login(request: web.Request) -> web.Response:
    """Send the browser to GitHub, with a one-time state tied to this browser."""
    settings = request.app[SETTINGS]
    state = secrets.token_urlsafe(32)
    response = _redirect(
        gh_auth.authorize_url(settings.github_client_id, settings.redirect_uri, state)
    )
    # Lax, not Strict: it must come back with the redirect from github.com.
    response.set_cookie(
        settings.state_cookie,
        state,
        max_age=gh_auth.STATE_TTL,
        path="/",
        httponly=True,
        secure=settings.secure,
        samesite="Lax",
    )
    return response


async def _auth_callback(request: web.Request) -> web.Response:
    """GitHub sends the browser back here with ?code&state. Every failure ends
    on "/?auth_error=<code>" — GitHub's own error text is never shown."""
    settings = request.app[SETTINGS]
    manager = request.app[MANAGER]
    query = request.query

    def finish(location: str) -> web.Response:
        response = _redirect(location)
        _clear_cookie(response, settings.state_cookie, settings)
        return response

    expected = request.cookies.get(settings.state_cookie, "")
    state = query.get("state", "")
    if not expected or not state or not secrets.compare_digest(state.encode(), expected.encode()):
        return finish("/?auth_error=state")
    if query.get("error"):
        return finish("/?auth_error=" + ("denied" if query["error"] == "access_denied" else "github"))
    code = query.get("code", "")
    if not _CODE_RE.match(code):
        return finish("/?auth_error=code")
    try:
        token = await gh_auth.exchange_code(
            settings.github_client_id, settings.github_client_secret, code, settings.redirect_uri
        )
        uid, login = await gh_auth.fetch_user(token)
    except gh_auth.AuthError as exc:
        log.warning("GitHub sign-in failed: %s", exc)
        return finish(f"/?auth_error={exc.code}")
    except Exception as exc:  # noqa: BLE001 - network trouble, bad JSON...
        log.warning("GitHub sign-in failed (%s)", exc.__class__.__name__)
        return finish("/?auth_error=github")
    # The token only served to learn who signed in: it is never kept.
    manager._spawn(_revoke_sign_in_token(settings, token))
    if manager.store is not None:
        try:
            await manager.store.ensure_user(uid)
        except Exception as exc:  # noqa: BLE001 - sign-in works without the history
            log.warning("report history: could not record a user (%s)", exc.__class__.__name__)
    response = finish("/")
    response.set_cookie(
        settings.auth_cookie,
        request.app[CODEC].encode(uid, login),
        max_age=gh_auth.SESSION_TTL,
        path="/",
        httponly=True,
        secure=settings.secure,
        # Strict would drop the cookie on the first page load after GitHub's redirect.
        samesite="Lax",
    )
    return response


async def _revoke_sign_in_token(settings: Settings, token: str) -> None:
    try:
        await gh_auth.revoke_token(settings.github_client_id, settings.github_client_secret, token)
    except Exception as exc:  # noqa: BLE001 - best effort: it grants no permissions anyway
        log.warning("could not revoke a sign-in token (%s)", exc.__class__.__name__)


async def _auth_logout(request: web.Request) -> web.Response:
    """Sign out: clear the cookie (the session holds no GitHub token)."""
    response = web.json_response({"ok": True})
    _clear_cookie(response, request.app[SETTINGS].auth_cookie, request.app[SETTINGS])
    return response


def _share_base(request: web.Request) -> str:
    """Share links exist only when the site has a public address."""
    settings = request.app[SETTINGS]
    return settings.public_url if settings.public else ""


async def _list_jobs(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    base = _share_base(request)
    items = [job.to_json(base) for job in manager.listing(request[OWNER])]
    session = request.get(AUTH)
    if session is not None and manager.store is not None:
        # History rows whose files are gone (expired, or lost with a restart).
        rows = await manager.history(session.uid)
        items += [
            expired_report_json(row, session.login)
            for row in rows
            if row.id not in manager.jobs and row.id not in manager.forgotten
        ]
        items.sort(key=lambda item: item["created_at"], reverse=True)
    return web.json_response({"jobs": items})


async def _create_job(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    manager = request.app[MANAGER]
    try:
        payload = await request.json()
    except ValueError:
        return _error("The request was not valid JSON.")
    try:
        job_request = parse_job_request(
            payload, {} if settings.public else effective_env(), require_tokens=settings.public
        )
    except RequestError as exc:
        return _error(str(exc))
    client = _client_address(request, settings)
    refusal = manager.admit(request[OWNER], client)
    if refusal is not None:
        return _error(*refusal)
    job = manager.submit(job_request, owner=request[OWNER], client=client)
    log.info("queued report %s", job.id)
    return web.json_response(job.to_json(_share_base(request)), status=201)


async def _check_tokens(request: web.Request) -> web.Response:
    """Before a run: does each typed token belong to its account and see
    private repositories? Blank tokens (.env in local mode) are not checked."""
    try:
        payload = await request.json()
    except ValueError:
        return _error("The request was not valid JSON.")
    raw = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or len(raw) > MAX_ACCOUNTS:
        return _error(f"Send a list of at most {MAX_ACCOUNTS} accounts.")
    rows: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            return _error("Each account must be an object.")
        login = str(item.get("login") or "").strip().lstrip("@")
        token = str(item.get("token") or "").strip()
        if _LOGIN_RE.match(login) and _TOKEN_RE.match(token):
            rows.append((login, token))
    tokens = list(dict.fromkeys(token for _login, token in rows))
    infos = dict(zip(tokens, await asyncio.gather(*(fetch_token_info(token) for token in tokens))))
    problems = []
    for login, token in rows:
        found = token_problem(login, token, infos[token])
        if found is not None:
            problems.append({"login": login, "severity": found[0], "message": found[1]})
    return web.json_response({"problems": problems})


async def _cancel_job(request: web.Request) -> web.Response:
    job = _job(request)
    request.app[MANAGER].cancel(job)
    return web.json_response(job.to_json(_share_base(request)))


async def _share_job(request: web.Request) -> web.Response:
    """Create the read-only link for a finished report (owner only)."""
    job = _job(request)
    if not _share_base(request):
        return _error("Share links need the hosted (public) mode: run webui.py with --public-url.")
    if job.status != "done":
        return _error("Only a finished report can be shared.", 409)
    request.app[MANAGER].share(job)
    return web.json_response(job.to_json(_share_base(request)))


async def _unshare_job(request: web.Request) -> web.Response:
    """Turn the link off: everyone who had it loses access at once."""
    job = _job(request)
    request.app[MANAGER].unshare(job)
    return web.json_response(job.to_json(_share_base(request)))


async def _linkedin(request: web.Request) -> web.Response:
    """A LinkedIn-ready draft built from the report's numbers (owner only)."""
    job = _job(request)
    if job.status != "done":
        return _error("The LinkedIn summary is ready once the report has finished.", 409)
    metrics = read_metrics(job.out_dir)
    if not metrics:
        return _error("This report has no summary to build a post from.", 409)
    text = build_linkedin_post(metrics)
    message = "" if text else "There's no activity in this report to post about."
    return web.json_response({"text": text, "message": message, "limit": LINKEDIN_LIMIT})


async def _delete_job(request: web.Request) -> web.Response:
    manager = request.app[MANAGER]
    session = request.get(AUTH)
    job_id = request.match_info["job_id"]
    if session is not None and manager.store is not None and job_id not in manager.jobs:
        # An expired history entry: only its row is left.
        deleted = await manager.forget_row(job_id, session.uid)
        if deleted is None:
            return _error("The report history is unavailable right now. Try again in a minute.", 503)
        if not deleted:
            raise web.HTTPNotFound(text="No such report.")
        return web.json_response({"deleted": job_id})
    job = _job(request)
    manager.delete(job)
    await manager.sync(job)  # the row is gone before the page lists reports again
    return web.json_response({"deleted": job.id})


async def _job_log(request: web.Request) -> web.Response:
    job = _job(request)
    try:
        since = max(int(request.query.get("since", "0")), 0)
    except ValueError:
        since = 0
    if not job.log and not job.active:
        job.load_run_log()
    lines, next_index = job.log_since(since)
    return web.json_response({"lines": lines, "next": next_index})


async def _job_file(request: web.Request) -> web.FileResponse:
    job = _job(request)
    kind = request.match_info["kind"]
    name = REPORT_FILES.get(kind)
    path = job.out_dir / name if name else None
    if path is None or not path.is_file():
        raise web.HTTPNotFound(text="That file isn't available for this report.")
    # HTML opens in a tab; everything else (and ?download) saves to disk.
    disposition = "inline" if kind == "html" and "download" not in request.query else "attachment"
    response = web.FileResponse(path)
    response.headers["Content-Disposition"] = f'{disposition}; filename="{job.download_name(kind)}"'
    if kind == "html":
        response.headers["Content-Security-Policy"] = _REPORT_HTML_CSP
    return response


# -- shared links (read-only, no session) -----------------------------------

#: Files a shared link exposes — never the logs, the workbook or the data.
SHARED_FILES = ("pdf", "html")

_SHARED_CSP = (
    "default-src 'none'; style-src 'self'; img-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _range_text(since: str, until: str) -> str:
    """'1 Jul – 31 Jul 2026' for the dates of a report's period."""
    def day(text: str, with_year: bool = True) -> str:
        moment = datetime.strptime(text, "%Y-%m-%d")
        label = f"{moment.day} {_MONTH_ABBR[moment.month - 1]}"
        return f"{label} {moment.year}" if with_year else label

    try:
        if since and until:
            return f"{day(since, since[:4] != until[:4])} – {day(until)}"
        if since:
            return f"From {day(since)}"
        if until:
            return f"Until {day(until)}"
    except ValueError:
        pass
    return "All time"


def _shared_job(request: web.Request) -> Job:
    job = request.app[MANAGER].by_share_token(request.match_info["token"])
    if job is None:
        raise web.HTTPNotFound(text="This link doesn't work any more. Ask for a new one.")
    return job


async def _shared_page(request: web.Request) -> web.Response:
    """The page someone opens from a shared link: what the report covers and
    links to the PDF / HTML. No logs, emails or data files."""
    job = _shared_job(request)
    token = request.match_info["token"]
    names = " + ".join(job.logins)
    summary = read_summary(job.out_dir)
    options = job.options
    stats = [(summary.get("total_lifetime_commits"), "commit", "commits")]
    if options.get("pull_requests", True):
        stats.append((summary.get("total_pull_requests"), "pull request", "pull requests"))
    stats += [
        (summary.get("repositories_contributed_to"), "repository", "repositories"),
        (summary.get("active_days"), "active day", "active days"),
    ]
    def number(value: object) -> int | None:
        try:
            return int(float(str(value)))
        except (TypeError, ValueError):
            return None

    stat_html = "".join(
        f"<span><b>{n:,}</b> {one if n == 1 else many}</span>"
        for n, one, many in ((number(value), one, many) for value, one, many in stats)
        if n is not None
    )
    added = number(summary.get("total_lines_added"))
    if added:
        deleted = number(summary.get("total_lines_deleted")) or 0
        stat_html += f"<span><b>+{added:,}</b> / <b>−{deleted:,}</b> lines</span>"
    period = _range_text(str(options.get("since") or ""), str(options.get("until") or ""))
    zone = str(options.get("timezone") or summary.get("report_timezone") or "UTC")
    files = job.files()
    base = f"/shared/{token}/files"
    buttons = []
    if "pdf" in files:
        buttons.append(f'<a class="btn btn-primary" href="{base}/pdf">Open PDF</a>')
        buttons.append(f'<a class="btn btn-outline" href="{base}/pdf?download">Download</a>')
    if "html" in files:
        buttons.append(
            f'<a class="btn btn-outline" href="{base}/html" target="_blank" '
            'rel="noopener noreferrer">View online</a>'
        )
    esc = html.escape
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Contribution report · {esc(names)}</title>
<link rel="icon" href="/static/favicon.svg" type="image/svg+xml">
<link rel="stylesheet" href="/static/app.css">
</head>
<body>
<main class="shared">
  <header class="topbar">
    <span class="brand" aria-hidden="true"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M2 12h6M16 12h6"/></svg></span>
    <h1>Contribution report</h1>
  </header>
  <article class="shared-card">
    <h2 class="shared-name">{esc(names)}</h2>
    <p class="report-sub">{esc(period)} · {esc(zone)}</p>
    <div class="stats">{stat_html}</div>
    <div class="report-actions">{''.join(buttons)}</div>
    <p class="hint">Generated {esc(job.created_at[:10])}. Shared with you by link: only people who have the link can open it.</p>
  </article>
</main>
</body>
</html>"""
    response = web.Response(text=page, content_type="text/html")
    response.headers["Content-Security-Policy"] = _SHARED_CSP
    return response


async def _shared_file(request: web.Request) -> web.FileResponse:
    job = _shared_job(request)
    kind = request.match_info["kind"]
    path = job.out_dir / REPORT_FILES[kind] if kind in SHARED_FILES else None
    if path is None or not path.is_file():
        raise web.HTTPNotFound(text="That file isn't available for this report.")
    disposition = "attachment" if "download" in request.query else "inline"
    response = web.FileResponse(path)
    response.headers["Content-Disposition"] = f'{disposition}; filename="{job.download_name(kind)}"'
    if kind == "html":
        response.headers["Content-Security-Policy"] = _REPORT_HTML_CSP
    return response


def create_app(
    settings: Settings | None = None, *, store: Store | None = None, **legacy
) -> web.Application:
    """The aiohttp application. ``legacy`` keyword arguments build local
    :class:`Settings` (``jobs_dir``, ``python``, ``script``).

    In GitHub sign-in mode (``settings.auth``) the report history lives in
    ``store`` (default: Postgres at ``settings.database_url``, else memory).
    Raises ``ValueError`` when sign-in is configured without SESSION_SECRET.
    """
    if settings is None:
        jobs_dir = legacy.pop("jobs_dir", DEFAULT_JOBS_DIR)
        legacy.pop("loopback_only", None)
        settings = Settings(jobs_dir=Path(jobs_dir), **legacy)
    codec = None
    if settings.auth:
        codec = gh_auth.SessionCodec(settings.session_secret)  # ValueError without a secret
        if store is None:
            store = make_store(settings.database_url)
    else:
        store = None  # history needs a GitHub user id
    manager = JobManager(settings, store)
    manager.load_history()
    manager.expire()
    app = web.Application(middlewares=[_guard(settings)], client_max_size=256 * 1024)
    app[MANAGER] = manager
    app[SETTINGS] = settings
    app.router.add_get("/", _index)
    app.router.add_get("/healthz", _healthz)
    app.router.add_static("/static/", WEB_DIR)
    app.router.add_get("/api/config", _config)
    app.router.add_get("/api/me", _me)
    if codec is not None:
        app[CODEC] = codec
        app.router.add_get("/auth/login", _auth_login)
        app.router.add_get("/auth/callback", _auth_callback)
        app.router.add_post("/auth/logout", _auth_logout)
        app.on_startup.append(manager.start_store)
        app.on_cleanup.append(manager.close_store)
    app.router.add_get("/api/jobs", _list_jobs)
    app.router.add_post("/api/jobs", _create_job)
    app.router.add_post("/api/tokens/check", _check_tokens)
    app.router.add_post("/api/jobs/{job_id}/cancel", _cancel_job)
    app.router.add_delete("/api/jobs/{job_id}", _delete_job)
    app.router.add_get("/api/jobs/{job_id}/log", _job_log)
    app.router.add_get("/api/jobs/{job_id}/files/{kind}", _job_file)
    app.router.add_get("/api/jobs/{job_id}/linkedin", _linkedin)
    app.router.add_post("/api/jobs/{job_id}/share", _share_job)
    app.router.add_delete("/api/jobs/{job_id}/share", _unshare_job)
    app.router.add_get("/shared/{token}", _shared_page)
    app.router.add_get("/shared/{token}/files/{kind}", _shared_file)
    app.on_response_prepare.append(_security_headers(settings))
    app.on_shutdown.append(manager.shutdown)

    if settings.retention_hours > 0:

        async def start_expiry(app: web.Application) -> None:
            app[EXPIRY_TASK] = asyncio.create_task(manager.expiry_loop())

        async def stop_expiry(app: web.Application) -> None:
            app[EXPIRY_TASK].cancel()

        app.on_startup.append(start_expiry)
        app.on_cleanup.append(stop_expiry)
    return app


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    settings: Settings | None = None,
    open_browser: bool = True,
    ssl_context=None,
) -> None:
    """Run the web UI until interrupted."""
    settings = settings or Settings()
    loopback = host in LOOPBACK_HOSTS
    if not settings.public and not loopback:
        raise ValueError(
            "Local mode only listens on 127.0.0.1. To serve other people, run in public "
            "mode (--public-url https://...) behind an HTTPS reverse proxy."
        )
    app = create_app(settings)
    shown = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    scheme = "https" if ssl_context is not None else "http"
    url = settings.public_url or f"{scheme}://{f'[{shown}]' if ':' in shown else shown}:{port}/"
    if open_browser and not settings.public:

        async def _open_browser(_app: web.Application) -> None:
            asyncio.get_running_loop().call_later(0.5, webbrowser.open, url)

        app.on_startup.append(_open_browser)
    mode = "public" if settings.public else "local"
    if settings.auth:
        mode += " mode, GitHub sign-in"
    else:
        mode += " mode"
    print(f"Contribution report UI ({mode}) running at {url}  (Ctrl+C to stop)", flush=True)
    web.run_app(app, host=host, port=port, access_log=None, print=None, ssl_context=ssl_context)
