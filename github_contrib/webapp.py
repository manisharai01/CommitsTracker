"""Local web UI: enter accounts and tokens in a browser, download the PDF.

``python webui.py`` serves a single page (``github_contrib/web/``) where you
enter one or more GitHub logins with their tokens — the same values ``.env``
holds — pick the report options and download the finished report.

Each submitted form becomes a *job* that runs ``github_report.py --pdf`` in a
subprocess:

* tokens reach the child only through its environment (never argv, never
  disk) and are dropped from memory as soon as the run ends;
* jobs run one at a time so parallel runs don't compete for rate limits;
* each run writes to ``output-web/<job id>/`` next to a token-free
  ``job.json``, so the report history survives a server restart.
"""

from __future__ import annotations

import asyncio
import codecs
import csv
import json
import os
import re
import secrets
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlsplit

from aiohttp import web

from . import __version__
from .config import _sanitize_login_for_env, token_env_candidates
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
)

#: Environment variables the form replaces, keyed by form field name.
FORM_ENV_VARS: dict[str, str] = {
    "author_emails": "AUTHOR_EMAILS",
    "extra_repos": "EXTRA_REPOS",
    "extra_orgs": "EXTRA_ORGS",
    "exclude_owners": "EXCLUDE_OWNERS",
}

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
ACTIVE_STATUSES = frozenset({"queued", "running"})
MAX_ACCOUNTS = 20
MAX_LOG_LINES = 5000
MAX_WARNINGS = 10

_LOGIN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$")
_EMAIL_RE = re.compile(r"^[^@\s,]+@[^@\s,]+$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_]{20,255}$")
_NEWLINES = re.compile(r"\r\n|\r|\n")
# tqdm progress line: "Commits:  45%|████▌     | 90/200 [00:30<00:40,  2.70repo/s]"
_PROGRESS_RE = re.compile(r"^(?P<label>[^:|]+):\s*\d+%\|.*?\|\s*(?P<n>\d+)/(?P<total>\d+)")
# Package log line: "2026-10-03 12:13:33 | INFO    | github_contrib.report | message"
_LOG_LINE_RE = re.compile(
    r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d \| (?P<level>[A-Z]+)\s*\| \S+ \| (?P<msg>.*)$"
)

#: Progress-bar labels (see report._gather_with_progress) -> phase shown in the UI.
_PROGRESS_PHASES: dict[str, str] = {
    "Commits": "Collecting commits",
    "Line stats": "Fetching line stats",
    "Pull requests": "Collecting pull requests",
}
#: Log message fragments that start a new phase, in pipeline order.
_LOG_PHASES: tuple[tuple[str, str], ...] = (
    ("starting for", "Authenticating"),
    ("discovering repositories", "Discovering repositories"),
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

    @property
    def logins(self) -> list[str]:
        return [a.login for a in self.accounts]

    @property
    def author_emails(self) -> list[str]:
        return list(dict.fromkeys(e for a in self.accounts for e in a.emails))

    def options(self) -> dict:
        """The token-free form options, as stored in job.json."""
        return {name: getattr(self, name) for name in (*_LIST_OPTIONS, *_FLAG_DEFAULTS)}


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


def parse_job_request(payload: object, env: Mapping[str, str]) -> JobRequest:
    """Validate a form submission.

    ``env`` is what the CLI child will see (see :func:`effective_env`): an
    account may leave its token blank when ``env`` already holds one.
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
            raise RequestError(f"'{login or '(blank)'}' is not a valid GitHub username.")
        if login.lower() in seen:
            raise RequestError(f"{login} is listed twice.")
        seen.add(login.lower())
        token = str(raw.get("token") or "").strip()
        if token and not _TOKEN_RE.match(token):
            raise RequestError(f"The token for {login} doesn't look like a GitHub token.")
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
    return JobRequest(accounts=accounts, **lists, **flags)


def build_command(
    request: JobRequest, out_dir: Path, python: str = sys.executable, script: Path = CLI_SCRIPT
) -> list[str]:
    """The ``github_report.py`` invocation for ``request`` (no secrets in it)."""
    cmd = [python, str(script)]
    for login in request.logins:
        cmd += ["--user", login]
    cmd += ["--output", str(out_dir), "--pdf"]
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


def build_env(request: JobRequest, base: Mapping[str, str]) -> dict[str, str]:
    """The child's environment: ``base`` plus this run's tokens and form lists."""
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
        return {
            **self.record(),
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
        return job

    def add_output(self, line: str) -> None:
        """Record one line of the child's output and update the live status."""
        line = self._redact(line).rstrip()
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

    def _redact(self, line: str) -> str:
        if self.request is not None:
            for account in self.request.accounts:
                if account.token:
                    line = line.replace(account.token, "••••")
        return line

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
        self.log = lines[-MAX_LOG_LINES:]
        self.log_dropped = 0


class JobManager:
    """Owns every job: queues them, runs them one at a time, keeps history."""

    def __init__(
        self, jobs_dir: Path, *, python: str = sys.executable, script: Path = CLI_SCRIPT
    ) -> None:
        self.jobs_dir = Path(jobs_dir).resolve()
        self.python = python
        self.script = script
        self.jobs: dict[str, Job] = {}
        self._turn = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()

    def load_history(self) -> None:
        for path in self.jobs_dir.glob("*/job.json"):
            try:
                job = Job.from_record(json.loads(path.read_text(encoding="utf-8")), path.parent)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log.warning("skipping unreadable %s: %s", path, exc)
                continue
            self.jobs[job.id] = job

    def listing(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda job: job.created_at, reverse=True)

    def submit(self, request: JobRequest) -> Job:
        job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{'+'.join(request.logins[:3])}-{secrets.token_hex(3)}"
        job = Job(
            id=job_id,
            out_dir=self.jobs_dir / job_id,
            accounts=[{"login": a.login, "emails": a.emails} for a in request.accounts],
            options=request.options(),
            request=request,
        )
        self.jobs[job.id] = job
        task = asyncio.create_task(self._run(job))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    def cancel(self, job: Job) -> None:
        if not job.active:
            return
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.request = None  # _run skips it once it gets its turn
        elif job.process is not None and job.process.returncode is None:
            try:
                job.process.terminate()
            except ProcessLookupError:
                pass

    async def shutdown(self, _app: web.Application | None = None) -> None:
        for job in self.jobs.values():
            self.cancel(job)
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)

    async def _run(self, job: Job) -> None:
        try:
            async with self._turn:
                if not job.cancel_requested:
                    await self._execute(job)
        except Exception as exc:  # noqa: BLE001 - one broken run must not take the server down
            log.exception("report %s crashed", job.id)
            job.status, job.error = "failed", str(exc) or exc.__class__.__name__
        finally:
            if job.active:
                job.status = "cancelled" if job.cancel_requested else "failed"
            job.request = job.process = job.progress = None  # drop the tokens
            job.finished_at = _now()
            self._save(job)

    async def _execute(self, job: Job) -> None:
        request = job.request
        assert request is not None
        job.status, job.phase = "running", "Starting"
        job.out_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *build_command(request, job.out_dir, self.python, self.script),
            cwd=PROJECT_ROOT,
            env=build_env(request, os.environ),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        job.process = process
        if job.cancel_requested:  # cancelled while the process was starting
            process.terminate()
        assert process.stdout is not None
        await pump_lines(process.stdout, job.add_output)
        code = await process.wait()

        if job.cancel_requested:
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

_INDEX_CSP = "; ".join(
    (
        "default-src 'self'",
        "img-src 'self' data: https://github.com https://avatars.githubusercontent.com",
        "style-src 'self' 'unsafe-inline'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)


def _guard(loopback_only: bool):
    """Middleware keeping other websites away from this server and its tokens."""

    @web.middleware
    async def guard(request: web.Request, handler):
        if loopback_only and urlsplit(f"//{request.host}").hostname not in LOOPBACK_HOSTS:
            # A foreign domain resolving to 127.0.0.1 (DNS rebinding).
            raise web.HTTPForbidden(text="Unexpected Host header.")
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("Origin")
            if origin is not None and urlsplit(origin).netloc != request.host:
                raise web.HTTPForbidden(text="Cross-site request refused.")
            if request.content_type != "application/json":
                raise web.HTTPUnsupportedMediaType(text="Send JSON.")
        response = await handler(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Cache-Control", "no-store" if request.path.startswith("/api/") else "no-cache"
        )
        return response

    return guard


def _error(message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _job(request: web.Request) -> Job:
    job = request.app[MANAGER].jobs.get(request.match_info["job_id"])
    if job is None:
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
    env = effective_env()
    return web.json_response(
        {
            "version": __version__,
            "env_logins": env_token_logins(env),
            "has_default_token": bool((env.get("GITHUB_TOKEN") or "").strip()),
            "defaults": {name: env.get(var, "") for name, var in FORM_ENV_VARS.items()},
            "pdf_browser": find_browser() is not None,
            "output_dir": _display_path(request.app[MANAGER].jobs_dir),
        }
    )


async def _list_jobs(request: web.Request) -> web.Response:
    return web.json_response({"jobs": [job.to_json() for job in request.app[MANAGER].listing()]})


async def _create_job(request: web.Request) -> web.Response:
    try:
        payload = await request.json()
    except ValueError:
        return _error("The request was not valid JSON.")
    try:
        job_request = parse_job_request(payload, effective_env())
    except RequestError as exc:
        return _error(str(exc))
    job = request.app[MANAGER].submit(job_request)
    log.info("queued report %s", job.id)
    return web.json_response(job.to_json(), status=201)


async def _cancel_job(request: web.Request) -> web.Response:
    job = _job(request)
    request.app[MANAGER].cancel(job)
    return web.json_response(job.to_json())


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
    return response


def create_app(
    jobs_dir: Path = DEFAULT_JOBS_DIR,
    *,
    loopback_only: bool = True,
    python: str = sys.executable,
    script: Path = CLI_SCRIPT,
) -> web.Application:
    manager = JobManager(jobs_dir, python=python, script=script)
    manager.load_history()
    app = web.Application(middlewares=[_guard(loopback_only)], client_max_size=256 * 1024)
    app[MANAGER] = manager
    app.router.add_get("/", _index)
    app.router.add_static("/static/", WEB_DIR)
    app.router.add_get("/api/config", _config)
    app.router.add_get("/api/jobs", _list_jobs)
    app.router.add_post("/api/jobs", _create_job)
    app.router.add_post("/api/jobs/{job_id}/cancel", _cancel_job)
    app.router.add_get("/api/jobs/{job_id}/log", _job_log)
    app.router.add_get("/api/jobs/{job_id}/files/{kind}", _job_file)
    app.on_shutdown.append(manager.shutdown)
    return app


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    jobs_dir: Path = DEFAULT_JOBS_DIR,
    open_browser: bool = True,
) -> None:
    """Run the web UI until interrupted."""
    loopback = host in LOOPBACK_HOSTS
    app = create_app(jobs_dir, loopback_only=loopback)
    shown = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    url = f"http://{f'[{shown}]' if ':' in shown else shown}:{port}/"
    if not loopback:
        log.warning(
            "Listening on %s: tokens typed into the page cross the network unencrypted. "
            "Prefer the default 127.0.0.1.", host
        )
    if open_browser:

        async def _open_browser(_app: web.Application) -> None:
            asyncio.get_running_loop().call_later(0.5, webbrowser.open, url)

        app.on_startup.append(_open_browser)
    print(f"Contribution report UI running at {url}  (Ctrl+C to stop)", flush=True)
    web.run_app(app, host=host, port=port, access_log=None, print=None)
