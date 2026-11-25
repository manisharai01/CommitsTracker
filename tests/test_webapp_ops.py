"""Operational limits of the web app: logs off the heap, token-check throttling,
memory-aware parallelism, the run deadline, expiry and the summary page.

Run directly:

    python tests/test_webapp_ops.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from github_contrib import webapp  # noqa: E402
from github_contrib.commitstore import STORE_PREFIX, STORE_SUFFIX  # noqa: E402
from github_contrib.webapp import (  # noqa: E402
    CONSOLE_LOG,
    MANAGER,
    TOKEN_CHECKS_PER_WINDOW,
    JobManager,
    Settings,
    TokenInfo,
    create_app,
    public_settings,
    runs_that_fit,
)
from tests.test_webapp import PUBLIC_URL, TOKEN, _fake, _payload, _wait_until_finished  # noqa: E402

# A run that reports its deadline and writes a full and a summary report.
DEADLINE_CLI = textwrap.dedent(
    """
    import os, sys
    args = sys.argv[1:]
    out = args[args.index("--output") + 1]
    print("deadline=" + os.environ.get("REPORT_DEADLINE", "none"), flush=True)
    for i in range(30):
        print(f"2026-10-06 10:00:00 | INFO    | github_contrib.report | line {i}", flush=True)
    with open(os.path.join(out, "contribution_summary.csv"), "w", encoding="utf-8-sig") as fh:
        fh.write("metric,value\\ntotal_lifetime_commits,2\\n")
    for name, text in (("report.html", "<html>full</html>"), ("summary.html", "<html>summary</html>"),
                       ("report.pdf", "%PDF-1.4 x %%EOF")):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    """
)


def _deadline_cli(tmp: Path) -> Path:
    path = tmp / "deadline_cli.py"
    path.write_text(DEADLINE_CLI, encoding="utf-8")
    return path


def test_finished_runs_keep_their_log_on_disk_not_in_memory():
    async def scenario(tmp: Path) -> None:
        app = create_app(Settings(jobs_dir=tmp / "runs", script=_deadline_cli(tmp)))
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/jobs", json=_payload("alice"))
            job = await _wait_until_finished(client, (await created.json())["id"])
            assert job["status"] == "done", job
            held = app[MANAGER].jobs[job["id"]]
            assert held.log == [], "the output left the server's memory"
            assert (tmp / "runs" / job["id"] / CONSOLE_LOG).is_file()
            lines = (await (await client.get(f"/api/jobs/{job['id']}/log")).json())
            assert any("line 29" in line for line in lines["lines"])
            assert lines["next"] == job["log_count"] == len(lines["lines"])
            more = (await (await client.get(f"/api/jobs/{job['id']}/log?since={lines['next']}")).json())
            assert more["lines"] == [] and more["next"] == lines["next"], "line numbers stay the same"

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_the_run_gets_a_deadline_before_the_hard_stop():
    async def scenario(tmp: Path) -> None:
        settings = Settings(jobs_dir=tmp / "runs", script=_deadline_cli(tmp), job_timeout=3600.0)
        app = create_app(settings)
        async with TestClient(TestServer(app)) as client:
            started = time.time()
            created = await client.post("/api/jobs", json=_payload("alice"))
            job = await _wait_until_finished(client, (await created.json())["id"])
            lines = (await (await client.get(f"/api/jobs/{job['id']}/log")).json())["lines"]
            value = float(next(line for line in lines if line.startswith("deadline="))[len("deadline="):])
            # Ten minutes are kept back to write the report from what was read.
            assert started + 3600 - 660 < value < started + 3600 - 540, value - started

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


BUILDING_CLI = textwrap.dedent(
    """
    import os, sys, time
    args = sys.argv[1:]
    out = args[args.index("--output") + 1]
    print("2026-10-06 10:00:00 | INFO    | github_contrib.report | Finished collection: 3 API requests", flush=True)
    time.sleep(float(os.environ.get("BUILD_TIME", "3")))
    with open(os.path.join(out, "contribution_summary.csv"), "w", encoding="utf-8-sig") as fh:
        fh.write("metric,value\\ntotal_lifetime_commits,2\\n")
    for name in ("report.html", "report.pdf"):
        with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
            fh.write("x")
    """
)


def test_a_report_being_written_may_finish_past_the_time_limit():
    async def scenario(tmp: Path, build_time: float, allowance: float) -> dict:
        script = tmp / "building_cli.py"
        script.write_text(BUILDING_CLI, encoding="utf-8")
        app = create_app(Settings(jobs_dir=tmp / "runs", script=script, job_timeout=1.0))
        original = webapp.BUILD_SECONDS
        webapp.BUILD_SECONDS = allowance
        os.environ["BUILD_TIME"] = str(build_time)
        try:
            async with TestClient(TestServer(app)) as client:
                created = await client.post("/api/jobs", json=_payload("alice"))
                return await _wait_until_finished(client, (await created.json())["id"])
        finally:
            webapp.BUILD_SECONDS = original
            os.environ.pop("BUILD_TIME", None)

    with tempfile.TemporaryDirectory() as tmp:
        job = asyncio.run(scenario(Path(tmp), build_time=2.5, allowance=30))
        assert job["status"] == "done", job
    with tempfile.TemporaryDirectory() as tmp:
        job = asyncio.run(scenario(Path(tmp), build_time=30, allowance=1.5))
        assert job["status"] == "failed" and "Writing the report took longer" in job["error"], job


def test_a_stopped_run_leaves_no_commit_store_behind():
    async def scenario(tmp: Path) -> None:
        app = create_app(Settings(jobs_dir=tmp / "runs", script=_fake(tmp), job_timeout=1.0))
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/jobs", json=_payload("slow"))
            job_id = (await created.json())["id"]
            folder = tmp / "runs" / job_id
            for _ in range(50):
                if folder.is_dir():
                    break
                await asyncio.sleep(0.05)
            (folder / f"{STORE_PREFIX}abc{STORE_SUFFIX}").write_bytes(b"x" * 1000)
            job = await _wait_until_finished(client, job_id)
            assert job["status"] == "failed", job
            assert not list(folder.glob(f"{STORE_PREFIX}*")), "the killed run's store was removed"

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_summary_page_is_served_for_save_as_pdf():
    async def scenario(tmp: Path) -> None:
        app = create_app(Settings(jobs_dir=tmp / "runs", script=_deadline_cli(tmp)))
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/jobs", json=_payload("alice"))
            job = await _wait_until_finished(client, (await created.json())["id"])
            assert set(job["files"]) >= {"html", "summary", "pdf"}
            response = await client.get(job["files"]["summary"])
            assert response.status == 200 and await response.text() == "<html>summary</html>"
            assert response.headers["Content-Disposition"].startswith("inline;")
            assert "-summary.html" in response.headers["Content-Disposition"]
            assert "sandbox" in response.headers["Content-Security-Policy"]

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_token_checks_are_throttled_in_public_mode():
    async def scenario(tmp: Path) -> None:
        settings = public_settings(PUBLIC_URL, jobs_dir=tmp / "runs")
        settings.script = _fake(tmp)
        app = create_app(settings)
        checked = []

        async def fake_info(token: str) -> TokenInfo:
            checked.append(token)
            return TokenInfo(200, "alice", "repo, read:org")

        original, webapp.fetch_token_info = webapp.fetch_token_info, fake_info
        try:
            async with TestClient(TestServer(app)) as client:
                origin = {"Origin": PUBLIC_URL}
                accounts = [{"login": f"user{i}", "token": TOKEN[:-2] + f"{i:02d}"} for i in range(20)]
                for _ in range(TOKEN_CHECKS_PER_WINDOW // 20):
                    ok = await client.post("/api/tokens/check", json={"accounts": accounts}, headers=origin)
                    assert ok.status == 200, await ok.text()
                refused = await client.post("/api/tokens/check", json={"accounts": accounts}, headers=origin)
                assert refused.status == 429
                assert len(checked) == TOKEN_CHECKS_PER_WINDOW, "nothing is sent to GitHub once refused"
        finally:
            webapp.fetch_token_info = original

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_parallel_runs_fit_the_servers_memory():
    original = webapp.memory_use
    try:
        webapp.memory_use = lambda: (100 * 2**20, 512 * 2**20)
        assert runs_that_fit(2) == 1, "two runs at once would take a 512 MB server down"
        webapp.memory_use = lambda: (100 * 2**20, 4096 * 2**20)
        assert runs_that_fit(2) == 2 and runs_that_fit(20) == 6
        webapp.memory_use = lambda: None
        assert runs_that_fit(3) == 3 and runs_that_fit(0) == 1
    finally:
        webapp.memory_use = original


def test_a_run_is_stopped_before_the_server_runs_out_of_memory():
    async def scenario(tmp: Path) -> None:
        settings = Settings(jobs_dir=tmp / "runs", script=_fake(tmp))
        app = create_app(settings)
        original, poll = webapp.memory_use, webapp.MEMORY_POLL_SECONDS
        # 500 of 512 MB in use: within the guard's headroom.
        webapp.memory_use = lambda: (500 * 2**20, 512 * 2**20)
        webapp.MEMORY_POLL_SECONDS = 0.05
        try:
            async with TestClient(TestServer(app)) as client:
                started = time.monotonic()
                created = await client.post("/api/jobs", json=_payload("slow"))  # the fake run sleeps 30 s
                job = await _wait_until_finished(client, (await created.json())["id"])
                assert job["status"] == "failed" and "more memory" in job["error"], job
                assert time.monotonic() - started < 15, "stopped at once, not after the run"
        finally:
            webapp.memory_use, webapp.MEMORY_POLL_SECONDS = original, poll

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def test_expiry_tolerates_dates_without_a_zone_and_prunes_rate_limits():
    async def scenario(tmp: Path) -> None:
        runs = tmp / "runs"
        old = runs / "old-report"
        old.mkdir(parents=True)
        (old / "job.json").write_text(json.dumps({
            "id": "old-report", "owner": "", "accounts": [{"login": "alice", "emails": []}], "options": {},
            "created_at": "2020-01-01T00:00:00", "finished_at": "2020-01-01T00:05:00", "status": "done",
        }), encoding="utf-8")
        manager = JobManager(Settings(jobs_dir=runs, retention_hours=1))
        manager.load_history()
        assert "old-report" in manager.jobs
        manager._recent["1.2.3.4"] = __import__("collections").deque([time.monotonic() - 7200])
        assert manager.expire() == 1 and not old.exists()
        assert "1.2.3.4" not in manager._recent

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in _all_tests():
        test()
        print("ok ", test.__name__)
