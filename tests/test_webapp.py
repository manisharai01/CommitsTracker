"""Web UI tests - no network access or GitHub tokens required.

A stand-in for github_report.py drives the real job pipeline (subprocess,
output streaming, downloads, history) end to end, in local and public mode.

Run directly:

    python tests/test_webapp.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from github_contrib.webapp import (  # noqa: E402
    CHILD_ENV_ALLOWLIST,
    MANAGER,
    Job,
    RequestError,
    Settings,
    build_command,
    build_env,
    create_app,
    env_token_logins,
    parse_job_request,
    public_settings,
    pump_lines,
    read_summary,
)

TOKEN = "ghp_" + "a1B2c3D4e5" * 4  # 44 chars, looks like a classic token
OTHER_TOKEN = "ghp_" + "Z9y8X7w6V5" * 4
PUBLIC_URL = "http://localhost:8765"  # public mode without TLS is allowed on loopback only

# Stand-in CLI: prints log lines and tqdm redraws, echoes the token it was given
# and its output path (to prove redaction), writes a run.log containing the
# token (to prove it is scrubbed), then the files a real run produces.
FAKE_CLI = textwrap.dedent(
    """
    import os, sys, time
    args = sys.argv[1:]
    users = [args[i + 1] for i, a in enumerate(args) if a == "--user"]
    out = args[args.index("--output") + 1]
    token = os.environ.get("GITHUB_TOKEN_" + users[0].upper().replace("-", "_"), "")
    def log(level, msg):
        print(f"2026-10-06 10:00:00 | {level:<7} | github_contrib.report | {msg}", flush=True)
    log("INFO", "github-contrib 1.0.0 starting for: " + ", ".join(users))
    if "broken" in users:
        log("ERROR", "Report generation failed: boom")
        sys.exit(1)
    if "slow" in users:
        time.sleep(30)
    log("WARNING", "[%s] classic token lacks 'read:org'" % users[0])
    print("token=" + token, flush=True)
    print("Output directory : " + os.path.abspath(out), flush=True)
    print("dotenv-disabled=" + os.environ.get("GITHUB_CONTRIB_NO_DOTENV", "0"), flush=True)
    print("leaked-secret=" + os.environ.get("SERVER_SECRET_FOR_TEST", "none"), flush=True)
    print("args=" + " ".join(args[2:]), flush=True)
    sys.stderr.write("Commits:  50%|#####     | 1/2 [00:01<00:01]\\rCommits: 100%|##########| 2/2 [00:02<00:00]\\n")
    sys.stderr.flush()
    with open(os.path.join(out, "run.log"), "w", encoding="utf-8") as fh:
        fh.write("2026-10-06 10:00:00 | DEBUG   | x | header Authorization: Bearer " + token + "\\n")
    with open(os.path.join(out, "contribution_summary.csv"), "w", encoding="utf-8-sig") as fh:
        fh.write("metric,value\\ntotal_lifetime_commits,2\\nactive_days,1\\nemails_seen," + os.environ["AUTHOR_EMAILS"] + "\\n")
    names = ["report.html", "github_contributions.xlsx"]
    if "nopdf" not in users:  # "nopdf": the web app must print the PDF itself
        names.append("report.pdf")
    for name in names:
        with open(os.path.join(out, name), "w") as fh:
            fh.write(name)
    log("INFO", "wrote report.md and report.html")
    """
)

# Stand-in for `python -m github_contrib.pdfexport <report.html>`.
FAKE_PDF = textwrap.dedent(
    """
    import pathlib, sys, time
    mode, html = sys.argv[1], pathlib.Path(sys.argv[2])
    def log(level, msg):
        print(f"2026-10-06 10:00:00 | {level:<7} | github_contrib.pdfexport | {msg}", flush=True)
    time.sleep(30 if mode == "slow" else 0.4)
    if mode == "fail":
        log("WARNING", "PDF export stopped: chromium took longer than 900 seconds (set PDF_TIMEOUT to allow more).")
        sys.exit(1)
    html.with_suffix(".pdf").write_text("pdf")
    log("INFO", "wrote report.pdf (via chromium, 1s)")
    """
)


def _payload(*logins: str, token: str = TOKEN, **options) -> dict:
    return {
        "accounts": [{"login": login, "token": token, "emails": ""} for login in logins],
        "options": options,
    }


def _raises(payload, env, fragment: str, **kwargs) -> None:
    try:
        parse_job_request(payload, env, **kwargs)
    except RequestError as exc:
        assert fragment in str(exc), f"{fragment!r} not in {exc!r}"
    else:
        raise AssertionError(f"expected RequestError containing {fragment!r}")


def test_parse_job_request():
    env = {"GITHUB_TOKEN_BOB_SMITH": "ghp_fromenv"}
    payload = {
        "accounts": [
            {"login": "@alice", "token": TOKEN, "emails": "a@work.com, a@home.com"},
            {"login": "bob-smith", "token": "", "emails": ["a@work.com", "b@work.com"]},
        ],
        "options": {"extra_repos": "acme/app acme/api", "commit_stats": False, "exclude_own_repos": True},
        "period": {"since": "2026-01-01", "until": "2026-03-31", "timezone": "Asia/Kolkata"},
    }
    req = parse_job_request(payload, env)
    assert req.logins == ["alice", "bob-smith"]
    assert req.accounts[0].token == TOKEN and req.accounts[1].token == ""
    assert req.author_emails == ["a@work.com", "a@home.com", "b@work.com"]
    assert req.extra_repos == ["acme/app", "acme/api"]
    assert req.commit_stats is False and req.exclude_own_repos is True and req.pull_requests is True
    assert (req.since, req.until, req.timezone) == ("2026-01-01", "2026-03-31", "Asia/Kolkata")
    options = json.dumps(req.options())
    assert "token" not in options and "2026-01-01" in options
    # An unknown browser zone falls back to its UTC offset.
    req2 = parse_job_request(
        {**_payload("carol"), "period": {"timezone": "Mars/Olympus", "timezone_offset": "+05:30"}}, {}
    )
    assert req2.timezone == "UTC+05:30"
    print("ok  test_parse_job_request")


def test_parse_job_request_rejects_bad_input():
    _raises({}, {}, "at least one")
    _raises(_payload("--pdf"), {}, "not a valid GitHub username")
    _raises(_payload("alice", "ALICE"), {}, "listed twice")
    _raises(_payload("alice", token=""), {}, "GITHUB_TOKEN_ALICE")
    _raises(_payload("alice", token="Bearer abc"), {}, "doesn't look like")
    _raises(_payload("alice", extra_repos="no-slash"), {}, "repository")
    _raises(_payload("alice", skip_forks="yes"), {}, "true or false")
    _raises({**_payload("alice"), "period": {"since": "2026-02-01", "until": "2026-01-01"}}, {}, "after it ends")
    _raises({**_payload("alice"), "period": {"since": "01/02/2026"}}, {}, "YYYY-MM-DD")
    _raises({**_payload("alice"), "period": {"timezone": "../../etc/passwd"}}, {}, "time zone")
    # Public mode never falls back to a server token.
    _raises(_payload("carol", token=""), {"GITHUB_TOKEN": "x"}, "Enter a personal access token", require_tokens=True)
    # A generic GITHUB_TOKEN covers any login locally.
    assert parse_job_request(_payload("carol", token=""), {"GITHUB_TOKEN": "x"}).logins == ["carol"]
    print("ok  test_parse_job_request_rejects_bad_input")


def test_build_command_and_env():
    req = parse_job_request(
        {
            "accounts": [{"login": "alice", "token": TOKEN, "emails": "a@x.com"}, {"login": "bob", "token": ""}],
            "options": {"default_branch_only": True, "pull_requests": False},
            "period": {"since": "2026-01-01", "timezone": "UTC"},
        },
        {"GITHUB_TOKEN_BOB": "ghp_env"},
    )
    cmd = build_command(req, Path("out"), "py", Path("cli.py"))
    assert cmd[:2] == ["py", "cli.py"]
    assert cmd.count("--user") == 2
    assert "--pdf" not in cmd, "the PDF is printed after the run, by the web app"
    assert "--default-branch-only" in cmd and "--no-prs" in cmd and "--no-commit-stats" not in cmd
    assert cmd[cmd.index("--since") + 1] == "2026-01-01" and "--until" not in cmd
    assert cmd[cmd.index("--timezone") + 1] == "UTC"
    assert TOKEN not in " ".join(cmd)

    base = {"AUTHOR_EMAILS": "stale@x.com", "PATH": "p", "DATABASE_PASSWORD": "s3cret", "GITHUB_TOKEN": "server"}
    env = build_env(req, base)
    assert env["GITHUB_TOKEN_ALICE"] == TOKEN
    assert "GITHUB_TOKEN_BOB" not in env  # left for the child to read from .env
    assert env["AUTHOR_EMAILS"] == "a@x.com"
    assert env["EXTRA_REPOS"] == "" and env["PATH"] == "p"

    # Public mode: only operating-system variables survive, and .env is off.
    isolated = build_env(req, base, isolated=True)
    assert "DATABASE_PASSWORD" not in isolated and "GITHUB_TOKEN" not in isolated
    assert isolated["PATH"] == "p" and isolated["GITHUB_CONTRIB_NO_DOTENV"] == "1"
    assert isolated["GITHUB_TOKEN_ALICE"] == TOKEN
    assert "PATH" in CHILD_ENV_ALLOWLIST and "GITHUB_TOKEN" not in CHILD_ENV_ALLOWLIST
    print("ok  test_build_command_and_env")


def test_job_output_parsing():
    job = Job(
        id="j", out_dir=Path("."), accounts=[{"login": "alice", "emails": []}], options={},
        redactions=[(TOKEN, "••••"), ("C:\\srv\\reports\\j", "<report>")],
    )
    job.add_output("2026-10-06 10:00:00 | INFO    | github_contrib.report | [alice] authenticated, discovering repositories…")
    assert job.phase == "Discovering repositories"
    job.add_output("Commits:  45%|████▌     | 9/20 [00:30<00:40,  2.70item/s]")
    assert job.phase == "Collecting commits"
    assert job.progress == {"label": "Commits", "n": 9, "total": 20}
    assert len(job.log) == 1  # progress redraws are not logged
    job.add_output("PR commits:  50%|#####     | 1/2 [00:01<00:01]")
    assert job.phase == "Reading pull-request commits"
    job.add_output("2026-10-06 10:00:01 | WARNING | github_contrib.report | token lacks 'read:org'")
    job.add_output("2026-10-06 10:00:01 | WARNING | github_contrib.report | token lacks 'read:org'")
    assert job.warnings == ["token lacks 'read:org'"]
    job.add_output(f"leaked {TOKEN} in C:\\srv\\reports\\j\\report.pdf")
    assert TOKEN not in job.log[-1] and "••••" in job.log[-1] and "<report>" in job.log[-1]
    job.add_output("Configuration error: Missing GitHub token(s) for: alice")
    assert job.error.startswith("Configuration error")
    lines, nxt = job.log_since(2)
    assert nxt == len(job.log) and lines == job.log[2:]
    print("ok  test_job_output_parsing")


def test_pump_lines_splits_progress_redraws():
    async def scenario() -> list[str]:
        reader = asyncio.StreamReader()
        data = "a\r\nb\rc\nd€".encode()
        reader.feed_data(data[:-2])  # split the multi-byte '€' across chunks
        reader.feed_data(data[-2:])
        reader.feed_eof()
        got: list[str] = []
        await pump_lines(reader, got.append)
        return got

    assert asyncio.run(scenario()) == ["a", "b", "c", "d€"]
    print("ok  test_pump_lines_splits_progress_redraws")


def test_read_summary_and_env_logins():
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        assert read_summary(out) == {}
        (out / "contribution_summary.csv").write_text(
            "metric,value\ntotal_lifetime_commits,74\nactive_days,34\ntracked_users,x\n", encoding="utf-8-sig"
        )
        assert read_summary(out) == {"total_lifetime_commits": "74", "active_days": "34"}
    env = {"GITHUB_TOKEN_NEHA": "t", "GITHUB_TOKEN_MY_ORG": "t", "GITHUB_TOKEN_EMPTY": " ", "GITHUB_TOKEN_1": "t"}
    assert env_token_logins(env) == ["my-org", "neha"]
    print("ok  test_read_summary_and_env_logins")


def test_public_settings_validation():
    settings = public_settings("https://reports.example.com/")
    assert settings.public and settings.secure and settings.public_origin == "https://reports.example.com"
    assert settings.session_cookie == "__Host-ct_session" and settings.retention_hours == 24
    for bad in ("http://reports.example.com", "https://x.com/sub", "ftp://x.com", "https://"):
        try:
            public_settings(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    assert public_settings(PUBLIC_URL).public  # loopback testing without TLS
    print("ok  test_public_settings_validation")


async def _wait_until_finished(client: TestClient, job_id: str, headers=None) -> dict:
    for _ in range(600):
        jobs = (await (await client.get("/api/jobs", headers=headers)).json())["jobs"]
        job = next(j for j in jobs if j["id"] == job_id)
        if job["status"] not in ("queued", "running"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


def _fake(tmp: Path) -> Path:
    fake = tmp / "fake_cli.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    return fake


def test_pdf_is_printed_after_the_report_is_done():
    from github_contrib import webapp

    async def job_state(client: TestClient, job_id: str) -> dict | None:
        jobs = (await (await client.get("/api/jobs")).json())["jobs"]
        return next((j for j in jobs if j["id"] == job_id), None)

    async def until(predicate, timeout: float = 20.0) -> None:
        for _ in range(int(timeout / 0.05)):
            if await predicate():
                return
            await asyncio.sleep(0.05)
        raise AssertionError("condition not met in time")

    async def scenario(tmp: Path) -> None:
        settings = Settings(jobs_dir=tmp / "runs", script=_fake(tmp))
        fake_pdf = tmp / "fake_pdf.py"
        fake_pdf.write_text(FAKE_PDF, encoding="utf-8")
        mode = {"value": "ok"}
        commands: list[list[str]] = []

        def command(_python, html_path):
            commands.append([mode["value"], str(html_path)])
            return [sys.executable, str(fake_pdf), mode["value"], str(html_path)]

        saved = webapp.pdf_command, webapp.find_browser
        webapp.pdf_command, webapp.find_browser = command, (lambda: "chromium")
        try:
            async with TestClient(TestServer(create_app(settings))) as client:
                async def start() -> str:
                    created = await client.post("/api/jobs", json=_payload("nopdf"))
                    assert created.status == 201, await created.text()
                    return (await created.json())["id"]

                # The report is done - HTML and Excel ready - while its PDF prints.
                job_id = await start()
                job = await _wait_until_finished(client, job_id)
                assert job["status"] == "done" and job["pdf"] == "rendering", job
                assert set(job["files"]) == {"html", "xlsx"}
                await until(lambda: _pdf_is(client, job_id, "ready"))
                job = await job_state(client, job_id)
                assert set(job["files"]) == {"pdf", "html", "xlsx"} and job["warnings"] == [
                    "[nopdf] classic token lacks 'read:org'"
                ]
                record = json.loads((settings.jobs_dir / job_id / "job.json").read_text(encoding="utf-8"))
                assert record["pdf"] == "ready" and commands[-1][1].endswith("report.html")
                assert (await client.get(job["files"]["pdf"])).status == 200

                # A PDF that fails leaves a complete report and says why.
                mode["value"] = "fail"
                job_id = await start()
                await until(lambda: _pdf_is(client, job_id, "failed"))
                job = await job_state(client, job_id)
                assert job["status"] == "done" and "pdf" not in job["files"] and "html" in job["files"]
                assert webapp.PDF_FAILED in job["warnings"]
                assert any("took longer than 900 seconds" in w for w in job["warnings"])

                # No browser at all: no PDF step is started.
                webapp.find_browser = lambda: None
                count = len(commands)
                job_id = await start()
                await until(lambda: _pdf_is(client, job_id, "failed"))
                assert webapp.PDF_NO_BROWSER in (await job_state(client, job_id))["warnings"]
                assert len(commands) == count
                webapp.find_browser = lambda: "chromium"

                # Deleting a report while its PDF prints stops the browser and
                # removes the report for good (nothing is written back).
                mode["value"] = "slow"
                job_id = await start()
                await until(lambda: _pdf_is(client, job_id, "rendering"))
                deleted = await client.delete(f"/api/jobs/{job_id}", json={})
                assert deleted.status == 200
                manager = client.server.app[MANAGER]
                await until(lambda: _gone(manager, settings.jobs_dir / job_id))
                await asyncio.sleep(0.3)
                assert not (settings.jobs_dir / job_id).exists()
                assert await job_state(client, job_id) is None
        finally:
            webapp.pdf_command, webapp.find_browser = saved

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))

    # A server that stopped mid-PDF: the report stays, the PDF is marked failed.
    with tempfile.TemporaryDirectory() as tmp:
        record = {"accounts": [{"login": "a"}], "created_at": "2026-10-06T10:00:00+00:00",
                  "status": "done", "pdf": "rendering", "warnings": []}
        job = Job.from_record(record, Path(tmp) / "x")
        from github_contrib.webapp import PDF_STOPPED
        assert job.status == "done" and job.pdf == "failed" and job.warnings == [PDF_STOPPED]
    print("ok  test_pdf_is_printed_after_the_report_is_done")


async def _pdf_is(client: TestClient, job_id: str, state: str) -> bool:
    jobs = (await (await client.get("/api/jobs")).json())["jobs"]
    job = next((j for j in jobs if j["id"] == job_id), None)
    return job is not None and job["status"] == "done" and job.get("pdf") == state


async def _gone(manager, folder: Path) -> bool:
    return folder.name not in manager.jobs and not folder.exists()


def test_web_end_to_end_local():
    async def scenario(tmp: Path) -> None:
        jobs_dir = tmp / "runs"
        settings = Settings(jobs_dir=jobs_dir, script=_fake(tmp))
        async with TestClient(TestServer(create_app(settings))) as client:
            page = await client.get("/")
            assert page.status == 200 and "default-src 'self'" in page.headers["Content-Security-Policy"]
            assert page.headers["Server"] == "CommitsTracker"
            for header in ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy"):
                assert header in page.headers, header
            assert (await client.get("/static/app.js")).status == 200
            config = await (await client.get("/api/config")).json()
            assert config["mode"] == "local" and {"env_logins", "pdf_browser"} <= set(config)
            assert "defaults" not in config  # .env emails / lists never reach the browser

            # Requests other sites could forge are refused.
            assert (await client.get("/api/jobs", headers={"Host": "evil.example"})).status == 403
            body = json.dumps(_payload("alice"))
            forged = await client.post("/api/jobs", data=body, headers={"Content-Type": "text/plain"})
            assert forged.status == 415
            cross = await client.post(
                "/api/jobs", data=body,
                headers={"Content-Type": "application/json", "Origin": "https://evil.example"},
            )
            assert cross.status == 403
            bad = await client.post("/api/jobs", json=_payload("-nope"))
            assert bad.status == 400 and "valid GitHub username" in (await bad.json())["error"]

            payload = _payload("alice")
            payload["accounts"][0]["emails"] = "alice@work.com"
            payload["period"] = {"since": "2026-01-01", "until": "2026-03-31", "timezone": "Asia/Kolkata"}
            created = await client.post("/api/jobs", json=payload)
            assert created.status == 201
            job = await _wait_until_finished(client, (await created.json())["id"])
            assert job["status"] == "done", job
            assert job["summary"]["total_lifetime_commits"] == "2"
            assert job["warnings"] == ["[alice] classic token lacks 'read:org'"]
            assert set(job["files"]) == {"pdf", "html", "xlsx"}
            assert job["options"]["since"] == "2026-01-01" and "owner" not in job

            pdf = await client.get(job["files"]["pdf"])
            assert pdf.status == 200 and await pdf.text() == "report.pdf"
            assert pdf.headers["Content-Disposition"].startswith('attachment; filename="github-report-alice-')
            html = await client.get(job["files"]["html"])
            assert html.headers["Content-Disposition"].startswith("inline")
            assert html.headers["Content-Security-Policy"].startswith("sandbox allow-scripts")
            assert (await client.get(f"/api/jobs/{job['id']}/files/run.log")).status == 404

            log = await (await client.get(f"/api/jobs/{job['id']}/log?since=0")).json()
            text = "\n".join(log["lines"])
            assert "token=••••" in text and TOKEN not in text
            assert "--since 2026-01-01 --until 2026-03-31" in text and "--timezone Asia/Kolkata" in text
            # The form's emails replace .env's AUTHOR_EMAILS in the child.
            run_dir = jobs_dir / job["id"]
            assert "emails_seen,alice@work.com" in (run_dir / "contribution_summary.csv").read_text(encoding="utf-8-sig")
            # The token written to run.log by the child is scrubbed from disk.
            run_log = (run_dir / "run.log").read_text(encoding="utf-8")
            assert TOKEN not in run_log and "Bearer ••••" in run_log

            failed = await client.post("/api/jobs", json=_payload("broken"))
            failed_job = await _wait_until_finished(client, (await failed.json())["id"])
            assert failed_job["status"] == "failed" and "boom" in failed_job["error"]

            assert TOKEN not in (run_dir / "job.json").read_text(encoding="utf-8")

        # History survives a restart.
        async with TestClient(TestServer(create_app(settings))) as client:
            jobs = (await (await client.get("/api/jobs")).json())["jobs"]
            assert [j["status"] for j in jobs] == ["failed", "done"]
            assert jobs[1]["files"]["pdf"].endswith("/files/pdf")
            # Deleting removes the report and its folder.
            deleted = await client.delete(f"/api/jobs/{jobs[1]['id']}", json={})
            assert deleted.status == 200 and not (jobs_dir / jobs[1]["id"]).exists()
            assert len((await (await client.get("/api/jobs")).json())["jobs"]) == 1

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_web_end_to_end_local")


def test_web_public_mode_isolation_and_limits():
    async def scenario(tmp: Path) -> None:
        jobs_dir = tmp / "runs"
        settings = public_settings(
            PUBLIC_URL, jobs_dir=jobs_dir, max_active_per_user=1, max_jobs_per_hour=0
        )
        settings.script = _fake(tmp)
        origin = {"Origin": PUBLIC_URL}
        import os

        os.environ["SERVER_SECRET_FOR_TEST"] = "do-not-leak"
        try:
            app = create_app(settings)
            async with TestClient(TestServer(app)) as alice:
                config = await (await alice.get("/api/config")).json()
                assert config["mode"] == "public"
                assert config["env_logins"] == [] and config["output_dir"] == "" and "defaults" not in config

                # Cross-site requests: the Origin must be the public origin.
                assert (await alice.post("/api/jobs", json=_payload("alice"))).status == 403
                evil = {"Origin": "https://evil.example"}
                assert (await alice.post("/api/jobs", json=_payload("alice"), headers=evil)).status == 403
                # Tokens are required; .env is never consulted.
                blank = await alice.post("/api/jobs", json=_payload("alice", token=""), headers=origin)
                assert blank.status == 400 and "personal access token" in (await blank.json())["error"]

                created = await alice.post("/api/jobs", json=_payload("alice"), headers=origin)
                assert created.status == 201
                job = await _wait_until_finished(alice, (await created.json())["id"])
                assert job["status"] == "done", job

                log = "\n".join((await (await alice.get(f"/api/jobs/{job['id']}/log")).json())["lines"])
                assert "dotenv-disabled=1" in log and "leaked-secret=none" in log
                assert "<report>" in log and str(jobs_dir.resolve()) not in log

                # A second browser (no cookie) sees nothing and can touch nothing.
                async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as bob:
                    url = alice.make_url
                    assert (await (await bob.get(url("/api/jobs"))).json())["jobs"] == []
                    for path in (f"/api/jobs/{job['id']}/log", job["files"]["pdf"], job["files"]["html"]):
                        assert (await bob.get(url(path))).status == 404, path
                    delete = await bob.delete(url(f"/api/jobs/{job['id']}"), json={}, headers=origin)
                    assert delete.status == 404
                    cancel = await bob.post(url(f"/api/jobs/{job['id']}/cancel"), json={}, headers=origin)
                    assert cancel.status == 404

                # One active report per session: a second one is refused until it ends.
                slow = await alice.post("/api/jobs", json=_payload("slow"), headers=origin)
                assert slow.status == 201
                refused = await alice.post("/api/jobs", json=_payload("alice"), headers=origin)
                assert refused.status == 429 and "already have 1" in (await refused.json())["error"]
                slow_id = (await slow.json())["id"]
                assert (await alice.delete(f"/api/jobs/{slow_id}", json={}, headers=origin)).status == 200
                manager = app[MANAGER]
                for _ in range(200):
                    if slow_id not in manager.jobs:  # dropped once its process is killed
                        break
                    await asyncio.sleep(0.05)
                assert slow_id not in manager.jobs and not (jobs_dir / slow_id).exists()
        finally:
            os.environ.pop("SERVER_SECRET_FOR_TEST", None)

        # After a restart, reports stay private to their session.
        restarted = create_app(settings)
        async with TestClient(TestServer(restarted)) as stranger:
            assert (await (await stranger.get("/api/jobs")).json())["jobs"] == []

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_web_public_mode_isolation_and_limits")


def test_share_links_and_linkedin_summary():
    async def scenario(tmp: Path) -> None:
        jobs_dir = tmp / "runs"
        settings = public_settings(PUBLIC_URL, jobs_dir=jobs_dir)
        settings.script = _fake(tmp)
        origin = {"Origin": PUBLIC_URL}
        app = create_app(settings)
        async with TestClient(TestServer(app)) as owner:
            payload = _payload("alice")
            payload["accounts"][0]["emails"] = "alice@private.example"
            created = await owner.post("/api/jobs", json=payload, headers=origin)
            job = await _wait_until_finished(owner, (await created.json())["id"])
            assert job["status"] == "done" and job["share_url"] == ""

            # LinkedIn draft: the report's numbers, no repository names.
            post = await (await owner.get(f"/api/jobs/{job['id']}/linkedin")).json()
            assert "I shipped 2 commits" in post["text"] and post["limit"] == 3000

            shared = await (await owner.post(f"/api/jobs/{job['id']}/share", json={}, headers=origin)).json()
            url = shared["share_url"]
            assert url.startswith(f"{PUBLIC_URL}/shared/") and "share_token" not in shared
            path = url[len(PUBLIC_URL):]
            again = await (await owner.post(f"/api/jobs/{job['id']}/share", json={}, headers=origin)).json()
            assert again["share_url"] == url, "sharing twice keeps the same link"

            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as stranger:
                at = owner.make_url
                page = await stranger.get(at(path))
                body = await page.text()
                assert page.status == 200 and "alice" in body and "<b>2</b> commits" in body
                assert "alice@private.example" not in body and job["id"] not in body
                assert page.headers["X-Robots-Tag"].startswith("noindex")
                assert page.headers["Cache-Control"] == "no-store"
                assert "script-src" not in page.headers["Content-Security-Policy"]  # no scripts at all
                assert "Set-Cookie" not in page.headers, "link viewers get no session"
                pdf = await stranger.get(at(f"{path}/files/pdf"))
                assert pdf.status == 200 and pdf.headers["Content-Disposition"].startswith("inline")
                html = await stranger.get(at(f"{path}/files/html"))
                assert html.headers["Content-Security-Policy"].startswith("sandbox")
                for hidden in (f"{path}/files/xlsx", f"{path}/files/md", f"/api/jobs/{job['id']}/log"):
                    assert (await stranger.get(at(hidden))).status == 404, hidden
                # Only the owner can change sharing.
                for method in ("post", "delete"):
                    response = await getattr(stranger, method)(
                        at(f"/api/jobs/{job['id']}/share"), json={}, headers=origin
                    )
                    assert response.status == 404
                assert (await stranger.get(at("/shared/not-a-real-token-at-all-xxxxxx"))).status == 404

                # Stop sharing: the link dies at once; a new link is a new secret.
                off = await (await owner.delete(f"/api/jobs/{job['id']}/share", json={}, headers=origin)).json()
                assert off["share_url"] == ""
                assert (await stranger.get(at(path))).status == 404
                fresh = await (await owner.post(f"/api/jobs/{job['id']}/share", json={}, headers=origin)).json()
                assert fresh["share_url"] != url
                path = fresh["share_url"][len(PUBLIC_URL):]

        # Links survive a restart, and die with the report.
        async with TestClient(TestServer(create_app(settings))) as visitor:
            assert (await visitor.get(path)).status == 200
            restored = create_app(settings)
            manager = restored[MANAGER]
            manager.delete(manager.jobs[job["id"]])
            async with TestClient(TestServer(restored)) as late:
                assert (await late.get(path)).status == 404

        # Local mode has no public address, so it offers no links.
        local = Settings(jobs_dir=tmp / "local", script=_fake(tmp))
        async with TestClient(TestServer(create_app(local))) as client:
            done = await _wait_until_finished(
                client, (await (await client.post("/api/jobs", json=_payload("alice"))).json())["id"]
            )
            refused = await client.post(f"/api/jobs/{done['id']}/share", json={})
            assert refused.status == 400 and "public" in (await refused.json())["error"]
            assert (await client.get(f"/api/jobs/{done['id']}/linkedin")).status == 200

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_share_links_and_linkedin_summary")


def test_job_timeout_and_retention():
    async def scenario(tmp: Path) -> None:
        settings = Settings(jobs_dir=tmp / "runs", script=_fake(tmp), job_timeout=1.0, retention_hours=1)
        app = create_app(settings)
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/jobs", json=_payload("slow"))
            job = await _wait_until_finished(client, (await created.json())["id"])
            assert job["status"] == "failed" and "longer than" in job["error"], job

            manager = app[MANAGER]
            assert manager.expire() == 0  # not old enough yet
            later = datetime.now(timezone.utc) + timedelta(hours=2)
            assert manager.expire(later) == 1
            assert manager.jobs == {} and not (tmp / "runs" / job["id"]).exists()

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_job_timeout_and_retention")


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for test in _all_tests():
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
            import traceback
            traceback.print_exc()
    total = len(_all_tests())
    print(f"\n{total - failures}/{total} tests passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
