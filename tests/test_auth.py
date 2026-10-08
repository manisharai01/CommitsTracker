"""GitHub sign-in, report history and hosting settings - no network, no database.

The GitHub calls of the OAuth flow are replaced by fakes, the history uses
MemoryStore (or a fake asyncpg pool), and a stand-in for github_report.py
reports which token reached it through its environment.

Run directly:

    python tests/test_auth.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the package (and webui.py) importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiohttp  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402
from yarl import URL  # noqa: E402

import webui  # noqa: E402
from github_contrib import auth, pdfexport  # noqa: E402
from github_contrib.store import (  # noqa: E402
    SCHEMA_SQL,
    MemoryStore,
    PostgresStore,
    ReportRow,
    Store,
    StoreError,
)
from github_contrib.webapp import (  # noqa: E402
    CHILD_ENV_ALLOWLIST,
    MANAGER,
    Job,
    RequestError,
    Settings,
    build_env,
    create_app,
    parse_job_request,
    public_settings,
)

PUBLIC_URL = "http://localhost:8765"  # public mode without TLS is allowed on loopback only
ORIGIN = {"Origin": PUBLIC_URL}
SECRET = "test-session-secret-0123456789"
CLIENT_ID = "Iv1.testclient"
CLIENT_SECRET = "client-secret-value"
OAUTH_TOKEN = "gho_" + "Q1w2E3r4T5" * 3 + "abcdef"  # 40 chars, like a real OAuth token
PAT = "ghp_" + "a1B2c3D4e5" * 4
UID, LOGIN = 4242, "octo-cat"
OTHER_UID, OTHER_LOGIN = 5151, "hubber"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Stand-in CLI: prints a hash of the token each --user got in its environment
# (the raw token would be redacted from the log), echoes the raw token and its
# argv (to prove redaction / no token on the command line), writes a run.log
# containing the token (to prove it is scrubbed), then a finished report.
FAKE_CLI = textwrap.dedent(
    """
    import hashlib, os, sys, time
    args = sys.argv[1:]
    users = [args[i + 1] for i, a in enumerate(args) if a == "--user"]
    out = args[args.index("--output") + 1]
    def env_token(login):
        return os.environ.get("GITHUB_TOKEN_" + login.upper().replace("-", "_"), "")
    for login in users:
        print("sha-%s=%s" % (login, hashlib.sha256(env_token(login).encode()).hexdigest()), flush=True)
    print("token=" + env_token(users[0]), flush=True)
    print("argv=" + " ".join(args), flush=True)
    print("no-sandbox=" + os.environ.get("PDF_NO_SANDBOX", "unset"), flush=True)
    if "slow" in users:
        time.sleep(30)
    sys.stderr.write("Branches:  50%|#####     | 1/2 [00:01<00:01]\\n")
    sys.stderr.flush()
    with open(os.path.join(out, "run.log"), "w", encoding="utf-8") as fh:
        fh.write("header Authorization: Bearer " + env_token(users[0]) + "\\n")
    with open(os.path.join(out, "contribution_summary.csv"), "w", encoding="utf-8-sig") as fh:
        fh.write("metric,value\\ntotal_lifetime_commits,3\\nactive_days,2\\n")
    for name in ("report.pdf", "report.html"):
        with open(os.path.join(out, name), "w") as fh:
            fh.write(name)
    """
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def patched(target, **values):
    """Temporarily replace attributes of ``target`` (a module or object)."""
    saved = {name: getattr(target, name) for name in values}
    for name, value in values.items():
        setattr(target, name, value)
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(target, name, value)


@contextlib.contextmanager
def env_vars(**values):
    saved = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        os.environ[name] = value
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _fake(tmp: Path) -> Path:
    fake = tmp / "fake_cli.py"
    fake.write_text(FAKE_CLI, encoding="utf-8")
    return fake


def _auth_settings(tmp: Path, **overrides) -> Settings:
    settings = public_settings(
        PUBLIC_URL,
        jobs_dir=tmp / "runs",
        github_client_id=CLIENT_ID,
        github_client_secret=CLIENT_SECRET,
        session_secret=SECRET,
        **overrides,
    )
    settings.script = _fake(tmp)
    return settings


def _cookie(uid: int = UID, login: str = LOGIN, now: float | None = None) -> str:
    return auth.SessionCodec(SECRET).encode(uid, login, now=now)


def _sign_in(jar: aiohttp.CookieJar, url: URL, **kwargs) -> None:
    """Put a valid session cookie in a client's jar (as the callback would)."""
    jar.update_cookies({"ct_auth": _cookie(**kwargs)}, response_url=url)


def _payload(*logins: str, token: str = PAT, **period) -> dict:
    payload: dict = {"accounts": [{"login": login, "token": token, "emails": ""} for login in logins]}
    if period:
        payload["period"] = period
    return payload


async def _jobs(client, url=None) -> list[dict]:
    if url is None:
        response = await client.get("/api/jobs")
    else:
        response = await client.get(url("/api/jobs"))
    assert response.status == 200, response.status
    return (await response.json())["jobs"]


async def _wait_until_finished(client: TestClient, job_id: str) -> dict:
    for _ in range(600):
        job = next(j for j in await _jobs(client) if j["id"] == job_id)
        if job["status"] not in ("queued", "running"):
            return job
        await asyncio.sleep(0.05)
    raise AssertionError("job did not finish")


async def _until(predicate, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.05)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class RecordingStore(MemoryStore):
    """MemoryStore that remembers every status written for a report."""

    def __init__(self) -> None:
        super().__init__()
        self.statuses: dict[str, list[str]] = {}

    async def add_report(self, report_id, user_id, period, status):
        await super().add_report(report_id, user_id, period, status)
        self.statuses.setdefault(report_id, []).append(status)

    async def set_status(self, report_id, status):
        await super().set_status(report_id, status)
        self.statuses.setdefault(report_id, []).append(status)


class BrokenStore(Store):
    """A database that is always down."""

    async def ensure_user(self, user_id):
        raise StoreError("down")

    async def add_report(self, report_id, user_id, period, status):
        raise StoreError("down")

    async def set_status(self, report_id, status):
        raise OSError("down")

    async def list_reports(self, user_id, limit=100):
        raise TimeoutError("down")

    async def delete_report(self, report_id, user_id):
        raise StoreError("down")

    async def fail_unfinished(self):
        raise StoreError("down")


# ---------------------------------------------------------------------------
# session cookie
# ---------------------------------------------------------------------------


def test_session_cookie_roundtrip_tamper_and_expiry():
    codec = auth.SessionCodec(SECRET)
    now = 1_800_000_000
    value = codec.encode(UID, LOGIN, now=now)
    assert "=" not in value and ";" not in value, "cookie values never need quoting"
    assert LOGIN not in value, "the cookie is encrypted, not just signed"

    session = codec.decode(value, now=now + 60)
    assert (session.uid, session.login) == (UID, LOGIN)
    assert session.exp == now + 14 * 24 * 3600
    assert not hasattr(session, "token"), "a session holds no GitHub token"
    assert "SessionCodec()" == repr(codec)

    middle = len(value) // 2
    tampered = value[:middle] + ("A" if value[middle] != "A" else "B") + value[middle + 1:]
    assert codec.decode(tampered, now=now) is None
    assert auth.SessionCodec("a-different-secret-entirely").decode(value, now=now) is None
    assert codec.decode(value, now=now + 14 * 24 * 3600 + 1) is None, "expired after 14 days"
    for junk in ("", "garbage", "x" * 5000, value + "!"):
        assert codec.decode(junk, now=now) is None

    # Correctly encrypted but malformed contents are refused too.
    from cryptography.fernet import Fernet

    fernet = Fernet(auth.fernet_key(SECRET))
    for data in (
        {"uid": "4242", "login": LOGIN, "exp": now + 99},
        {"uid": UID, "login": "--x", "exp": now + 99},
        {"uid": UID, "login": LOGIN},
        # A cookie from when sign-in still kept a GitHub token: dropped.
        {"uid": UID, "login": LOGIN, "token": OAUTH_TOKEN, "exp": now + 99},
    ):
        forged = fernet.encrypt_at_time(json.dumps(data).encode(), now).decode().rstrip("=")
        assert codec.decode(forged, now=now) is None, data

    # The Fernet key is sha256(SESSION_SECRET) in urlsafe base64.
    assert auth.fernet_key("abc") == base64.urlsafe_b64encode(hashlib.sha256(b"abc").digest())
    for weak in ("", "short"):
        try:
            auth.SessionCodec(weak)
        except ValueError as exc:
            assert "SESSION_SECRET" in str(exc)
        else:
            raise AssertionError("accepted a weak SESSION_SECRET")
    print("ok  test_session_cookie_roundtrip_tamper_and_expiry")


def test_auth_settings_and_session_secret_required():
    with tempfile.TemporaryDirectory() as tmp:
        settings = public_settings(
            PUBLIC_URL, jobs_dir=Path(tmp), github_client_id=CLIENT_ID, github_client_secret=CLIENT_SECRET
        )
        assert settings.auth and settings.auth_cookie == "ct_auth"
        assert settings.redirect_uri == f"{PUBLIC_URL}/auth/callback"
        assert CLIENT_SECRET not in repr(settings)
        try:
            create_app(settings)
        except ValueError as exc:
            assert "SESSION_SECRET" in str(exc)
        else:
            raise AssertionError("started GitHub sign-in without SESSION_SECRET")
    secure = public_settings("https://reports.example.com", github_client_id="a", github_client_secret="b")
    assert secure.auth_cookie == "__Host-ct_auth" and secure.state_cookie == "__Host-ct_oauth_state"
    # Sign-in needs public mode and both OAuth credentials.
    assert not public_settings(PUBLIC_URL, github_client_id=CLIENT_ID).auth
    assert not Settings(github_client_id=CLIENT_ID, github_client_secret=CLIENT_SECRET).auth
    print("ok  test_auth_settings_and_session_secret_required")


# ---------------------------------------------------------------------------
# OAuth flow
# ---------------------------------------------------------------------------


def test_oauth_login_and_callback():
    calls: list[tuple] = []

    async def fake_exchange(client_id, client_secret, code, redirect_uri):
        calls.append(("exchange", client_id, client_secret, code, redirect_uri))
        if code == "bad-code":
            raise auth.AuthError("exchange", "bad_verification_code")
        return OAUTH_TOKEN

    async def fake_user(token):
        calls.append(("user", token))
        return UID, LOGIN

    revoked: list[tuple[str, str, str]] = []

    async def fake_revoke(client_id, client_secret, token):
        revoked.append((client_id, client_secret, token))

    async def scenario(tmp: Path) -> None:
        store = MemoryStore()
        app = create_app(_auth_settings(tmp), store=store)
        async with TestClient(TestServer(app)) as client:

            async def start_login() -> str:
                response = await client.get("/auth/login", allow_redirects=False)
                assert response.status == 302
                target = URL(response.headers["Location"])
                assert (target.scheme, target.host, target.path) == ("https", "github.com", "/login/oauth/authorize")
                query = target.query
                assert query["client_id"] == CLIENT_ID
                assert query["redirect_uri"] == f"{PUBLIC_URL}/auth/callback"
                assert "scope" not in query, "sign-in asks for no permissions"
                assert query["allow_signup"] == "true"
                cookie = response.headers["Set-Cookie"]
                assert cookie.startswith("ct_oauth_state=") and "HttpOnly" in cookie
                assert "SameSite=Lax" in cookie and "Max-Age=600" in cookie and "Path=/" in cookie
                assert response.headers["Cache-Control"] == "no-store"
                assert len(query["state"]) >= 32
                return query["state"]

            async def callback(**params) -> aiohttp.ClientResponse:
                return await client.get("/auth/callback", params=params, allow_redirects=False)

            # A state that doesn't match this browser's cookie is refused,
            # before GitHub is ever called.
            await start_login()
            refused = await callback(code="abc", state="not-the-state")
            assert refused.status == 302 and refused.headers["Location"] == "/?auth_error=state"
            assert "ct_auth=" not in refused.headers.get("Set-Cookie", "")
            assert calls == []
            # No state cookie at all (e.g. a link opened in another browser).
            client.session.cookie_jar.clear()
            assert (await callback(code="abc", state="x")).headers["Location"] == "/?auth_error=state"

            # The user clicked "Cancel" on GitHub.
            state = await start_login()
            denied = await callback(error="access_denied", error_description="<script>", state=state)
            assert denied.headers["Location"] == "/?auth_error=denied" and calls == []

            # GitHub rejects the code: a short error code, never GitHub's text.
            state = await start_login()
            bad = await callback(code="bad-code", state=state)
            assert bad.headers["Location"] == "/?auth_error=exchange"
            assert not (await (await client.get("/api/me")).json())["signed_in"]

            calls.clear()
            state = await start_login()
            ok = await callback(code="good-code", state=state)
            assert ok.status == 302 and ok.headers["Location"] == "/"
            assert calls == [
                ("exchange", CLIENT_ID, CLIENT_SECRET, "good-code", f"{PUBLIC_URL}/auth/callback"),
                ("user", OAUTH_TOKEN),
            ]
            set_cookies = ok.headers.getall("Set-Cookie")
            session_cookie = next(c for c in set_cookies if c.startswith("ct_auth="))
            assert "HttpOnly" in session_cookie and "SameSite=Lax" in session_cookie
            assert "Path=/" in session_cookie and OAUTH_TOKEN not in session_cookie
            assert any(c.startswith("ct_oauth_state=") and "Max-Age=0" in c for c in set_cookies), set_cookies
            assert UID in store.users and store.reports == {}
            # The token only told us who signed in: it is revoked straight away.
            await _until(lambda: revoked == [(CLIENT_ID, CLIENT_SECRET, OAUTH_TOKEN)])

            me = await (await client.get("/api/me")).json()
            assert me == {
                "signed_in": True,
                "login": LOGIN,
                "id": UID,
                "avatar_url": f"https://avatars.githubusercontent.com/u/{UID}?v=4",
            }
            assert (await client.get("/api/jobs")).status == 200

            # The state is single-use: replaying the callback fails.
            assert (await callback(code="good-code", state=state)).headers["Location"] == "/?auth_error=state"

    with patched(auth, exchange_code=fake_exchange, fetch_user=fake_user, revoke_token=fake_revoke):
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp)))
    print("ok  test_oauth_login_and_callback")


def test_signed_out_requests_get_401():
    async def scenario(tmp: Path) -> None:
        app = create_app(_auth_settings(tmp), store=MemoryStore())
        async with TestClient(TestServer(app)) as client:
            for path in ("/api/jobs", "/api/jobs/x/log", "/api/jobs/x/files/pdf", "/api/jobs/x/linkedin"):
                response = await client.get(path)
                assert response.status == 401, path
                assert (await response.json()) == {"error": "Sign in with GitHub first."}
            post = await client.post("/api/jobs", json=_payload(LOGIN, token=PAT), headers=ORIGIN)
            assert post.status == 401
            delete = await client.delete(f"/api/jobs/{uuid.uuid4()}", json={}, headers=ORIGIN)
            assert delete.status == 401
            # The Origin guard still runs first.
            assert (await client.post("/api/jobs", json=_payload(LOGIN, token=PAT))).status == 403

            config = await (await client.get("/api/config")).json()
            assert config["auth"] is True and config["mode"] == "public" and config["env_logins"] == []
            assert (await (await client.get("/api/me")).json()) == {"signed_in": False}
            for path in ("/", "/static/app.js", "/static/app.css", "/healthz"):
                response = await client.get(path)
                assert response.status == 200, path
                assert "Set-Cookie" not in response.headers, "no anonymous session in sign-in mode"
            assert await (await client.get("/healthz")).text() == "ok"
            assert (await client.get("/shared/not-a-real-token-at-all-xxxxxx")).status == 404

            # Forged and expired cookies are refused and cleared.
            url = client.make_url("/")
            client.session.cookie_jar.update_cookies({"ct_auth": "forged"}, response_url=url)
            forged = await client.get("/api/jobs")
            assert forged.status == 401 and "ct_auth=" in forged.headers.get("Set-Cookie", "")
            stale = time.time() - 15 * 24 * 3600
            _sign_in(client.session.cookie_jar, url, now=stale)
            assert (await client.get("/api/jobs")).status == 401
            _sign_in(client.session.cookie_jar, url)
            assert (await client.get("/api/jobs")).status == 200

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_signed_out_requests_get_401")


# ---------------------------------------------------------------------------
# jobs in sign-in mode
# ---------------------------------------------------------------------------


def test_reports_use_each_accounts_own_token():
    other_pat = "ghp_" + "Z9y8X7w6V5" * 4

    async def scenario(tmp: Path) -> None:
        store = RecordingStore()
        settings = _auth_settings(tmp)
        app = create_app(settings, store=store)
        async with TestClient(TestServer(app)) as client:
            _sign_in(client.session.cookie_jar, client.make_url("/"))

            # Signing in never stands in for a token - not even the signed-in
            # account's own row - so no report is silently read with the
            # wrong account's access.
            for logins in ((LOGIN,), ("someone-else",), (LOGIN, "someone-else")):
                refused = await client.post("/api/jobs", json=_payload(*logins, token=""), headers=ORIGIN)
                assert refused.status == 400, logins
                assert "personal access token" in (await refused.json())["error"]
            assert store.reports == {}

            # Signed in as one account, a report on two others, each with its own token.
            payload = {
                "accounts": [{"login": "someone-else", "token": PAT}, {"login": "pat-user", "token": other_pat}],
                "period": {"since": "2026-01-01", "until": "2026-03-31", "timezone": "UTC"},
            }
            created = await client.post("/api/jobs", json=payload, headers=ORIGIN)
            assert created.status == 201, await created.text()
            job_id = (await created.json())["id"]
            assert str(uuid.UUID(job_id)) == job_id and LOGIN not in job_id, "uuid ids, no logins"

            job = await _wait_until_finished(client, job_id)
            assert job["status"] == "done", job
            assert set(job["files"]) == {"pdf", "html"} and "expired" not in job
            log = "\n".join((await (await client.get(f"/api/jobs/{job_id}/log")).json())["lines"])
            # Each row's own token reached the run, through the env only.
            assert f"sha-someone-else={_sha(PAT)}" in log
            assert f"sha-pat-user={_sha(other_pat)}" in log
            argv = next(line for line in log.splitlines() if line.startswith("argv="))
            assert "••••" not in argv and "ghp_" not in argv
            assert "token=••••" in log and PAT not in log and other_pat not in log
            run_dir = settings.jobs_dir / job_id
            for path in run_dir.rglob("*"):
                text = path.read_text(encoding="utf-8", errors="ignore")
                assert PAT not in text and other_pat not in text, path
            record = json.loads((run_dir / "job.json").read_text(encoding="utf-8"))
            assert record["owner"] == f"gh:{UID}"

            # The signed-in account can be in its own report too, with its token.
            mine = await client.post("/api/jobs", json=_payload(LOGIN, token=PAT), headers=ORIGIN)
            assert mine.status == 201, await mine.text()
            await _wait_until_finished(client, (await mine.json())["id"])

            # The history row: id, user, period, status - and the status trail.
            await _until(lambda: store.statuses.get(job_id, [])[-1:] == ["done"])
            row = store.reports[job_id]
            assert (row.user_id, row.period, row.status) == (UID, "2026-01-01..2026-03-31", "done")
            assert store.statuses[job_id] == ["queued", "running", "done"]

            # Another signed-in user sees nothing of it.
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as other:
                url = client.make_url
                _sign_in(other.cookie_jar, url("/"), uid=OTHER_UID, login=OTHER_LOGIN)
                assert await _jobs(other, url) == []
                for path in (f"/api/jobs/{job_id}/log", job["files"]["pdf"]):
                    assert (await other.get(url(path))).status == 404
                gone = await other.delete(url(f"/api/jobs/{job_id}"), json={}, headers=ORIGIN)
                assert gone.status == 404 and job_id in store.reports

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_reports_use_each_accounts_own_token")


def test_public_runs_need_a_token_per_account():
    blank = {"accounts": [{"login": "alice", "token": ""}, {"login": "bob", "token": PAT}]}
    try:
        parse_job_request(blank, {}, require_tokens=True)
    except RequestError as exc:
        assert "personal access token" in str(exc) and "alice" in str(exc)
    else:
        raise AssertionError("accepted a blank token in public mode")
    other_pat = "ghp_" + "Z9y8X7w6V5" * 4
    full = {"accounts": [{"login": "alice", "token": other_pat}, {"login": "bob", "token": PAT}]}
    request = parse_job_request(full, {}, require_tokens=True)
    env = build_env(request, {"PATH": "p", "PDF_NO_SANDBOX": "1", "DATABASE_URL": "x"}, isolated=True)
    assert env["GITHUB_TOKEN_ALICE"] == other_pat and env["GITHUB_TOKEN_BOB"] == PAT
    assert env["PDF_NO_SANDBOX"] == "1" and "DATABASE_URL" not in env
    assert "PDF_NO_SANDBOX" in CHILD_ENV_ALLOWLIST and "SESSION_SECRET" not in CHILD_ENV_ALLOWLIST
    print("ok  test_public_runs_need_a_token_per_account")


def test_history_merges_live_jobs_with_expired_rows():
    async def scenario(tmp: Path) -> None:
        store = MemoryStore()
        old_done, stale, foreign = (str(uuid.uuid4()) for _ in range(3))
        jan = datetime(2026, 1, 31, 9, 30, tzinfo=timezone.utc)
        store.users.update({UID: jan, OTHER_UID: jan})
        store.reports[old_done] = ReportRow(old_done, UID, jan, "2026-01-01..2026-01-31", "done")
        store.reports[stale] = ReportRow(stale, UID, jan - timedelta(days=1), "..", "running")
        store.reports[foreign] = ReportRow(foreign, OTHER_UID, jan, "..", "done")

        settings = _auth_settings(tmp)
        app = create_app(settings, store=store)
        async with TestClient(TestServer(app)) as client:
            # Startup marked the run that died with the last server as failed.
            assert store.reports[stale].status == "failed"
            _sign_in(client.session.cookie_jar, client.make_url("/"))
            created = await client.post("/api/jobs", json=_payload(LOGIN), headers=ORIGIN)
            live_id = (await created.json())["id"]
            await _wait_until_finished(client, live_id)

            jobs = await _jobs(client)
            assert [j["id"] for j in jobs] == [live_id, old_done, stale], "newest first"
            assert foreign not in {j["id"] for j in jobs}
            assert "expired" not in jobs[0] and jobs[0]["files"]
            assert jobs[1] == {
                "id": old_done,
                "status": "done",
                "created_at": "2026-01-31T09:30:00.000+00:00",
                "finished_at": "",
                "options": {"since": "2026-01-01", "until": "2026-01-31", "timezone": "UTC"},
                "accounts": [{"login": LOGIN, "emails": []}],
                "logins": [LOGIN],
                "files": {},
                "summary": {},
                "warnings": [],
                "error": "",
                "share_url": "",
                "log_count": 0,
                "expired": True,
            }
            assert jobs[2]["status"] == "failed" and jobs[2]["options"]["since"] == ""

            # Files expire after the retention period; the entry stays, as expired.
            manager = app[MANAGER]
            await _until(lambda: store.reports.get(live_id) and store.reports[live_id].status == "done")
            assert manager.expire(datetime.now(timezone.utc) + timedelta(hours=48)) == 1
            assert not (settings.jobs_dir / live_id).exists()
            expired = next(j for j in await _jobs(client) if j["id"] == live_id)
            assert expired["expired"] is True and expired["status"] == "done" and expired["files"] == {}

            # Deleting an expired entry deletes its row; nobody else's rows can be deleted.
            assert (await client.delete(f"/api/jobs/{old_done}", json={}, headers=ORIGIN)).status == 200
            assert old_done not in store.reports
            assert (await client.delete(f"/api/jobs/{foreign}", json={}, headers=ORIGIN)).status == 404
            assert foreign in store.reports
            assert (await client.delete("/api/jobs/not-a-uuid", json={}, headers=ORIGIN)).status == 404
            for path in (f"/api/jobs/{stale}/log", f"/api/jobs/{stale}/files/pdf"):
                assert (await client.get(path)).status == 404

            # Deleting a report that still has its files removes files and row.
            created = await client.post("/api/jobs", json=_payload(LOGIN), headers=ORIGIN)
            second = (await created.json())["id"]
            await _wait_until_finished(client, second)
            assert (await client.delete(f"/api/jobs/{second}", json={}, headers=ORIGIN)).status == 200
            assert second not in store.reports and not (settings.jobs_dir / second).exists()
            assert [j["id"] for j in await _jobs(client)] == [live_id, stale]

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_history_merges_live_jobs_with_expired_rows")


def test_store_failures_never_break_sign_in_or_runs():
    async def fake_exchange(*_args):
        return OAUTH_TOKEN

    async def fake_user(_token):
        return UID, LOGIN

    async def scenario(tmp: Path) -> None:
        app = create_app(_auth_settings(tmp), store=BrokenStore())  # startup survives too
        async with TestClient(TestServer(app)) as client:
            login = await client.get("/auth/login", allow_redirects=False)
            state = URL(login.headers["Location"]).query["state"]
            back = await client.get("/auth/callback", params={"code": "c", "state": state}, allow_redirects=False)
            assert back.headers["Location"] == "/"
            assert (await (await client.get("/api/me")).json())["signed_in"]

            created = await client.post("/api/jobs", json=_payload(LOGIN), headers=ORIGIN)
            assert created.status == 201
            job = await _wait_until_finished(client, (await created.json())["id"])
            assert job["status"] == "done"
            assert [j["id"] for j in await _jobs(client)] == [job["id"]]
            assert (await client.delete(f"/api/jobs/{job['id']}", json={}, headers=ORIGIN)).status == 200
            unknown = await client.delete(f"/api/jobs/{uuid.uuid4()}", json={}, headers=ORIGIN)
            assert unknown.status == 503

    async def fake_revoke(*_args):
        return None

    with patched(auth, exchange_code=fake_exchange, fetch_user=fake_user, revoke_token=fake_revoke):
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp)))
    print("ok  test_store_failures_never_break_sign_in_or_runs")


def test_logout_clears_the_session_and_leaves_runs_alone():
    revoked: list[tuple[str, str, str]] = []

    async def fake_revoke(client_id, client_secret, token):
        revoked.append((client_id, client_secret, token))

    async def scenario(tmp: Path) -> None:
        app = create_app(_auth_settings(tmp), store=MemoryStore())
        async with TestClient(TestServer(app)) as client:
            url = client.make_url("/")
            _sign_in(client.session.cookie_jar, url)
            assert (await client.post("/auth/logout", json={})).status == 403, "Origin guard"
            out = await client.post("/auth/logout", json={}, headers=ORIGIN)
            assert out.status == 200 and await out.json() == {"ok": True}
            assert "ct_auth=" in out.headers["Set-Cookie"] and "Max-Age=0" in out.headers["Set-Cookie"]
            assert not (await (await client.get("/api/me")).json())["signed_in"]
            assert (await client.get("/api/jobs")).status == 401

            # A report running at sign-out keeps going: it uses its own tokens.
            _sign_in(client.session.cookie_jar, url)
            created = await client.post("/api/jobs", json=_payload("slow"), headers=ORIGIN)
            job_id = (await created.json())["id"]
            manager = app[MANAGER]
            await _until(lambda: manager.jobs[job_id].status == "running")
            assert (await client.post("/auth/logout", json={}, headers=ORIGIN)).status == 200
            await asyncio.sleep(0.3)
            assert manager.jobs[job_id].status == "running"
            manager.cancel(manager.jobs[job_id])
            await _until(lambda: manager.jobs[job_id].status == "cancelled")
            assert revoked == [], "the session holds no token, so there is nothing to revoke"

    with patched(auth, revoke_token=fake_revoke):
        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(Path(tmp)))
    print("ok  test_logout_clears_the_session_and_leaves_runs_alone")


def test_hourly_limit_counts_per_account():
    async def scenario(tmp: Path) -> None:
        app = create_app(_auth_settings(tmp, max_jobs_per_hour=1, max_active_per_user=5), store=MemoryStore())
        async with TestClient(TestServer(app)) as first:
            _sign_in(first.session.cookie_jar, first.make_url("/"))
            assert (await first.post("/api/jobs", json=_payload(LOGIN), headers=ORIGIN)).status == 201
            again = await first.post("/api/jobs", json=_payload(LOGIN), headers=ORIGIN)
            assert again.status == 429 and "from you" in (await again.json())["error"]
            # Same address, different account: not limited by the first one.
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as second:
                url = first.make_url
                _sign_in(second.cookie_jar, url("/"), uid=OTHER_UID, login=OTHER_LOGIN)
                ok = await second.post(url("/api/jobs"), json=_payload(OTHER_LOGIN), headers=ORIGIN)
                assert ok.status == 201
            manager = app[MANAGER]
            await _until(lambda: not any(job.active for job in manager.jobs.values()))

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_hourly_limit_counts_per_account")


def test_local_and_anonymous_public_modes_unchanged():
    async def scenario(tmp: Path) -> None:
        local = Settings(jobs_dir=tmp / "local", script=_fake(tmp))
        app = create_app(local, store=MemoryStore())
        assert app[MANAGER].store is None
        async with TestClient(TestServer(app)) as client:
            config = await (await client.get("/api/config")).json()
            assert config["mode"] == "local" and config["auth"] is False
            assert (await (await client.get("/api/me")).json()) == {"signed_in": False}
            assert (await client.get("/auth/login", allow_redirects=False)).status == 404
            assert await (await client.get("/healthz")).text() == "ok"
            created = await client.post("/api/jobs", json=_payload("alice", token=PAT))
            job_id = (await created.json())["id"]
            assert re.match(r"^\d{8}-\d{6}-alice-[0-9a-f]{16}$", job_id), job_id
            await _until(lambda: not app[MANAGER].jobs[job_id].active)

        # Public mode without OAuth credentials: anonymous sessions, as before.
        public = public_settings(PUBLIC_URL, jobs_dir=tmp / "public")
        app = create_app(public, store=MemoryStore())
        assert app[MANAGER].store is None
        async with TestClient(TestServer(app)) as client:
            page = await client.get("/")
            assert "ct_session=" in page.headers["Set-Cookie"]
            assert "Set-Cookie" not in (await client.get("/healthz")).headers
            assert (await (await client.get("/api/config")).json())["auth"] is False
            assert (await client.get("/api/jobs")).status == 200
            assert (await client.get("/auth/login", allow_redirects=False)).status == 404

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(scenario(Path(tmp)))
    print("ok  test_local_and_anonymous_public_modes_unchanged")


def test_branches_progress_phase():
    job = Job(id="j", out_dir=Path("."), accounts=[{"login": "a", "emails": []}], options={})
    job.add_output("Branches:  25%|##        | 3/12 [00:01<00:03]")
    assert job.phase == "Listing branches" and job.progress == {"label": "Branches", "n": 3, "total": 12}
    print("ok  test_branches_progress_phase")


# ---------------------------------------------------------------------------
# Postgres store (fake asyncpg pool - no database is contacted)
# ---------------------------------------------------------------------------


class _FakePool:
    """Just enough of an asyncpg pool (and connection) for PostgresStore."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False

    def acquire(self):
        pool = self

        class _Acquire:
            async def __aenter__(self):
                return pool

            async def __aexit__(self, *exc):
                return False

        return _Acquire()

    @contextlib.asynccontextmanager
    async def _transaction(self):
        self.calls.append(("begin", ()))
        yield
        self.calls.append(("commit", ()))

    def transaction(self):
        return self._transaction()

    async def execute(self, sql, *args):
        sql = " ".join(sql.split())
        self.calls.append((sql, args))
        if sql.startswith("delete"):
            return "DELETE 1"
        if sql.startswith("update reports set status = 'failed'"):
            return "UPDATE 2"
        return "INSERT 0 1"

    async def fetch(self, sql, *args):
        self.calls.append((" ".join(sql.split()), args))
        return self.rows

    async def close(self):
        self.closed = True

    def terminate(self):
        self.closed = True


def test_postgres_store_with_a_fake_pool():
    import asyncpg

    report_id = str(uuid.uuid4())
    created_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
    pool = _FakePool(
        [{"id": uuid.UUID(report_id), "user_id": UID, "created_at": created_at, "period": "..", "status": "done"}]
    )
    options: dict = {}

    async def fake_create_pool(dsn, **kwargs):
        options.update(dsn=dsn, **kwargs)
        return pool

    async def scenario() -> None:
        store = PostgresStore("postgresql://user:pw@db.example/postgres")
        assert "pw" not in repr(store)
        await store.start()
        assert options["min_size"] == 1 and options["max_size"] == 3
        assert options["statement_cache_size"] == 0, "needed by Supabase's poolers"
        assert pool.calls[0][0].startswith("create table if not exists users")
        assert "enable row level security" in pool.calls[0][0]

        pool.calls.clear()
        await store.add_report(report_id, UID, "2026-01-01..", "queued")
        assert [c[0].split(" (")[0] for c in pool.calls] == [
            "begin", "insert into users", "insert into reports", "commit"
        ]
        assert pool.calls[2][1] == (uuid.UUID(report_id), UID, "2026-01-01..", "queued")
        await store.set_status(report_id, "running")
        assert pool.calls[-1] == ("update reports set status = $2 where id = $1", (uuid.UUID(report_id), "running"))
        rows = await store.list_reports(UID, limit=5)
        assert rows == [ReportRow(report_id, UID, created_at, "..", "done")]
        assert pool.calls[-1][1] == (UID, 5)
        assert await store.delete_report(report_id, UID) is True
        assert await store.fail_unfinished() == 2
        await store.ensure_user(UID)
        # Ids that aren't uuids never reach the database.
        before = len(pool.calls)
        assert await store.delete_report("../etc", UID) is False
        await store.set_status("nope", "done")
        try:
            await store.add_report("nope", UID, "..", "queued")
        except StoreError:
            pass
        else:
            raise AssertionError("accepted a non-uuid id")
        assert len(pool.calls) == before
        await store.close()
        assert pool.closed

    async def unreachable(dsn, **kwargs):
        raise OSError(f"could not connect to {dsn}")

    async def failing_scenario() -> None:
        store = PostgresStore("postgresql://postgres.ref:hunter2-secret@pooler.example:5432/postgres")
        await store.start()  # logs a warning, doesn't raise
        try:
            await store.list_reports(UID)
        except StoreError as exc:
            assert "unavailable" in str(exc)  # retried later, not on every call
        else:
            raise AssertionError("expected StoreError")
        store._next_attempt = 0.0
        try:
            await store.list_reports(UID)
        except StoreError as exc:
            assert "hunter2" not in str(exc) and "postgres.ref" not in str(exc), str(exc)
        else:
            raise AssertionError("expected StoreError")

    with patched(asyncpg, create_pool=fake_create_pool):
        asyncio.run(scenario())
    with patched(asyncpg, create_pool=unreachable):
        asyncio.run(failing_scenario())
    print("ok  test_postgres_store_with_a_fake_pool")


def test_schema_sql_file_matches_the_code():
    def statements(sql: str) -> list[str]:
        without_comments = re.sub(r"--[^\n]*", "", sql)
        return [" ".join(s.split()) for s in without_comments.split(";") if s.strip()]

    shipped = (PROJECT_ROOT / "schema.sql").read_text(encoding="utf-8")
    assert statements(shipped) == statements(SCHEMA_SQL)
    assert "token" not in " ".join(statements(SCHEMA_SQL)), "no tokens in the database"
    print("ok  test_schema_sql_file_matches_the_code")


# ---------------------------------------------------------------------------
# hosting
# ---------------------------------------------------------------------------


def test_webui_defaults_from_the_environment():
    _parser, args, host, settings = webui.configure([], {})
    assert (host, args.port, settings.public) == ("127.0.0.1", 8765, False)

    render = {
        "PORT": "10000",
        "RENDER": "true",
        "RENDER_EXTERNAL_URL": "https://commitstracker.onrender.com",
        "GITHUB_CLIENT_ID": CLIENT_ID,
        "GITHUB_CLIENT_SECRET": CLIENT_SECRET,
        "SESSION_SECRET": SECRET,
        "DATABASE_URL": "postgresql://u:p@h:5432/db",
        "RETENTION_HOURS": "6",
        "PARALLEL": "1",
    }
    _parser, args, host, settings = webui.configure([], render)
    assert (host, args.port) == ("0.0.0.0", 10000)
    assert settings.public_url == "https://commitstracker.onrender.com" and settings.auth
    assert settings.trust_proxy and settings.retention_hours == 6 and settings.parallel == 1
    assert settings.database_url == render["DATABASE_URL"]

    custom = {**render, "PUBLIC_URL": "https://reports.example.com", "HOST": "::"}
    _parser, args, host, settings = webui.configure(["--retention-hours", "2"], custom)
    assert settings.public_url == "https://reports.example.com" and host == "::"
    assert settings.retention_hours == 2

    # Flags still win, and local mode stays on loopback even when PORT is set.
    _parser, args, host, settings = webui.configure(["--port", "9000"], {"PORT": "3000"})
    assert (host, args.port, settings.public) == ("127.0.0.1", 9000, False)
    print("ok  test_webui_defaults_from_the_environment")


def test_webui_takes_only_sign_in_settings_from_dotenv():
    with tempfile.TemporaryDirectory() as tmp:
        dotenv = Path(tmp) / ".env"
        dotenv.write_text(
            f"GITHUB_TOKEN_ALICE={OAUTH_TOKEN}\nGITHUB_TOKEN={OAUTH_TOKEN}\nAUTHOR_EMAILS=a@x.com\n"
            f"DATABASE_URL=postgresql://u:p@h:5432/db\nGITHUB_CLIENT_ID={CLIENT_ID}\n"
            f"GITHUB_CLIENT_SECRET={CLIENT_SECRET}\nSESSION_SECRET=from-dotenv-0123456789\n"
            "PUBLIC_URL=http://localhost:8765\n",
            encoding="utf-8",
        )
        env = webui._with_dotenv_settings({"SESSION_SECRET": SECRET}, dotenv)
    assert env["DATABASE_URL"] == "postgresql://u:p@h:5432/db" and env["GITHUB_CLIENT_ID"] == CLIENT_ID
    assert env["SESSION_SECRET"] == SECRET, "a real environment variable wins"
    assert not any(key.startswith("GITHUB_TOKEN") or key == "AUTHOR_EMAILS" for key in env), (
        "tokens in .env must never reach the server"
    )
    # PUBLIC_URL in .env is enough: plain `python webui.py` starts with sign-in.
    _parser, _args, host, settings = webui.configure([], env)
    assert settings.public_url == "http://localhost:8765" and settings.auth and settings.database_url
    assert host == "127.0.0.1"
    print("ok  test_webui_takes_only_sign_in_settings_from_dotenv")


def test_pdf_no_sandbox_only_when_asked():
    seen: list[list[str]] = []

    def fake_run(cmd, **_kwargs):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "")

    with tempfile.TemporaryDirectory() as tmp:
        html = Path(tmp) / "report.html"
        html.write_text("<html></html>", encoding="utf-8")
        with patched(pdfexport, find_browser=lambda: "chromium"), patched(subprocess, run=fake_run):
            with env_vars(PDF_NO_SANDBOX="0"):
                pdfexport.export_pdf(html)
            assert seen and all("--no-sandbox" not in cmd for cmd in seen)
            seen.clear()
            with env_vars(PDF_NO_SANDBOX="1"):
                pdfexport.export_pdf(html)
            assert seen and all("--no-sandbox" in cmd for cmd in seen)
            assert all(cmd[1].startswith("--headless") for cmd in seen)
    print("ok  test_pdf_no_sandbox_only_when_asked")


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
