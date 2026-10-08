"""GraphQL collector tests - no network or tokens needed.

One in-memory "GitHub" answers both the GraphQL queries the new collector
sends and the REST calls of the original collectors, so every test can check
that both paths produce the same records - including the fallbacks taken when
a query times out, a repository is forbidden or a comparison is impossible.

Run directly:

    python tests/test_graphql.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib import graphql  # noqa: E402
from github_contrib.client import GitHubClient, GitHubError, GraphQLQueryError  # noqa: E402
from github_contrib.commits import collect_commits_for_repo, collect_pr_commits  # noqa: E402
from github_contrib.models import Coverage, PullRequestRecord, RepoRecord  # noqa: E402


# ---------------------------------------------------------------------------
# A tiny GitHub
# ---------------------------------------------------------------------------


class Model:
    """Repositories with branches (full histories, newest first), commits, users and PRs."""

    def __init__(self) -> None:
        self.users = {"neha": "U_neha", "other": "U_other"}
        self.repos: dict[str, dict] = {}
        self.commits: dict[str, dict] = {}
        self.prs: dict[str, list[dict]] = {}

    def commit(self, sha, login="neha", email="neha@x.com", parents=1, stats=(3, 1, 2)):
        self.commits[sha] = {"login": login, "email": email, "parents": parents, "stats": stats}
        return sha

    def repo(self, full, branches, default="main", orphans=()):
        self.repos[full] = {"default": default, "branches": branches, "orphans": set(orphans)}

    def node(self, sha):
        c = self.commits[sha]
        return {
            "oid": sha,
            "url": f"https://github.com/x/y/commit/{sha}",
            "message": f"change {sha}\n\nbody",
            "authoredDate": "2026-04-21T11:33:34Z",
            "committedDate": "2026-04-22T08:00:00Z",
            "author": {"name": "N", "email": c["email"], "user": {"login": c["login"]} if c["login"] else None},
            "committer": {"name": "GitHub", "email": "noreply@github.com"},
            "parents": {"totalCount": c["parents"]},
        }

    def rest(self, sha):
        c = self.commits[sha]
        return {
            "sha": sha,
            "html_url": f"https://github.com/x/y/commit/{sha}",
            "commit": {
                "message": f"change {sha}\n\nbody",
                "author": {"name": "N", "email": c["email"], "date": "2026-04-21T11:33:34Z"},
                "committer": {"name": "GitHub", "email": "noreply@github.com", "date": "2026-04-22T08:00:00Z"},
            },
            "author": {"login": c["login"]} if c["login"] else None,
            "parents": [{"sha": "p"}] * c["parents"],
        }


def _page(items, first, after):
    start = int(after or 0)
    end = start + first
    return items[start:end], {"hasNextPage": end < len(items), "endCursor": str(end) if end < len(items) else None}


class FakeGraphQL:
    """Answers the collector's GraphQL queries from a :class:`Model`.

    ``fail(query, aliases)`` may raise to fail a whole query; repositories in
    ``forbidden`` answer with a per-alias FORBIDDEN error.
    """

    _parse_next_link = staticmethod(GitHubClient._parse_next_link)

    def __init__(self, model: Model, *, fail=None, forbidden=()) -> None:
        self.model = model
        self.fail = fail
        self.forbidden = set(forbidden)
        self.queries: list[str] = []
        self.rest = FakeRest(model)  # a real client speaks both APIs

    async def request(self, method, path, *, params=None, accept=None):
        return await self.rest.request(method, path, params=params, accept=accept)

    async def graphql(self, query: str):
        self.queries.append(query)
        lines = query.strip().splitlines()[1:-1]
        if self.fail is not None:
            self.fail(query, len(lines))
        data, errors = {}, []
        for line in lines:
            alias, kind, args, body = re.match(r"^(w\d+): (repository|user)\(([^)]*)\) \{ (.*) \}$", line).groups()
            try:
                data[alias] = self._answer(kind, args, body)
            except LookupError as exc:
                data[alias] = None
                errors.append({"type": exc.args[0], "path": [alias], "message": exc.args[1]})
        return data, errors

    def _answer(self, kind, args, body):
        m = self.model
        first = int((re.search(r"first: (\d+)", body) or [0, 0])[1])
        after = (re.search(r'after: "([^"]*)"', body) or [None, None])[1]
        if kind == "user":
            login = json.loads(re.search(r"login: (\".*\")", args)[1])
            if body == "id":
                if login not in m.users:
                    raise LookupError("NOT_FOUND", f"no user {login}")
                return {"id": m.users[login]}
            nodes, info = _page(m.prs.get(login, []), first, after)
            return {"pullRequests": {"totalCount": len(m.prs.get(login, [])), "pageInfo": info, "nodes": nodes}}

        owner, name = re.search(r'owner: "([^"]+)", name: "([^"]+)"', args).groups()
        full = f"{owner}/{name}"
        if full in self.forbidden or full not in m.repos:
            raise LookupError("FORBIDDEN" if full in self.forbidden else "NOT_FOUND", f"{full} unavailable")
        repo = m.repos[full]
        branches = repo["branches"]

        if body.startswith("defaultBranchRef"):
            default = {"name": repo["default"], "target": {"oid": branches[repo["default"]][0]}}
            out = {"defaultBranchRef": default}
            if "refs(" in body:
                refs = [{"name": b, "target": {"oid": h[0]}} for b, h in sorted(branches.items())]
                nodes, info = _page(refs, first, after)
                out["refs"] = {"pageInfo": info, "nodes": nodes}
            return out
        if "changedFilesIfAvailable" in body:
            sha = re.search(r'object\(oid: "([^"]+)"\)', body)[1]
            if sha not in m.commits:
                return {"object": None}
            adds, dels, files = m.commits[sha]["stats"]
            return {"object": {"additions": adds, "deletions": dels, "changedFilesIfAvailable": files}}
        if "history(" in body:
            head = re.search(r'object\(oid: "([^"]+)"\)', body)[1]
            history = next(h for h in branches.values() if h[0] == head)
            ident = re.search(r'author: \{id: "([^"]+)"\}', body)
            emails = re.search(r"author: \{emails: (\[.*?\])\}", body)
            if ident:
                login = next(k for k, v in m.users.items() if v == ident[1])
                matched = [s for s in history if m.commits[s]["login"] == login]
            else:
                wanted = set(json.loads(emails[1]))
                matched = [s for s in history if m.commits[s]["email"] in wanted]
            nodes, info = _page([m.node(s) for s in matched], first, after)
            return {"object": {"history": {"pageInfo": info, "nodes": nodes}}}
        if "compare(" in body:
            base = re.search(r'qualifiedName: "refs/heads/([^"]+)"', body)[1]
            head = re.search(r'headRef: "refs/heads/([^"]+)"', body)[1]
            if head in repo["orphans"]:
                raise LookupError("UNPROCESSABLE", "no common ancestor")
            on_base = set(branches[base])
            ahead = [s for s in reversed(branches[head]) if s not in on_base]
            nodes, info = _page([m.node(s) for s in ahead], first, after)
            return {"ref": {"compare": {"commits": {"totalCount": len(ahead), "pageInfo": info, "nodes": nodes}}}}
        if "pullRequest(number" in body:
            number = int(re.search(r"pullRequest\(number: (\d+)\)", body)[1])
            pr = next(p for prs in m.prs.values() for p in prs if p["number"] == number)
            nodes, info = _page(pr["_all_commits"], first, after)
            return {"pullRequest": {"commits": {"pageInfo": info, "nodes": nodes}}}
        raise AssertionError(f"unexpected query body: {body[:80]}")


class FakeRest:
    """The REST calls of the original collectors, from the same :class:`Model`."""

    _parse_next_link = staticmethod(GitHubClient._parse_next_link)

    def __init__(self, model: Model) -> None:
        self.model = model
        self.paths: list[str] = []

    async def paginate(self, path, *, params=None, accept=None, max_items=None):
        self.paths.append(path)
        full = "/".join(path.split("/")[2:4])
        repo = self.model.repos[full]
        if path.endswith("/branches"):
            for name, history in repo["branches"].items():
                yield {"name": name, "commit": {"sha": history[0]}}
        elif path.endswith("/commits") and "/pulls/" not in path:
            author = params["author"]
            for sha in repo["branches"][params["sha"]]:
                c = self.model.commits[sha]
                if author in (c["login"], c["email"]):
                    yield self.model.rest(sha)
        elif "/pulls/" in path:
            number = int(path.split("/")[-2])
            pr = next(p for prs in self.model.prs.values() for p in prs if p["number"] == number)
            for node in pr["_all_commits"]:
                yield self.model.rest(node["commit"]["oid"])

    async def request(self, method, path, *, params=None, accept=None):
        self.paths.append(path)
        sha = path.rsplit("/", 1)[1]
        adds, dels, files = self.model.commits[sha]["stats"]
        return {"stats": {"additions": adds, "deletions": dels}, "files": [{}] * files}, {}, 200


def _repo(full, *, affiliated=True, default="main"):
    owner, name = full.split("/")
    return RepoRecord(full_name=full, name=name, owner=owner, organization=owner,
                      default_branch=default, affiliated=affiliated)


def _model() -> Model:
    """A company repo with every branch shape, plus an upstream project."""
    m = Model()
    for sha in ("a1", "a2", "a4", "f1", "f3", "big1", "big2", "big3", "big4", "big5", "g1", "u1"):
        m.commit(sha)
    m.commit("a3", login="other", email="other@x.com")
    m.commit("f2", login="other", email="other@x.com")
    m.commit("e1", login=None, email="neha@work.com")  # email not linked to the account
    m.commit("m1", parents=2)  # a merge by neha
    main = ["m1", "a4", "a3", "a2", "a1"]
    m.repo("acme/app", {
        "main": main,
        "feature": ["f3", "f2", "f1", "e1", "a2", "a1"],     # forked from a2
        "feature-copy": ["f3", "f2", "f1", "e1", "a2", "a1"],  # same head: not read twice
        "merged": ["a4", "a3", "a2", "a1"],                   # already on main
        "zeta": ["f1", "e1", "a2", "a1"],                     # shares commits with feature
        "huge": ["big5", "big4", "big3", "big2", "big1", "a1"],  # too far ahead to compare
        "gh-pages": ["g1"],                                   # no history in common
    }, orphans=("gh-pages",))
    m.repo("up/stream", {"main": ["u1"], "dev": ["u1"]})
    return m


def _keys(commits):
    return sorted((c.full_name, c.sha, c.branch, c.author_login, c.parent_count) for c in commits)


async def _collect_both(model, **fake):
    old_page, old_limit = graphql.PAGE, graphql.COMPARE_MAX_AHEAD
    # Tiny pages force pagination; "huge" (5 ahead) overflows, "feature" (4) does not.
    graphql.PAGE, graphql.COMPARE_MAX_AHEAD = 2, 4
    try:
        gql = FakeGraphQL(model, **fake)
        repos = [_repo("acme/app"), _repo("up/stream", affiliated=False)]
        result = await graphql.collect_commits(
            repos, lambda _r: gql, ["neha"], ["neha@work.com"], scan_all_branches=True, inflight=3
        )
    finally:
        graphql.PAGE, graphql.COMPARE_MAX_AHEAD = old_page, old_limit
    rest_commits = []
    for repo in repos:
        rest_commits += await collect_commits_for_repo(
            FakeRest(model), repo, ["neha"], scan_all_branches=True, author_emails=["neha@work.com"],
            coverage=Coverage(),
        )
    return gql, result, rest_commits


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_commits_match_the_rest_collector():
    gql, (commits, rest_repos), rest_commits = asyncio.run(_collect_both(_model()))
    assert rest_repos == []
    assert _keys(commits) == _keys(rest_commits)
    by_sha = {c.sha: c for c in commits if c.full_name == "acme/app"}
    assert by_sha["a2"].branch == "main", "default branch wins"
    assert by_sha["f1"].branch == "feature", "first branch by name"
    assert by_sha["e1"].author_login == "neha", "unlinked email credited to the tracked login"
    assert by_sha["g1"].branch == "gh-pages" and by_sha["big3"].branch == "huge"
    assert "f2" not in by_sha and "a3" not in by_sha, "other people's commits are left out"
    assert {c.sha for c in commits if c.full_name == "up/stream"} == {"u1"}
    lines = [line for q in gql.queries for line in q.splitlines()]
    compared = {re.search(r'headRef: "refs/heads/([^"]+)"', line)[1] for line in lines if "compare(" in line}
    assert compared == {"feature", "gh-pages", "huge", "merged", "zeta"}, compared  # feature-copy: same head
    walked = {line for line in lines if "history(" in line and 'object(oid: "big5")' in line}
    assert len(walked) >= 2, "the overflowing branch is read once per author"
    assert not any('owner: "up"' in line and "refs(" in line for line in lines), "upstream: default branch only"
    print("ok  test_commits_match_the_rest_collector")


def test_timeouts_split_batches_and_shrink_pages():
    def slow(query, aliases):
        if aliases > 1 or "first: 2," in query or "first: 2)" in query:
            raise GraphQLQueryError("GraphQL query failed: timeout", status=502)

    old_min = graphql.MIN_PAGE
    graphql.MIN_PAGE = 1
    try:
        gql, (commits, rest_repos), rest_commits = asyncio.run(_collect_both(_model(), fail=slow))
    finally:
        graphql.MIN_PAGE = old_min
    assert rest_repos == [] and _keys(commits) == _keys(rest_commits)
    assert any("first: 1" in q for q in gql.queries), "pages were shrunk"
    print("ok  test_timeouts_split_batches_and_shrink_pages")


def test_unreadable_repositories_are_handed_to_rest():
    _gql, (commits, rest_repos), _rest = asyncio.run(_collect_both(_model(), forbidden={"acme/app"}))
    assert [r.full_name for r in rest_repos] == ["acme/app"]
    assert {c.full_name for c in commits} == {"up/stream"}

    async def unknown_user():
        model = _model()
        return await graphql.collect_commits(
            [_repo("acme/app")], lambda _r: FakeGraphQL(model), ["ghost"], [], scan_all_branches=True, inflight=2
        )

    assert asyncio.run(unknown_user()) is None, "no GraphQL user id: the caller uses REST for everything"
    print("ok  test_unreadable_repositories_are_handed_to_rest")


def _pr_node(model, number, repo, commit_shas, *, merged=True):
    owner, name = repo.split("/")
    commits = [{"commit": model.node(s)} for s in commit_shas]
    node = {
        "number": number, "title": f"PR {number}", "state": "MERGED" if merged else "OPEN",
        "createdAt": "2026-04-20T10:00:00Z", "updatedAt": "2026-04-23T10:00:00Z",
        "closedAt": "2026-04-23T10:00:00Z" if merged else None,
        "mergedAt": "2026-04-23T10:00:00Z" if merged else None,
        "url": f"https://github.com/{repo}/pull/{number}", "baseRefName": "main", "headRefName": f"feat-{number}",
        "mergeCommit": {"oid": f"merge{number}"} if merged else None, "author": {"login": "neha"},
        "repository": {
            "nameWithOwner": repo, "name": name, "owner": {"login": owner, "__typename": "Organization"},
            "isPrivate": True, "isFork": False, "isArchived": False, "defaultBranchRef": {"name": "main"},
            "url": f"https://github.com/{repo}", "description": "", "primaryLanguage": {"name": "Dart"},
            "stargazerCount": 0, "forkCount": 0, "pushedAt": "2026-04-23T10:00:00Z", "createdAt": "2025-01-01T00:00:00Z",
        },
        "commits": {"totalCount": len(commits), "pageInfo": {}, "nodes": commits},
        "_all_commits": commits,
    }
    return node


def test_pull_requests_and_their_commits():
    model = _model()
    for sha in ("p1", "p3"):
        model.commit(sha)
    model.commit("p2", login="other", email="other@x.com")
    big = _pr_node(model, 7, "acme/app", ["p1", "p2", "p3"])
    # Only the first 2 commits come inline: the rest needs a follow-up page.
    big["commits"] = {"totalCount": 3, "pageInfo": {"hasNextPage": True, "endCursor": "2"},
                      "nodes": big["_all_commits"][:2]}
    open_pr = _pr_node(model, 8, "corp/site", [], merged=False)
    model.prs["neha"] = [big, open_pr]
    model.repo("corp/site", {"main": ["a1"]})

    gql = FakeGraphQL(model)
    data = asyncio.run(graphql.collect_pull_requests({"neha": gql, "neha2": gql}, ["neha"], inflight=2))
    assert data is not None and [(p.full_name, p.number) for p in data.prs] == [("acme/app", 7), ("corp/site", 8)]
    pr7, pr8 = data.prs
    assert (pr7.state, pr7.merged, pr7.merge_commit_sha, pr7.head_branch) == ("closed", True, "merge7", "feat-7")
    assert (pr8.state, pr8.merged, pr8.merge_commit_sha) == ("open", False, "")
    site = data.repos["corp/site"]
    assert site.organization == "corp" and site.is_private and not site.affiliated
    assert site.discovered_via == {"neha", "neha2"}, "every token that sees the PR can read its repo"

    coverage = Coverage()
    shas, records = data.commits_of(pr7, _repo("acme/app"), ["neha"], [], coverage)
    rest_shas, rest_records = asyncio.run(collect_pr_commits(
        FakeRest(model), _repo("acme/app"), pr7, ["neha"], [], Coverage()
    ))
    assert shas == rest_shas == ["p1", "p2", "p3"]
    assert _keys(records) == _keys(rest_records) and {r.sha for r in records} == {"p1", "p3"}
    assert coverage.notes == []

    async def broken():
        def fail(query, _aliases):
            if "pullRequests(" in query:
                raise GitHubError("Forbidden", status=403)
        return await graphql.collect_pull_requests({"neha": FakeGraphQL(model, fail=fail)}, ["neha"], inflight=1)

    assert asyncio.run(broken()) is None, "an unreadable list hands pull requests back to REST"
    print("ok  test_pull_requests_and_their_commits")


def test_line_stats_in_batches_with_rest_fallback():
    model = _model()
    model.commit("s1", stats=(10, 4, 3))
    model.commit("s2", stats=(5, 0, 1))
    model.commit("mx", parents=2)
    gql = FakeGraphQL(model)
    rest = gql.rest

    async def run():
        from tests.test_accuracy import _commit  # the shared CommitRecord factory

        commits = [_commit("s1"), _commit("s1", repo="neha/fork"), _commit("s2"), _commit("mx", parents=2)]

        original = gql.graphql

        async def no_file_count(query):
            data, errors = await original(query)
            for value in data.values():  # GitHub could not count s2's files
                obj = (value or {}).get("object") or {}
                if obj.get("additions") == 5:
                    obj["changedFilesIfAvailable"] = None
            return data, errors

        gql.graphql = no_file_count
        coverage = Coverage()
        await graphql.enrich_commits_with_stats({"acme/app": gql}, commits, coverage, inflight=2)
        return commits, coverage

    old = dict(graphql.BATCH)
    graphql.BATCH["stats"] = 2
    try:
        commits, coverage = asyncio.run(run())
    finally:
        graphql.BATCH.update(old)
    s1, s1_fork, s2, merge = commits
    assert (s1.additions, s1.deletions, s1.files_changed, s1.stats_fetched) == (10, 4, 3, True)
    assert (s1_fork.additions, s1_fork.stats_fetched) == (10, True), "same SHA shares its stats"
    assert (s2.additions, s2.files_changed, s2.stats_fetched) == (5, 1, True), "measured through REST"
    assert merge.stats_fetched is False
    assert rest.paths == ["/repos/acme/app/commits/s2"], rest.paths
    assert sum("changedFilesIfAvailable" in q for q in gql.queries) == 1, "s1 and s2 share a query"
    assert coverage.notes == []
    print("ok  test_line_stats_in_batches_with_rest_fallback")


def test_client_graphql_waits_out_rate_limits():
    calls = []

    async def fake_request(method, path, *, params=None, accept=None, json_body=None, retries=None):
        calls.append((method, path, retries))
        if len(calls) == 1:
            return {"data": None, "errors": [{"type": "RATE_LIMITED", "message": "slow down"}]}, {
                "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "0"}, 200
        if len(calls) == 2:
            return {"data": {"w0": {"id": "U1"}}, "errors": [{"path": ["w1"], "type": "NOT_FOUND"}]}, {}, 200
        return {"data": None, "errors": [{"message": "Something went wrong (timeout)"}]}, {}, 200

    async def run():
        client = GitHubClient(token="t", session=None, semaphore=asyncio.Semaphore(1), max_retries=2)
        client.request = fake_request
        slept = []

        async def no_sleep(seconds):
            slept.append(seconds)

        real_sleep, asyncio.sleep = asyncio.sleep, no_sleep
        try:
            data, errors = await client.graphql("query { w0: user(login: \"a\") { id } }")
            try:
                await client.graphql("query { }")
                raise AssertionError("a failed query must raise")
            except GraphQLQueryError as exc:
                assert "timeout" in str(exc)
        finally:
            asyncio.sleep = real_sleep
        return client, data, errors, slept

    client, data, errors, slept = asyncio.run(run())
    assert data == {"w0": {"id": "U1"}} and errors[0]["path"] == ["w1"]
    assert slept and client.rate_limit_waits == 1 and client.graphql_count == 3
    assert all(c == ("POST", "/graphql", 0) for c in calls), "timeouts are split by the caller, not retried"
    print("ok  test_client_graphql_waits_out_rate_limits")


def _all_tests():
    return [obj for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]


def main() -> int:
    failures = 0
    for test in _all_tests():
        try:
            test()
        except Exception:  # noqa: BLE001
            import traceback

            failures += 1
            print(f"FAIL {test.__name__}")
            traceback.print_exc()
    print(f"\n{len(_all_tests()) - failures} passed, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
