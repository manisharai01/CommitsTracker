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
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .config import (
    NO_DOTENV_ENV,
    ConfigError,
    _sanitize_login_for_env,
    resolve_period,
    resolve_timezone,
    token_env_candidates,
)
from .htmlreport import REPORT_CSP
from .logging_config import get_logger
from .pdfexport import find_browser

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
    )
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
MAX_ACCOUNTS = 20
MAX_LOG_LINES = 5000
MAX_WARNINGS = 10
SESSION_MAX_AGE = 30 * 24 * 3600

_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
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
    max_jobs_per_hour: int = 0  # per client address; 0 = unlimited
    trust_proxy: bool = False  # take the client address from X-Forwarded-For
    python: str = sys.executable
    script: Path = CLI_SCRIPT

    @property
    def public(self) -> bool:
        return bool(self.public_url)

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
    payload: object, env: Mapping[str, str], *, require_tokens: bool = False
) -> JobRequest:
    """Validate a form submission.

    ``env`` is what the CLI child will see (see :func:`effective_env`): an
    account may leave its token blank when ``env`` already holds one — unless
    ``require_tokens`` (public mode), where every account brings its own.
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
    cmd += ["--output", str(out_dir), "--pdf", "--timezone", request.timezone]
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


def read_summary(out_dir: Path) -> dict[str, str]:
    """The headline metrics of a finished run (empty when unavailable)."""
    try:
        with (out_dir / "contribution_summary.csv").open(encoding="utf-8-sig", newline="") as fh:
            rows = {row["metric"]: row["value"] for row in csv.DictReader(fh)}
    except (OSError, KeyError, csv.Error):
        return {}
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

    @property
    def logins(self) -> list[str]:
        return [a["login"] for a in self.accounts]

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def files(self) -> dict[str, str]:
        return {
            kind: f"/api/jobs/{self.id}/files/{kind}"
            for kind, name in REPORT_FILES.items()
            if (self.out_dir / name).is_file()
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
        }

    def to_json(self) -> dict:
        data = self.record()
        del data["owner"]
        return {
            **data,
            "logins": self.logins,
            "phase": self.phase,
            "progress": self.progress,
            "message": self.message,
            "files": self.files(),
            "log_count": self.log_dropped + len(self.log),
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
        if job.active:  # the server stopped mid-run
            job.status = "failed"
            job.error = job.error or "The server stopped before this report finished."
            job.finished_at = job.finished_at or job.created_at
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

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.jobs_dir = Path(settings.jobs_dir).resolve()
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(max(1, settings.parallel))
        self._tasks: set[asyncio.Task] = set()
        self._recent: dict[str, collections.deque[float]] = {}

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
            window = self._recent.setdefault(client, collections.deque())
            cutoff = time.monotonic() - 3600
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) >= self.settings.max_jobs_per_hour:
                return "Too many reports from this address in the last hour. Try again later.", 429
        return None

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
            self._recent.setdefault(client, collections.deque()).append(time.monotonic())
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
        elif job.process is not None:
            _kill_tree(job.process)

    def delete(self, job: Job) -> None:
        """Remove a report and its files (an active run is cancelled first)."""
        if job.active:
            job.delete_requested = True
            self.cancel(job)
            if job.status == "cancelled" and job.process is None:
                self._remove(job)  # it never started
            return
        self._remove(job)

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
            self._remove(job)
        if expired:
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
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)

    # -- running ------------------------------------------------------------

    async def _run(self, job: Job) -> None:
        try:
            async with self._slots:
                if not job.cancel_requested:
                    await self._execute(job)
        except Exception as exc:  # noqa: BLE001 - one broken run must not take the server down
            log.exception("report %s crashed", job.id)
            job.status, job.error = "failed", job.redact(str(exc) or exc.__class__.__name__)
        finally:
            if job.active:
                job.status = "cancelled" if job.cancel_requested else "failed"
            self._scrub_run_log(job)
            job.request = job.process = job.progress = None  # drop the tokens
            job.redactions = self._server_paths(job.out_dir)
            job.finished_at = _now()
            if job.delete_requested:
                self._remove(job)
            else:
                self._save(job)

    async def _execute(self, job: Job) -> None:
        request = job.request
        assert request is not None
        settings = self.settings
        job.status, job.phase = "running", "Starting"
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
            if "pdf" not in job.files():
                job.warnings.append(
                    "The PDF could not be rendered (it needs Microsoft Edge or Google "
                    "Chrome). Open the HTML report and print it to PDF instead."
                )
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
#: The session's owner id (sha256 of the session cookie) on each request.
OWNER = web.RequestKey("owner", str) if hasattr(web, "RequestKey") else "owner"

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

        session = request.cookies.get(settings.session_cookie, "")
        fresh = not _SESSION_RE.match(session)
        if fresh:
            session = secrets.token_urlsafe(32)
        request[OWNER] = hashlib.sha256(session.encode("ascii")).hexdigest()
        response = await handler(request)
        if fresh:
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
        headers.setdefault(
            "Cache-Control", "no-store" if request.path.startswith("/api/") else "no-cache"
        )
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
        local: dict[str, object] = {
            "env_logins": [], "has_default_token": False, "defaults": {}, "output_dir": "",
        }
    else:
        env = effective_env()
        local = {
            "env_logins": env_token_logins(env),
            "has_default_token": bool((env.get("GITHUB_TOKEN") or "").strip()),
            "defaults": {name: env.get(var, "") for name, var in FORM_ENV_VARS.items()},
            "output_dir": _display_path(request.app[MANAGER].jobs_dir),
        }
    return web.json_response(
        {
            "version": __version__,
            "mode": "public" if settings.public else "local",
            "retention_hours": settings.retention_hours,
            "max_active_per_user": settings.max_active_per_user,
            "pdf_browser": find_browser() is not None,
            **local,
        }
    )


async def _list_jobs(request: web.Request) -> web.Response:
    jobs = request.app[MANAGER].listing(request[OWNER])
    return web.json_response({"jobs": [job.to_json() for job in jobs]})


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
    return web.json_response(job.to_json(), status=201)


async def _cancel_job(request: web.Request) -> web.Response:
    job = _job(request)
    request.app[MANAGER].cancel(job)
    return web.json_response(job.to_json())


async def _delete_job(request: web.Request) -> web.Response:
    job = _job(request)
    request.app[MANAGER].delete(job)
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


def create_app(settings: Settings | None = None, **legacy) -> web.Application:
    """The aiohttp application. ``legacy`` keyword arguments build local
    :class:`Settings` (``jobs_dir``, ``python``, ``script``)."""
    if settings is None:
        jobs_dir = legacy.pop("jobs_dir", DEFAULT_JOBS_DIR)
        legacy.pop("loopback_only", None)
        settings = Settings(jobs_dir=Path(jobs_dir), **legacy)
    manager = JobManager(settings)
    manager.load_history()
    manager.expire()
    app = web.Application(middlewares=[_guard(settings)], client_max_size=256 * 1024)
    app[MANAGER] = manager
    app[SETTINGS] = settings
    app.router.add_get("/", _index)
    app.router.add_static("/static/", WEB_DIR)
    app.router.add_get("/api/config", _config)
    app.router.add_get("/api/jobs", _list_jobs)
    app.router.add_post("/api/jobs", _create_job)
    app.router.add_post("/api/jobs/{job_id}/cancel", _cancel_job)
    app.router.add_delete("/api/jobs/{job_id}", _delete_job)
    app.router.add_get("/api/jobs/{job_id}/log", _job_log)
    app.router.add_get("/api/jobs/{job_id}/files/{kind}", _job_file)
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
    print(f"Contribution report UI ({mode} mode) running at {url}  (Ctrl+C to stop)", flush=True)
    web.run_app(app, host=host, port=port, access_log=None, print=None, ssl_context=ssl_context)
