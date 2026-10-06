"""Web UI tests - no network access or GitHub tokens required.

A stand-in for github_report.py drives the real job pipeline (subprocess,
output streaming, downloads, history) end to end.

Run directly:

    python tests/test_webapp.py

or with pytest:

    pytest -q
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import textwrap
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from github_contrib.webapp import (  # noqa: E402
    Job,
    RequestError,
    build_command,
    build_env,
    create_app,
    env_token_logins,
    parse_job_request,
    pump_lines,
    read_summary,
)

TOKEN = "ghp_" + "a1B2c3D4e5" * 4  # 44 chars, looks like a classic token

# Stand-in CLI: prints log lines and tqdm redraws, echoes the token it was given
# (to prove redaction), then writes the files a real run produces.
FAKE_CLI = textwrap.dedent(
    """
    import os, sys
    args = sys.argv[1:]
    users = [args[i + 1] for i, a in enumerate(args) if a == "--user"]
    out = args[args.index("--output") + 1]
    def log(level, msg):
        print(f"2026-10-06 10:00:00 | {level:<7} | github_contrib.report | {msg}", flush=True)
    log("INFO", "github-contrib 1.0.0 starting for: " + ", ".join(users))
    if "broken" in users:
        log("ERROR", "Report generation failed: boom")
        sys.exit(1)
    log("WARNING", "[%s] classic token lacks 'read:org'" % users[0])
    print("token=" + os.environ.get("GITHUB_TOKEN_" + users[0].upper().replace("-", "_"), ""), flush=True)
    sys.stderr.write("Commits:  50%|#####     | 1/2 [00:01<00:01]\\rCommits: 100%|##########| 2/2 [00:02<00:00]\\n")
    sys.stderr.flush()
    log("INFO", "collected 2 commit(s)")
    with open(os.path.join(out, "contribution_summary.csv"), "w", encoding="utf-8-sig") as fh:
        fh.write("metric,value\\ntotal_lifetime_commits,2\\nactive_days,1\\nemails_seen," + os.environ["AUTHOR_EMAILS"] + "\\n")
    for name in ("report.pdf", "report.html", "github_contributions.xlsx"):
        with open(os.path.join(out, name), "w") as fh:
            fh.write(name)
    log("INFO", "wrote report.md and report.html")
    """
)


def _payload(*logins: str, token: str = TOKEN, **options) -> dict:
    return {
        "accounts": [{"login": login, "token": token, "emails": ""} for login in logins],
        "options": options,
    }


def _raises(payload, env, fragment: str) -> None:
    try:
        parse_job_request(payload, env)
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
    }
    req = parse_job_request(payload, env)
    assert req.logins == ["alice", "bob-smith"]
    assert req.accounts[0].token == TOKEN and req.accounts[1].token == ""
    assert req.author_emails == ["a@work.com", "a@home.com", "b@work.com"]
    assert req.extra_repos == ["acme/app", "acme/api"]
    assert req.commit_stats is False and req.exclude_own_repos is True and req.pull_requests is True
    assert "token" not in json.dumps(req.options())
    print("ok  test_parse_job_request")


def test_parse_job_request_rejects_bad_input():
    _raises({}, {}, "at least one")
    _raises(_payload("--pdf"), {}, "not a valid GitHub username")
    _raises(_payload("alice", "ALICE"), {}, "listed twice")
    _raises(_payload("alice", token=""), {}, "GITHUB_TOKEN_ALICE")
    _raises(_payload("alice", token="Bearer abc"), {}, "doesn't look like")
    _raises(_payload("alice", extra_repos="no-slash"), {}, "repository")
    _raises(_payload("alice", skip_forks="yes"), {}, "true or false")
    # A generic GITHUB_TOKEN covers any login.
    assert parse_job_request(_payload("carol", token=""), {"GITHUB_TOKEN": "x"}).logins == ["carol"]
    print("ok  test_parse_job_request_rejects_bad_input")


def test_build_command_and_env():
    req = parse_job_request(
        {
            "accounts": [{"login": "alice", "token": TOKEN, "emails": "a@x.com"}, {"login": "bob", "token": ""}],
            "options": {"default_branch_only": True, "pull_requests": False},
        },
        {"GITHUB_TOKEN_BOB": "ghp_env"},
    )
    cmd = build_command(req, Path("out"), "py", Path("cli.py"))
    assert cmd[:2] == ["py", "cli.py"]
    assert cmd.count("--user") == 2 and "--pdf" in cmd
    assert "--default-branch-only" in cmd and "--no-prs" in cmd and "--no-commit-stats" not in cmd
    assert TOKEN not in " ".join(cmd)

    env = build_env(req, {"AUTHOR_EMAILS": "stale@x.com", "PATH": "p"})
    assert env["GITHUB_TOKEN_ALICE"] == TOKEN
    assert "GITHUB_TOKEN_BOB" not in env  # left for the child to read from .env
    assert env["AUTHOR_EMAILS"] == "a@x.com"
    assert env["EXTRA_REPOS"] == "" and env["PATH"] == "p"
    print("ok  test_build_command_and_env")


def test_job_output_parsing():
    req = parse_job_request(_payload("alice"), {})
    job = Job(id="j", out_dir=Path("."), accounts=[{"login": "alice", "emails": []}], options={}, request=req)
    job.add_output("2026-10-06 10:00:00 | INFO    | github_contrib.report | [alice] authenticated, discovering repositories…")
    assert job.phase == "Discovering repositories"
    job.add_output("Commits:  45%|████▌     | 9/20 [00:30<00:40,  2.70repo/s]")
    assert job.phase == "Collecting commits"
    assert job.progress == {"label": "Commits", "n": 9, "total": 20}
    assert len(job.log) == 1  # progress redraws are not logged
    job.add_output("2026-10-06 10:00:01 | WARNING | github_contrib.report | token lacks 'read:org'")
    job.add_output("2026-10-06 10:00:01 | WARNING | github_contrib.report | token lacks 'read:org'")
    assert job.warnings == ["token lacks 'read:org'"]
    job.add_output(f"leaked {TOKEN}")
    assert TOKEN not in job.log[-1] and "••••" in job.log[-1]
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


async def _wait_until_finished(client: TestClient, job_id: str) -> dict:
    for _ in range(300):
        jobs = (await (await client.get("/api/jobs")).json())["jobs"]
        job = next(j for j in jobs if j["id"] == job_id)
        if job["status"] not in ("queued", "running"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


def test_web_end_to_end():
    async def scenario(tmp: Path) -> None:
        fake = tmp / "fake_cli.py"
        fake.write_text(FAKE_CLI, encoding="utf-8")
        jobs_dir = tmp / "runs"
        async with TestClient(TestServer(create_app(jobs_dir, script=fake))) as client:
            page = await client.get("/")
            assert page.status == 200 and "default-src 'self'" in page.headers["Content-Security-Policy"]
            assert (await client.get("/static/app.js")).status == 200
            config = await (await client.get("/api/config")).json()
            assert {"env_logins", "defaults", "pdf_browser"} <= set(config)

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
            created = await client.post("/api/jobs", json=payload)
            assert created.status == 201
            job = await _wait_until_finished(client, (await created.json())["id"])
            assert job["status"] == "done", job
            assert job["summary"]["total_lifetime_commits"] == "2"
            assert job["warnings"] == ["[alice] classic token lacks 'read:org'"]
            assert set(job["files"]) == {"pdf", "html", "xlsx"}

            pdf = await client.get(job["files"]["pdf"])
            assert pdf.status == 200 and await pdf.text() == "report.pdf"
            assert pdf.headers["Content-Disposition"].startswith('attachment; filename="github-report-alice-')
            html = await client.get(job["files"]["html"])
            assert html.headers["Content-Disposition"].startswith("inline")
            assert (await client.get(f"/api/jobs/{job['id']}/files/run.log")).status == 404

            log = await (await client.get(f"/api/jobs/{job['id']}/log?since=0")).json()
            text = "\n".join(log["lines"])
            assert "token=••••" in text and TOKEN not in text
            # The form's emails replace .env's AUTHOR_EMAILS in the child.
            summary_csv = (jobs_dir / job["id"] / "contribution_summary.csv").read_text(encoding="utf-8-sig")
            assert "emails_seen,alice@work.com" in summary_csv

            failed = await client.post("/api/jobs", json=_payload("broken"))
            failed_job = await _wait_until_finished(client, (await failed.json())["id"])
            assert failed_job["status"] == "failed" and "boom" in failed_job["error"]

            saved = (jobs_dir / job["id"] / "job.json").read_text(encoding="utf-8")
            assert TOKEN not in saved

        # History survives a restart.
        async with TestClient(TestServer(create_app(jobs_dir, script=fake))) as client:
            jobs = (await (await client.get("/api/jobs")).json())["jobs"]
            assert [j["status"] for j in jobs] == ["failed", "done"]
            assert jobs[1]["files"]["pdf"].endswith("/files/pdf")

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_web_end_to_end")


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
