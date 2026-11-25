"""Rate limits, time limits and partial failures during collection - no network.

Run directly:

    python tests/test_collection_limits.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import sys
import time
from email.utils import formatdate
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib import graphql  # noqa: E402
from github_contrib.client import DeadlineError, GitHubClient, RateLimitedError  # noqa: E402
from github_contrib.discovery import search_all  # noqa: E402
from github_contrib.models import Coverage  # noqa: E402
from github_contrib.report import TIME_LIMIT_NOTE, _guard  # noqa: E402
from tests.test_graphql import FakeGraphQL, _model, _pr_node, _repo  # noqa: E402

graphql.RETRY_PAUSE = 0  # retried pages don't wait in tests


class _Response:
    def __init__(self, status, json_data=None, headers=None, text="", bad_json=False):
        self.status, self._json, self.headers, self._text, self._bad = status, json_data, headers or {}, text, bad_json

    async def json(self):
        if self._bad:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, method, url, **_kw):
        self.calls += 1
        return self.responses.pop(0)


def _client(responses, **kw) -> GitHubClient:
    return GitHubClient(token="t", session=_Session(responses), semaphore=asyncio.Semaphore(4), login="neha", **kw)


async def _no_sleep(*_a, **_k):
    return None


def _run_without_sleeping(coro_fn):
    real = asyncio.sleep

    async def runner():
        asyncio.sleep = _no_sleep
        try:
            return await coro_fn()
        finally:
            asyncio.sleep = real

    return asyncio.run(runner())


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


def test_rate_limit_waits_follow_githubs_clock():
    now = time.time()
    # GitHub says it is 30 s before the reset; this machine's clock is 2 minutes fast.
    headers = {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(now) - 90),
               "Date": formatdate(now - 120, usegmt=True)}
    wait = GitHubClient._rate_limit_wait(headers)
    assert 25 <= wait <= 35, wait
    # Without GitHub's clock a reset that looks past may be a fast local clock.
    assert GitHubClient._rate_limit_wait({"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}) == 60.0
    assert GitHubClient._rate_limit_wait({"Retry-After": "7"}) == 7.0
    assert GitHubClient._rate_limit_wait({"X-RateLimit-Remaining": "12", "X-RateLimit-Reset": "0"}) is None


def test_secondary_limit_pauses_the_whole_token_for_a_minute_or_more():
    secondary = _Response(403, headers={"X-RateLimit-Remaining": "4000"},
                          text="You have exceeded a secondary rate limit.")
    client = _client([secondary, _Response(200, {"ok": True})])
    before = time.monotonic()

    async def run():
        return await client.request("GET", "/x")

    data, _headers, status = _run_without_sleeping(run)
    assert (data, status) == ({"ok": True}, 200)
    assert client.rate_limit_waits == 1
    assert client._resume_at - before >= 60, "GitHub asks for at least a minute"
    assert GitHubClient._secondary_wait(3) >= 240, "then exponentially longer"


def test_rate_limit_retries_end_with_a_distinct_error():
    secondary = [
        _Response(429, headers={}, text="slow down") for _ in range(20)
    ]
    client = _client(secondary, max_retries=2)

    async def run():
        try:
            await client.request("GET", "/x")
        except RateLimitedError as exc:
            return exc
        raise AssertionError("expected RateLimitedError")

    exc = _run_without_sleeping(run)
    assert exc.status == 429 and client.session.calls == 9  # 8 rate-limit retries, then give up


def test_deadline_stops_requests_and_long_waits():
    past = _client([_Response(200, {"ok": True})], deadline=time.time() - 1)

    async def first():
        await past.request("GET", "/x")

    try:
        asyncio.run(first())
        raise AssertionError("a request past the deadline must not be sent")
    except DeadlineError:
        assert past.session.calls == 0

    # A one-hour rate-limit wait that would end after the deadline: stop now.
    limited = _Response(403, headers={"X-RateLimit-Remaining": "0",
                                      "X-RateLimit-Reset": str(int(time.time()) + 3600),
                                      "Date": formatdate(time.time(), usegmt=True)}, text="API rate limit exceeded")
    soon = _client([limited], deadline=time.time() + 30)
    started = time.monotonic()
    try:
        asyncio.run(soon.request("GET", "/x"))
        raise AssertionError("expected DeadlineError")
    except DeadlineError:
        assert time.monotonic() - started < 5, "no waiting for a limit that outlasts the run"


def test_unreadable_json_is_retried_not_fatal():
    client = _client([_Response(200, bad_json=True), _Response(200, [1, 2])])

    async def run():
        return await client.request("GET", "/x")

    data, _headers, status = _run_without_sleeping(run)
    assert (data, status) == ([1, 2], 200) and client.session.calls == 2


# ---------------------------------------------------------------------------
# GraphQL walks
# ---------------------------------------------------------------------------


def test_one_hidden_pull_request_is_noted_not_fatal():
    model = _model()
    model.commit("c1")
    model.prs["neha"] = [_pr_node(model, i, "acme/app", ["c1"]) for i in range(1, 8)]
    gql = FakeGraphQL(model)
    original = gql.graphql

    async def sso(query):
        data, errors = await original(query)
        for alias, value in data.items():
            nodes = ((value or {}).get("pullRequests") or {}).get("nodes")
            if nodes and len(nodes) > 3:
                nodes[3] = None
                errors = errors + [{"type": "FORBIDDEN", "path": [alias, "pullRequests", "nodes", 3],
                                    "message": "Resource protected by organization SAML enforcement."}]
        return data, errors

    gql.graphql = sso
    coverage = Coverage()
    data = asyncio.run(graphql.collect_pull_requests({"neha": gql}, ["neha"], inflight=1, coverage=coverage))
    assert data is not None, "one hidden pull request must not send every pull request to REST"
    assert len(data.prs) == 6
    assert len(coverage.notes) == 1 and "SAML" in coverage.notes[0] and "1 pull request" in coverage.notes[0]


def test_transient_alias_errors_are_retried():
    model = _model()
    gql = FakeGraphQL(model)
    original = gql.graphql
    failed = []

    async def flaky(query):
        data, errors = await original(query)
        if "history(" in query and not failed:
            alias = next(iter(data))
            failed.append(alias)
            data[alias] = None
            errors = errors + [{"path": [alias], "message": "Something went wrong while executing your query."}]
        return data, errors

    gql.graphql = flaky
    result = asyncio.run(graphql.collect_commits(
        [_repo("acme/app")], lambda _r: gql, ["neha"], ["neha@work.com"], scan_all_branches=True, inflight=2
    ))
    commits, rest = result
    assert failed and rest == [], "a blip is retried, not handed to REST"
    assert {c.sha for c in commits} >= {"a1", "a2", "a4", "f1", "f3"}


def test_a_failed_branch_alone_goes_to_rest():
    model = _model()
    gql = FakeGraphQL(model)
    original = gql.graphql

    async def broken_branch(query):
        data, errors = await original(query)
        for line, alias in zip(query.strip().splitlines()[1:-1], sorted(data)):
            if '"big5"' in line or 'refs/heads/huge"' in line:  # the "huge" branch, every way it is read
                data[alias] = None
                errors = errors + [{"type": "FORBIDDEN", "path": [alias], "message": "no"}]
        return data, errors

    gql.graphql = broken_branch
    old_page, old_limit = graphql.PAGE, graphql.COMPARE_MAX_AHEAD
    graphql.PAGE, graphql.COMPARE_MAX_AHEAD = 2, 4
    try:
        commits, rest = asyncio.run(graphql.collect_commits(
            [_repo("acme/app")], lambda _r: gql, ["neha"], ["neha@work.com"], scan_all_branches=True, inflight=2
        ))
    finally:
        graphql.PAGE, graphql.COMPARE_MAX_AHEAD = old_page, old_limit
    assert [(r.full_name, branches) for r, branches in rest] == [("acme/app", ["huge"])]
    shas = {c.sha for c in commits}
    assert {"a1", "a2", "a4", "f1", "f3", "g1"} <= shas, "the rest of the repository is kept"
    assert not shas & {"big1", "big5"}, "the failed branch is left to REST"


def test_compared_branches_keep_only_the_tracked_authors():
    model = _model()
    gql = FakeGraphQL(model)
    walks = []
    real_compare = graphql._compare_walk

    def spy(*args, **kwargs):
        walk = real_compare(*args, **kwargs)
        walks.append(walk)
        return walk

    graphql._compare_walk = spy
    try:
        asyncio.run(graphql.collect_commits(
            [_repo("acme/app")], lambda _r: gql, ["neha"], ["neha@work.com"], scan_all_branches=True, inflight=2
        ))
    finally:
        graphql._compare_walk = real_compare
    held = {node["oid"] for walk in walks for node in walk.nodes}
    assert "f2" not in held and "f1" in held, "other people's commits are dropped as pages arrive"


# ---------------------------------------------------------------------------
# Search and the time limit
# ---------------------------------------------------------------------------


def test_search_keeps_only_what_is_picked():
    class Searcher:
        async def search(self, path, query, *, split_above=None):
            items = [{"repository": {"full_name": f"o/r{i % 3}", "owner": {"x": "y" * 500}}} for i in range(50)]
            return items, 50, False

    from datetime import datetime, timezone

    found = asyncio.run(search_all(
        Searcher(), "/search/commits", "author:neha", "author-date",
        datetime(2020, 1, 1, tzinfo=timezone.utc), datetime(2021, 1, 1, tzinfo=timezone.utc),
        Coverage(), "commits", pick=lambda item: item["repository"]["full_name"],
    ))
    assert found[:3] == ["o/r0", "o/r1", "o/r2"] and len(found) == 50


def test_time_limit_is_one_note_whatever_was_being_read():
    coverage = Coverage()

    async def late():
        raise DeadlineError()

    async def run():
        for label in ("acme/a", "acme/b", "acme/c"):
            assert await _guard(late(), label, coverage) == []

    asyncio.run(run())
    assert coverage.notes == [TIME_LIMIT_NOTE]


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in _all_tests():
        test()
        print("ok ", test.__name__)
