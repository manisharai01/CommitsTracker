"""GraphQL collection: the REST collectors' commits, pull requests and line
statistics in a fraction of the requests.

The REST path costs one request per page of (repository x branch x author)
plus one per commit for line statistics: thousands of requests for an active
developer, more than GitHub's 5,000 per hour, so a run spent most of its time
asleep waiting for the budget to reset. Here:

* many repositories, branches or commits share one query (aliases);
* every branch other than the default one is *compared* with the default
  branch, so only the commits that exist on that branch alone are read,
  instead of re-reading its whole history once per author;
* pull requests come from each user's own pull-request list, with their
  commits, instead of listing every pull request of every repository;
* line statistics are read 25 commits per query.

Each query costs one point of the separate 5,000-point GraphQL budget.
GitHub resolves the aliases of a query one after another and gives up after
about ten seconds, so slow work gets small batches, and a query that fails as
a whole is split (then its page shrunk) and retried. Whatever still cannot be
read through GraphQL is handed back to be collected with the REST collectors,
which also record the data-completeness notes: accuracy never depends on this
module.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .client import DeadlineError, GitHubClient, GitHubError, RateLimitedError
from .commits import PR_COMMITS_LIMIT, _authored_by, _commit_from_payload
from .commits import enrich_commits_with_stats as enrich_commits_with_stats_rest
from .logging_config import get_logger
from .models import CommitRecord, Coverage, PullRequestRecord, RepoRecord, optional_int, parse_github_datetime

log = get_logger("graphql")

#: Items per page of a connection (GitHub's maximum).
PAGE = 100
#: Pull requests per page: each brings up to 100 of its commits along.
PR_PAGE = 50
#: A page that keeps timing out is halved down to this size before giving up.
MIN_PAGE = 10
#: A walk gives up after this many failed pages in a row. Each failure waits
#: (RETRY_PAUSE seconds, doubling); from the second one in a row the page is
#: also halved (down to MIN_PAGE). A success resets the count, so a long walk
#: survives the occasional blip instead of failing on its fifth over hours.
MAX_FAILURES_IN_A_ROW = 6
RETRY_PAUSE = 1.0
#: After this many good pages in a row a halved page doubles again (a page
#: halved by two blips in a row needn't stay small for the rest of the walk).
GROW_AFTER = 20
#: GraphQL error types that retrying can't fix.
_PERMANENT_ERRORS = frozenset({"NOT_FOUND", "FORBIDDEN", "UNAUTHORIZED", "UNPROCESSABLE"})
#: Aliases per query, by kind of work. Walking a long history for one author
#: is the slow part, so those batches are small.
BATCH: dict[str, int] = {
    "users": 20,
    "heads": 20,
    "history": 4,
    "compare": 6,
    "pr_commits": 4,
    "prs": 2,
    "stats": 25,
}
#: A branch further ahead of the default branch than this is read with
#: per-author history walks instead of listing every commit it adds.
COMPARE_MAX_AHEAD = 1000

COMMIT_FIELDS = (
    "oid url message authoredDate committedDate "
    "author { name email user { login } } committer { name email } parents { totalCount }"
)
REPO_FIELDS = (
    "nameWithOwner name owner { login __typename } isPrivate isFork isArchived "
    "defaultBranchRef { name } url description primaryLanguage { name } "
    "stargazerCount forkCount pushedAt createdAt"
)
_PAGE_INFO = "pageInfo { hasNextPage endCursor }"


# ---------------------------------------------------------------------------
# Paging engine
# ---------------------------------------------------------------------------


class _Missing(Exception):
    """Something a query relies on is gone (e.g. the default branch was renamed)."""


@dataclass(eq=False)
class Walk:
    """Reads one connection page by page (or a single object) under an alias.

    ``root`` is the top-level field (``repository(...)`` / ``user(...)``),
    ``body(page_size, after)`` the selection inside it, and ``path`` picks the
    connection out of the root object: ``None`` means "nothing there", and
    raising :class:`_Missing` marks the walk as failed.
    """

    client: GitHubClient
    root: str
    body: Callable[[int, str], str]
    path: Callable[[dict], dict | None]
    page_size: int = field(default_factory=lambda: PAGE)
    cursor: str | None = None
    # Stop after the first page when the connection holds more than this.
    limit: int | None = None
    # Applied to each node as it arrives: what to keep of it (None = drop it),
    # so a walk never holds what its caller would throw away.
    convert: Callable[[dict], dict | None] | None = None
    nodes: list[dict] = field(default_factory=list)
    total: int | None = None
    root_data: dict | None = None  # the root object of the first page
    overflow: bool = False
    done: bool = False
    error: str = ""
    # Errors of single nodes (e.g. one pull request hidden by SAML SSO): the
    # rest of the page was read; the caller notes what is missing.
    skipped: list[str] = field(default_factory=list)
    failures: int = 0  # failed pages in a row
    successes: int = 0  # good pages in a row since the page was last resized
    full_page: int = 0  # the page size to grow back to

    def __post_init__(self) -> None:
        self.full_page = self.page_size

    @property
    def finished(self) -> bool:
        return self.done or bool(self.error)

    def failed_page(self, reason: str) -> float | None:
        """Count a failed page: the pause before trying it again (halving the
        page from the second failure in a row), or None once the walk has
        failed MAX_FAILURES_IN_A_ROW times in a row (``error`` is then set)."""
        self.failures += 1
        self.successes = 0
        if self.failures > MAX_FAILURES_IN_A_ROW:
            self.error = reason
            return None
        if self.failures >= 2 and self.page_size > MIN_PAGE:
            self.page_size = max(MIN_PAGE, self.page_size // 2)
        return min(RETRY_PAUSE * 2 ** (self.failures - 1), 30.0) * random.uniform(1.0, 1.25)

    def good_page(self) -> None:
        self.failures = 0
        self.successes += 1
        if self.successes >= GROW_AFTER and self.page_size < self.full_page:
            self.page_size = min(self.full_page, self.page_size * 2)
            self.successes = 0


def _str(value: str) -> str:
    """A GraphQL string literal (JSON's escapes are valid GraphQL escapes)."""
    return json.dumps(value)


def _repo_root(full_name: str) -> str:
    owner, _, name = full_name.partition("/")
    return f"repository(owner: {_str(owner)}, name: {_str(name)})"


def _after(cursor: str | None) -> str:
    return f", after: {_str(cursor)}" if cursor else ""


def _single(obj: dict) -> dict:
    """``path`` for a walk that reads one object, not a connection."""
    return {"nodes": [obj]}


def _error_text(error: dict) -> str:
    kind = error.get("type")
    message = " ".join(str(error.get("message") or "").split())[:200]
    return f"{kind}: {message}" if kind else message or "unknown GraphQL error"


def _take_batch(queue: deque[Walk], size: int) -> list[Walk]:
    """Up to ``size`` queued walks that use the same client (one token per query)."""
    first = queue.popleft()
    batch = [first]
    skipped: list[Walk] = []
    while queue and len(batch) < size:
        walk = queue.popleft()
        (batch if walk.client is first.client else skipped).append(walk)
    queue.extendleft(reversed(skipped))
    return batch


def _in_list(path: list) -> bool:
    """Whether an error ``path`` points into one element of a list (a single
    node), rather than at the object or connection a walk reads."""
    return any(isinstance(part, int) for part in path[1:])


async def _pause(walk: Walk, seconds: float | None) -> None:
    """Wait before ``walk`` is tried again (not past the run's deadline)."""
    if not seconds:
        return
    try:
        await getattr(walk.client, "_sleep", asyncio.sleep)(seconds)
    except DeadlineError as exc:
        walk.error = str(exc)


async def _run_batch(batch: list[Walk]) -> list[Walk]:
    """Fetch the next page of every walk in ``batch`` with one query. Never raises.

    A failed query is split and retried; a single walk that keeps failing is
    retried after a pause, then with smaller pages, and only fails after
    MAX_FAILURES_IN_A_ROW failures in a row. Rate limits and the deadline are
    the client's business: splitting would only multiply refused requests.
    """
    aliases = [f"w{i}" for i in range(len(batch))]
    query = "query {\n" + "\n".join(
        f"{alias}: {walk.root} {{ {walk.body(walk.page_size, _after(walk.cursor))} }}"
        for alias, walk in zip(aliases, batch)
    ) + "\n}"
    try:
        data, errors = await batch[0].client.graphql(query)
    except GitHubError as exc:
        if isinstance(exc, (RateLimitedError, DeadlineError)) or exc.status in (401, 403):
            for walk in batch:  # no use splitting: every part fails alike
                walk.error = str(exc)
        elif len(batch) > 1:
            middle = len(batch) // 2
            await asyncio.gather(_run_batch(batch[:middle]), _run_batch(batch[middle:]))
        else:
            await _pause(batch[0], batch[0].failed_page(str(exc)))
        return batch
    except Exception as exc:  # noqa: BLE001 - one bad query must not end the run
        for walk in batch:
            walk.error = f"{type(exc).__name__}: {exc}"
        return batch

    by_alias: dict[str, dict] = {}
    by_node: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for error in errors:
        where = error.get("path")
        if isinstance(where, list) and where and isinstance(where[0], str):
            if _in_list(where):
                by_node.setdefault(where[0], []).append(error)
            else:
                by_alias.setdefault(where[0], error)
        else:
            loose.append(error)

    pauses: list[float] = []
    for alias, walk in zip(aliases, batch):
        obj = data.get(alias)
        error = by_alias.get(alias)
        if error is not None or not isinstance(obj, dict):
            if error is None and not loose:
                walk.error = "no data returned"
            elif error is not None and error.get("type") in _PERMANENT_ERRORS:
                walk.error = _error_text(error)
            else:  # "Something went wrong…", a timeout, an internal error: try again
                pause = walk.failed_page(_error_text(error if error is not None else loose[0]))
                if pause is not None:
                    pauses.append(pause)
            continue
        try:
            conn = walk.path(obj)
        except _Missing as exc:
            walk.error = str(exc)
            continue
        if walk.root_data is None:
            walk.root_data = obj
        walk.good_page()
        # One unreadable node (an SSO-protected pull request, a deleted
        # author...) is noted and skipped; the rest of the page counts.
        walk.skipped.extend(_error_text(e) for e in by_node.get(alias, []))
        if conn is None:
            walk.done = True
            continue
        if walk.total is None and isinstance(conn.get("totalCount"), int):
            walk.total = conn["totalCount"]
        if walk.limit is not None and walk.total is not None and walk.total > walk.limit:
            walk.overflow = walk.done = True
            continue
        for node in conn.get("nodes") or []:
            if isinstance(node, dict):
                kept = walk.convert(node) if walk.convert is not None else node
                if kept is not None:
                    walk.nodes.append(kept)
        info = conn.get("pageInfo") or {}
        cursor = info.get("endCursor")
        if info.get("hasNextPage") and cursor:
            if cursor == walk.cursor:
                walk.error = "pagination did not advance"
            walk.cursor = str(cursor)
        else:
            walk.done = True
    if pauses:
        await _pause(batch[0], max(pauses))
    return batch


def note_skipped(walks: list[Walk], coverage: Coverage | None, what: str) -> None:
    """One completeness note for the nodes ``walks`` couldn't read."""
    reasons = [reason for walk in walks for reason in walk.skipped]
    if reasons and coverage is not None:
        coverage.warn(
            f"{len(reasons)} {what} could not be read and are missing ({reasons[0]})."
        )


async def run_walks(
    walks: list[Walk], batch_size: int, *, inflight: int, progress: _Progress | None = None,
    after_batch: Callable[[], None] | None = None,
) -> None:
    """Run every walk to the end (or to an error), keeping ``inflight`` queries
    busy. ``after_batch`` runs after each answered query (no walk is being
    updated then): collect_commits moves what was read to disk there."""
    queue: deque[Walk] = deque()
    for walk in walks:
        if walk.finished:
            if progress is not None:
                progress.update()
        else:
            queue.append(walk)
    running: set[asyncio.Task] = set()
    while queue or running:
        while queue and len(running) < max(1, inflight):
            running.add(asyncio.create_task(_run_batch(_take_batch(queue, batch_size))))
        finished, running = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for task in finished:
            for walk in task.result():
                if not walk.finished:
                    queue.append(walk)
                elif progress is not None:
                    progress.update()
        if after_batch is not None:
            after_batch()


class _Progress:
    """A tqdm bar the web UI reads ("Commits: 45%|...| 90/200"), or nothing."""

    def __init__(self, desc: str, total: int) -> None:
        try:
            from tqdm import tqdm  # type: ignore import-not-found

            self.bar = tqdm(total=total, desc=desc, unit="item")
        except Exception:  # pragma: no cover - tqdm is a hard dependency in practice
            self.bar = None

    def add(self, count: int) -> None:
        if self.bar is not None and count:
            self.bar.total += count
            self.bar.refresh()

    def update(self, count: int = 1) -> None:
        if self.bar is not None:
            self.bar.update(count)

    def __enter__(self) -> _Progress:
        return self

    def __exit__(self, *_exc: object) -> None:
        if self.bar is not None:
            self.bar.close()


# ---------------------------------------------------------------------------
# Payload conversion (GraphQL -> the REST shapes the collectors understand)
# ---------------------------------------------------------------------------


def rest_commit(node: dict[str, Any]) -> dict[str, Any]:
    """A GraphQL ``Commit`` as a REST commit payload."""
    author = node.get("author") or {}
    committer = node.get("committer") or {}
    login = (author.get("user") or {}).get("login")
    return {
        "sha": str(node.get("oid") or ""),
        "html_url": str(node.get("url") or ""),
        "commit": {
            "message": str(node.get("message") or ""),
            "author": {"name": author.get("name"), "email": author.get("email"), "date": node.get("authoredDate")},
            "committer": {
                "name": committer.get("name"),
                "email": committer.get("email"),
                "date": node.get("committedDate"),
            },
        },
        "author": {"login": login} if login else None,
        "parents": [{}] * int((node.get("parents") or {}).get("totalCount") or 0),
    }


def repo_record(node: dict[str, Any], discovered_by: str) -> RepoRecord:
    """A repository found through GraphQL (an upstream project: not affiliated)."""
    owner = node.get("owner") or {}
    owner_login = str(owner.get("login") or "")
    return RepoRecord(
        full_name=str(node.get("nameWithOwner") or ""),
        name=str(node.get("name") or ""),
        owner=owner_login,
        organization=owner_login if owner.get("__typename") == "Organization" else "",
        is_private=bool(node.get("isPrivate")),
        is_fork=bool(node.get("isFork")),
        is_archived=bool(node.get("isArchived")),
        default_branch=str((node.get("defaultBranchRef") or {}).get("name") or "main"),
        html_url=str(node.get("url") or ""),
        description=str(node.get("description") or ""),
        language=str((node.get("primaryLanguage") or {}).get("name") or ""),
        stargazers=int(node.get("stargazerCount") or 0),
        forks=int(node.get("forkCount") or 0),
        pushed_at=parse_github_datetime(node.get("pushedAt")),
        created_at=parse_github_datetime(node.get("createdAt")),
        discovered_via={discovered_by},
        affiliated=False,
    )


def pr_record(node: dict[str, Any], repo: RepoRecord) -> PullRequestRecord:
    """A GraphQL ``PullRequest`` as the record the REST collector produces."""
    merged_at = parse_github_datetime(node.get("mergedAt"))
    return PullRequestRecord(
        repository=repo.name,
        full_name=repo.full_name,
        organization=repo.organization,
        number=int(node.get("number") or 0),
        title=str(node.get("title") or ""),
        author_login=str((node.get("author") or {}).get("login") or ""),
        state="open" if str(node.get("state") or "").upper() == "OPEN" else "closed",
        merged=merged_at is not None,
        created_at=parse_github_datetime(node.get("createdAt")),
        updated_at=parse_github_datetime(node.get("updatedAt")),
        closed_at=parse_github_datetime(node.get("closedAt")),
        merged_at=merged_at,
        base_branch=str(node.get("baseRefName") or ""),
        head_branch=str(node.get("headRefName") or ""),
        url=str(node.get("url") or ""),
        merge_commit_sha=str((node.get("mergeCommit") or {}).get("oid") or "") if merged_at else "",
        additions=optional_int(node.get("additions")),
        deletions=optional_int(node.get("deletions")),
        changed_files=optional_int(node.get("changedFiles")),
    )


# ---------------------------------------------------------------------------
# Commits
# ---------------------------------------------------------------------------


def _user_walk(client: GitHubClient, login: str) -> Walk:
    return Walk(client, f"user(login: {_str(login)})", lambda _size, _after: "id", _single)


def _heads_walk(client: GitHubClient, repo: RepoRecord, all_branches: bool) -> Walk:
    def body(size: int, after: str) -> str:
        text = "defaultBranchRef { name target { oid } }"
        if all_branches:
            text += (
                f' refs(refPrefix: "refs/heads/", first: {size}{after}) '
                f"{{ {_PAGE_INFO} nodes {{ name target {{ oid }} }} }}"
            )
        return text

    def path(obj: dict) -> dict | None:
        return obj.get("refs") if all_branches else {"nodes": []}

    return Walk(client, _repo_root(repo.full_name), body, path)


def _history_walk(
    client: GitHubClient, full_name: str, head: str, author: str, since: str = "",
    convert: Callable[[dict], dict | None] | None = None,
) -> Walk:
    """Commits by ``author`` (a GraphQL CommitAuthor) reachable from ``head``
    (committed from ``since`` on, an ISO time, when given)."""
    since_arg = f", since: {_str(since)}" if since else ""

    def body(size: int, after: str) -> str:
        return (
            f"object(oid: {_str(head)}) {{ ... on Commit {{ "
            f"history(first: {size}{after}, author: {author}{since_arg}) "
            f"{{ {_PAGE_INFO} nodes {{ {COMMIT_FIELDS} }} }} }} }}"
        )

    def path(obj: dict) -> dict | None:
        target = obj.get("object")
        if not isinstance(target, dict):
            raise _Missing(f"commit {head[:7]} could not be read")
        return target.get("history")

    return Walk(client, _repo_root(full_name), body, path, convert=convert)


def node_author_filter(logins: set[str], emails: set[str]) -> Callable[[dict], dict | None]:
    """``Walk.convert`` keeping only commit nodes by ``logins`` / ``emails``
    (lower case): a compared branch lists everyone's commits."""

    def keep(node: dict) -> dict | None:
        author = node.get("author") or {}
        login = str((author.get("user") or {}).get("login") or "").lower()
        email = str(author.get("email") or "").lower()
        return node if login in logins or (email and email in emails) else None

    return keep


def _compare_walk(
    client: GitHubClient, full_name: str, base: str, branch: str,
    keep: Callable[[dict], dict | None] | None = None,
) -> Walk:
    """Every commit on ``branch`` that is not on ``base`` (by the tracked
    authors only, with ``keep``)."""

    def body(size: int, after: str) -> str:
        return (
            f"ref(qualifiedName: {_str('refs/heads/' + base)}) {{ "
            f"compare(headRef: {_str('refs/heads/' + branch)}) {{ "
            f"commits(first: {size}{after}) {{ totalCount {_PAGE_INFO} nodes {{ {COMMIT_FIELDS} }} }} }} }}"
        )

    def path(obj: dict) -> dict | None:
        compare = (obj.get("ref") or {}).get("compare")
        if not isinstance(compare, dict):
            raise _Missing(f"branch '{branch}' could not be compared with '{base}'")
        return compare.get("commits")

    return Walk(client, _repo_root(full_name), body, path, limit=COMPARE_MAX_AHEAD, convert=keep)


@dataclass(eq=False)
class _Scan:
    """One source of a repository's commits, in attribution order."""

    branch: str
    walk: Walk
    fallback_login: str
    # History walks are filtered by author on GitHub; compared commits are not.
    by_author: bool
    head: str = ""  # head commit of a compared branch
    # Set when a comparison was too large or failed: per-author history walks.
    replaced_by: list[_Scan] | None = None
    # Where its commits rank when one SHA is found by several scans: lower
    # wins (the default branch first, then branches by name).
    priority: int = 0

    def leaves(self) -> list[_Scan]:
        return self.replaced_by if self.replaced_by is not None else [self]


@dataclass(eq=False)
class _RepoPlan:
    repo: RepoRecord
    client: GitHubClient
    all_branches: bool
    heads: Walk = field(init=False)
    scans: list[_Scan] = field(default_factory=list)
    # Why the repository couldn't be planned through GraphQL at all.
    unplanned: str = ""
    since: str = ""  # history walks start here (an ISO time; "" = all history)
    # Where read commits go as they pile up (collect_commits with a store).
    store: Any = None

    def __post_init__(self) -> None:
        self.heads = _heads_walk(self.client, self.repo, self.all_branches)

    def plan(
        self, filters: list[tuple[str, str]], primary: str,
        keep: Callable[[dict], dict | None] | None = None, since: str = "",
    ) -> None:
        """Default branch walks per author, then one comparison per other branch."""
        self.since = since
        default_ref = (self.heads.root_data or {}).get("defaultBranchRef")
        heads: dict[str, str] = {}
        for node in self.heads.nodes:
            name, oid = node.get("name"), (node.get("target") or {}).get("oid")
            if name and oid:
                heads[str(name)] = str(oid)
        if not isinstance(default_ref, dict):
            if heads:  # branches but no default one: let the REST collector read them
                self.unplanned = "the repository has branches but no default branch"
            return  # else an empty repository
        default = str(default_ref.get("name") or "")
        default_head = str((default_ref.get("target") or {}).get("oid") or "")
        if not default or not default_head:
            return
        for login, author in filters:
            walk = _history_walk(self.client, self.repo.full_name, default_head, author, since)
            self.scans.append(_Scan(default, walk, login, by_author=True, priority=len(self.scans) * 1000))

        seen = {default_head}
        for name in sorted(heads):
            if name == default or heads[name] in seen:
                continue  # same head as a branch already scanned: same history
            seen.add(heads[name])
            walk = _compare_walk(self.client, self.repo.full_name, default, name, keep)
            self.scans.append(
                _Scan(name, walk, primary, by_author=False, head=heads[name], priority=len(self.scans) * 1000)
            )

    def replace_failed_compares(self, filters: list[tuple[str, str]]) -> list[Walk]:
        """Per-author history walks for branches whose comparison overflowed or
        failed (e.g. a branch with no history in common, like gh-pages).

        Those walks re-read history the plan may already hold (every branch
        far ahead of a stale default branch shares most of it): a commit
        already read is dropped as it arrives, so memory holds each commit once."""
        store = self.store
        for scan in self.scans:  # what a failed comparison read is read again
            if not scan.by_author and scan.walk.error:
                scan.walk.nodes = []
                if store is not None:
                    store.delete_repo(self.repo.full_name, scan.priority)
        self.known = {
            str(node.get("oid") or "")
            for scan in self.scans if not (scan.walk.overflow or scan.walk.error)
            for node in scan.walk.nodes
        }

        def new_only(node: dict) -> dict | None:
            oid = str(node.get("oid") or "")
            if oid in self.known or (store is not None and store.has(self.repo.full_name, oid)):
                return None
            self.known.add(oid)
            return node

        added: list[Walk] = []
        for scan in self.scans:
            if scan.by_author or not (scan.walk.overflow or scan.walk.error):
                continue
            scan.replaced_by = [
                _Scan(
                    scan.branch,
                    _history_walk(self.client, self.repo.full_name, scan.head, author, self.since, new_only),
                    login,
                    True,
                    priority=scan.priority + index + 1,
                )
                for index, (login, author) in enumerate(filters)
            ]
            added.extend(child.walk for child in scan.replaced_by)
        return added

    def failure(self) -> str:
        """Why the whole repository must be read with REST instead ("" = it
        needn't): its branches couldn't be listed, or its default branch
        couldn't be read."""
        if self.heads.error or self.unplanned:
            return self.heads.error or self.unplanned
        for scan in self.scans:
            if scan.by_author and scan.walk.error:
                return scan.walk.error
        return ""

    def failed_branches(self) -> list[str]:
        """Other branches that couldn't be read through GraphQL (REST reads
        just these; everything else this plan read stays)."""
        return [
            scan.branch for scan in self.scans
            if not scan.by_author and any(leaf.walk.error for leaf in scan.leaves())
        ]

    known: set = field(default_factory=set)  # SHAs replacement walks skip (see replace_failed_compares)

    def held(self) -> int:
        """Commit nodes this plan holds in memory."""
        return sum(len(leaf.walk.nodes) for scan in self.scans for leaf in scan.leaves())

    def spill(self, logins: set[str], emails: set[str]) -> None:
        """Move what was read so far into the store, each scan's commits under
        its priority: memory stays bounded however big the repository is."""
        for scan in self.scans:
            for leaf in scan.leaves():
                if leaf.walk.nodes and not leaf.walk.error and not leaf.walk.overflow:
                    self.store.add(self._records(leaf, logins, emails), leaf.priority)
                    leaf.walk.nodes = []
        self.known.clear()  # the store answers for them now

    def finish(self, logins: set[str], emails: set[str]) -> None:
        """The plan is done (not failed): its rows from failed walks go (REST
        reads those branches), the rest of what it read goes to the store."""
        for scan in self.scans:
            for leaf in scan.leaves():
                if leaf.walk.error:
                    self.store.delete_repo(self.repo.full_name, leaf.priority)
                    leaf.walk.nodes = []
        self.spill(logins, emails)

    def _records(self, leaf: _Scan, logins: set[str], emails: set[str]):
        for node in leaf.walk.nodes:
            payload = rest_commit(node)
            if not leaf.by_author and not _authored_by(payload, logins, emails):
                continue
            if payload["sha"]:
                yield _commit_from_payload(payload, self.repo, leaf.branch, leaf.fallback_login)

    def commits(self, logins: set[str], emails: set[str]) -> list[CommitRecord]:
        """De-duplicated by SHA; the first source (default branch first) wins."""
        seen: set[str] = set()
        out: list[CommitRecord] = []
        for scan in self.scans:
            for leaf in scan.leaves():
                if leaf.walk.error:
                    continue  # read again with REST (failed_branches)
                for node in leaf.walk.nodes:
                    payload = rest_commit(node)
                    if not leaf.by_author and not _authored_by(payload, logins, emails):
                        continue
                    sha = payload["sha"]
                    if sha and sha not in seen:
                        seen.add(sha)
                        out.append(_commit_from_payload(payload, self.repo, leaf.branch, leaf.fallback_login))
        return out


async def resolve_user_ids(client: GitHubClient, logins: list[str], *, inflight: int) -> dict[str, str] | None:
    """GraphQL node ids of ``logins``, or ``None`` if any cannot be resolved."""
    walks = [_user_walk(client, login) for login in logins]
    await run_walks(walks, BATCH["users"], inflight=inflight)
    ids: dict[str, str] = {}
    for login, walk in zip(logins, walks):
        node = walk.nodes[0] if walk.nodes else None
        if walk.error or not isinstance(node, dict) or not node.get("id"):
            log.info("could not resolve user %s through GraphQL: %s", login, walk.error or "not found")
            return None
        ids[login] = str(node["id"])
    return ids


#: Commit nodes collect_commits holds in memory before it moves them to its
#: store (about 1.8 KB each).
NODE_BUDGET = 20_000

#: History walks of a period report start this long before the period: the
#: walk filters by commit date, the report by author date (a commit is
#: committed after it was authored, by days when it is rebased later).
SINCE_MARGIN = timedelta(days=90)


async def collect_commits(
    repos: list[RepoRecord],
    client_for: Callable[[RepoRecord], GitHubClient],
    target_logins: list[str],
    author_emails: list[str],
    *,
    scan_all_branches: bool,
    inflight: int,
    since: datetime | None = None,
    coverage: Coverage | None = None,
    store: Any = None,
) -> tuple[list[CommitRecord], list[tuple[RepoRecord, list[str] | None]]] | None:
    """Commits by the tracked logins / emails in ``repos``.

    Same result as :func:`commits.collect_commits_for_repo` on each repo:
    every branch for affiliated repositories (when ``scan_all_branches``),
    the default branch otherwise, de-duplicated by SHA with the default branch
    first. ``since`` (the report period's start) spares reading older history.

    Returns ``(commits, what to read with REST instead)`` — each a repository
    with the branches to read (``None``: all of it) — or ``None`` when GraphQL
    cannot be used at all. A branch that can't be read through GraphQL costs
    only that branch, never what was read of the rest of its repository.

    With a ``store`` (a :class:`~github_contrib.commitstore.CommitStore`),
    commits go there instead, each under its scan's priority, whenever more
    than NODE_BUDGET are in memory: the returned list is then empty and the
    store keeps the first of each SHA per repository
    (``keep_first_per_repo_sha``) once everything is read.
    """
    if not repos:
        return [], []
    ids = await resolve_user_ids(client_for(repos[0]), target_logins, inflight=inflight)
    if ids is None:
        return None
    primary = target_logins[0] if target_logins else ""
    filters = [(login, f"{{id: {_str(uid)}}}") for login, uid in ids.items()]
    if author_emails:
        filters.append((primary, "{emails: [" + ", ".join(_str(e) for e in author_emails) + "]}"))
    logins = {login.lower() for login in target_logins}
    emails = {email.lower() for email in author_emails}
    keep = node_author_filter(logins, emails)
    since_text = (
        (since - SINCE_MARGIN).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if since else ""
    )

    plans = [_RepoPlan(repo, client_for(repo), scan_all_branches and repo.affiliated) for repo in repos]
    for plan in plans:
        plan.store = store
    with _Progress("Branches", len(plans)) as bar:
        await run_walks([p.heads for p in plans], BATCH["heads"], inflight=inflight, progress=bar)
    live = [p for p in plans if not p.heads.error]
    for plan in live:
        plan.plan(filters, primary, keep, since_text)

    def bound_memory() -> None:
        if store is not None and sum(p.held() for p in live) > NODE_BUDGET:
            for plan in live:
                plan.spill(logins, emails)

    histories = [s.walk for p in live for s in p.scans if s.by_author]
    compares = [s.walk for p in live for s in p.scans if not s.by_author]
    with _Progress("Commits", len(histories) + len(compares)) as bar:
        await asyncio.gather(
            run_walks(histories, BATCH["history"], inflight=inflight, progress=bar, after_batch=bound_memory),
            run_walks(compares, BATCH["compare"], inflight=inflight, progress=bar, after_batch=bound_memory),
        )
        extra = [walk for p in live for walk in p.replace_failed_compares(filters)]
        bar.add(len(extra))
        await run_walks(extra, BATCH["history"], inflight=inflight, progress=bar, after_batch=bound_memory)

    commits: list[CommitRecord] = []
    rest: list[tuple[RepoRecord, list[str] | None]] = []
    for plan in plans:
        reason = plan.failure()
        if reason:
            log.debug("%s: GraphQL failed (%s); using REST", plan.repo.full_name, reason)
            if store is not None:
                store.delete_repo(plan.repo.full_name)
            rest.append((plan.repo, None))
            continue
        if store is not None:
            plan.finish(logins, emails)
        else:
            commits.extend(plan.commits(logins, emails))
        failed = plan.failed_branches()
        if failed:
            log.debug("%s: %d branch(es) to read with REST", plan.repo.full_name, len(failed))
            rest.append((plan.repo, failed))
    note_skipped(
        [s.walk for p in live for scan in p.scans for s in scan.leaves()], coverage, "commit(s)"
    )
    if extra:
        log.info("%d branch(es) far ahead of their default branch were read per author", len(extra))
    return commits, rest


# ---------------------------------------------------------------------------
# Pull requests
# ---------------------------------------------------------------------------


_PR_FIELDS = (
    "number title state createdAt updatedAt closedAt mergedAt url baseRefName headRefName "
    "additions deletions changedFiles "
    f"mergeCommit {{ oid }} author {{ login }} repository {{ {REPO_FIELDS} }} "
    f"commits(first: {PAGE}) {{ totalCount {_PAGE_INFO} nodes {{ commit {{ {COMMIT_FIELDS} }} }} }}"
)


def _prs_walk(client: GitHubClient, login: str) -> Walk:
    def body(size: int, after: str) -> str:
        return (
            f"pullRequests(first: {size}{after}, orderBy: {{field: CREATED_AT, direction: ASC}}) "
            f"{{ totalCount {_PAGE_INFO} nodes {{ {_PR_FIELDS} }} }}"
        )

    return Walk(client, f"user(login: {_str(login)})", body, lambda obj: obj.get("pullRequests"), page_size=PR_PAGE)


def _pr_commits_walk(client: GitHubClient, full_name: str, number: int, cursor: str) -> Walk:
    def body(size: int, after: str) -> str:
        return (
            f"pullRequest(number: {int(number)}) {{ commits(first: {size}{after}) "
            f"{{ {_PAGE_INFO} nodes {{ commit {{ {COMMIT_FIELDS} }} }} }} }}"
        )

    def path(obj: dict) -> dict | None:
        pr = obj.get("pullRequest")
        if not isinstance(pr, dict):
            raise _Missing(f"pull request #{number} could not be read")
        return pr.get("commits")

    return Walk(client, _repo_root(full_name), body, path, cursor=cursor)


@dataclass
class PullRequestData:
    """Everything :func:`collect_pull_requests` found."""

    prs: list[PullRequestRecord] = field(default_factory=list)
    # (repo, number) -> the SHA of every commit of the pull request, in order
    commit_shas: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    # (repo, number) -> the commit nodes by the tracked users (only those are
    # kept: a team's pull requests hold mostly other people's commits)
    commit_nodes: dict[tuple[str, int], list[dict]] = field(default_factory=dict)
    # (repo, number) -> how many commits GitHub says the pull request has
    commit_totals: dict[tuple[str, int], int] = field(default_factory=dict)
    # Pull requests whose commits could not all be read (use REST for those)
    incomplete: set[tuple[str, int]] = field(default_factory=set)
    # Repositories the pull requests live in, keyed by full name
    repos: dict[str, RepoRecord] = field(default_factory=dict)

    def add_commits(self, key: tuple[str, int], nodes: list[dict], keep: Callable[[dict], dict | None]) -> None:
        shas = self.commit_shas.setdefault(key, [])
        mine = self.commit_nodes.setdefault(key, [])
        for node in nodes:
            sha = str(node.get("oid") or "")
            if sha:
                shas.append(sha)
                if keep(node) is not None:
                    mine.append(node)

    def commits_of(
        self,
        pr: PullRequestRecord,
        repo: RepoRecord,
        target_logins: list[str],
        author_emails: list[str],
        coverage: Coverage,
    ) -> tuple[list[str], list[CommitRecord]]:
        """Like :func:`commits.collect_pr_commits`: all SHAs + the tracked users' commits."""
        logins = {login.lower() for login in target_logins}
        emails = {email.lower() for email in author_emails}
        primary = target_logins[0] if target_logins else ""
        branch = pr.head_branch or f"pull/{pr.number}"
        key = (pr.full_name, pr.number)
        shas = list(self.commit_shas.get(key, []))
        records: list[CommitRecord] = []
        for node in self.commit_nodes.get(key, []):
            payload = rest_commit(node)
            if payload["sha"] and _authored_by(payload, logins, emails):
                records.append(_commit_from_payload(payload, repo, branch, primary))
        total = self.commit_totals.get(key, len(shas))
        if len(shas) < total or len(shas) == PR_COMMITS_LIMIT:
            coverage.warn(
                f"{repo.full_name} PR #{pr.number}: GitHub lists only the first "
                f"{PR_COMMITS_LIMIT} commits of a pull request; later commits that exist "
                "only in this pull request may be missing."
            )
        return shas, records


def _slim_pr(keep: Callable[[dict], dict | None]) -> Callable[[dict], dict | None]:
    """``Walk.convert`` for pull requests: each keeps its commits' SHAs but
    only the tracked users' commit nodes (5,000 pull requests of a team carry
    hundreds of thousands of commit nodes otherwise)."""

    def convert(node: dict) -> dict | None:
        conn = node.get("commits")
        if isinstance(conn, dict):
            commits = [c["commit"] for c in conn.get("nodes") or [] if isinstance((c or {}).get("commit"), dict)]
            node = {
                **node,
                "commits": {key: value for key, value in conn.items() if key != "nodes"},
                "_shas": [str(c.get("oid") or "") for c in commits],
                "_mine": [c for c in commits if keep(c) is not None],
            }
        return node

    return convert


async def collect_pull_requests(
    clients: dict[str, GitHubClient],
    target_logins: list[str],
    *,
    inflight: int,
    author_emails: list[str] | None = None,
    coverage: Coverage | None = None,
) -> PullRequestData | None:
    """Every pull request opened by ``target_logins`` that any token can see.

    Replaces both listing every pull request of every affiliated repository
    and the pull-request search: a user's own pull-request list covers private
    repositories the token can read as well as upstream projects. Returns
    ``None`` if a list could not be read completely (the caller then uses the
    REST collectors). A single pull request GitHub won't show (SAML SSO, a
    deleted repository) is noted and left out instead.
    """
    keep = node_author_filter(
        {login.lower() for login in target_logins}, {email.lower() for email in author_emails or []}
    )
    pairs = [(account, login) for account in clients for login in target_logins]
    walks = [_prs_walk(clients[account], login) for account, login in pairs]
    for walk in walks:
        walk.convert = _slim_pr(keep)
    with _Progress("Pull requests", len(walks)) as bar:
        await run_walks(walks, BATCH["prs"], inflight=inflight, progress=bar)
    for (account, login), walk in zip(pairs, walks):
        if walk.error:
            log.info("[%s] pull requests of %s could not be listed through GraphQL: %s", account, login, walk.error)
            return None
    note_skipped(walks, coverage, "pull request(s)")

    result = PullRequestData()
    follow: list[tuple[tuple[str, int], Walk]] = []
    for (account, _login), walk in zip(pairs, walks):
        for node in walk.nodes:
            repo_node = node.get("repository")
            if not isinstance(repo_node, dict) or not repo_node.get("nameWithOwner"):
                continue
            repo = repo_record(repo_node, account)
            known = result.repos.get(repo.full_name)
            if known is None:
                result.repos[repo.full_name] = known = repo
            else:
                known.discovered_via.add(account)
            pr = pr_record(node, known)
            key = (pr.full_name, pr.number)
            if key in result.commit_shas:
                continue  # seen through another token
            result.prs.append(pr)
            result.commit_shas[key] = [sha for sha in node.get("_shas") or [] if sha]
            result.commit_nodes[key] = list(node.get("_mine") or [])
            conn = node.get("commits") or {}
            if isinstance(conn.get("totalCount"), int):
                result.commit_totals[key] = conn["totalCount"]
            info = conn.get("pageInfo") or {}
            if info.get("hasNextPage") and info.get("endCursor"):
                follow.append((key, _pr_commits_walk(clients[account], pr.full_name, pr.number, str(info["endCursor"]))))
        walk.nodes = []  # read: free it

    if follow:
        await run_walks([w for _key, w in follow], BATCH["pr_commits"], inflight=inflight)
        for key, walk in follow:
            if walk.error:
                result.incomplete.add(key)
            else:
                commits = [n["commit"] for n in walk.nodes if isinstance(n.get("commit"), dict)]
                result.add_commits(key, commits, keep)
                walk.nodes = []
    return result


# ---------------------------------------------------------------------------
# Line statistics
# ---------------------------------------------------------------------------


def _stats_walk(client: GitHubClient, full_name: str, sha: str) -> Walk:
    def body(_size: int, _after: str) -> str:
        return f"object(oid: {_str(sha)}) {{ ... on Commit {{ additions deletions changedFilesIfAvailable }} }}"

    return Walk(client, _repo_root(full_name), body, lambda obj: {"nodes": [obj.get("object") or {}]})


async def enrich_commits_with_stats(
    repo_client: dict[str, GitHubClient],
    commits: list[CommitRecord],
    coverage: Coverage,
    *,
    inflight: int,
    failed: list[str] | None = None,
) -> None:
    """Additions / deletions / files changed, 25 commits per query.

    Same rules as :func:`commits.enrich_commits_with_stats` (merge commits
    skipped, one lookup per SHA). Commits GraphQL cannot measure — a file
    count GitHub could not compute, an unreadable commit — are measured with
    the REST collector, which reports any that still fail.
    """
    fallback = next(iter(repo_client.values()), None)
    if fallback is None:
        return
    by_sha: dict[str, list[CommitRecord]] = {}
    for commit in commits:
        if commit.sha and not commit.is_merge:
            by_sha.setdefault(commit.sha, []).append(commit)
    walks = {
        sha: _stats_walk(repo_client.get(group[0].full_name) or fallback, group[0].full_name, sha)
        for sha, group in by_sha.items()
    }
    with _Progress("Line stats", len(walks)) as bar:
        await run_walks(list(walks.values()), BATCH["stats"], inflight=inflight, progress=bar)

    leftover: list[CommitRecord] = []
    for sha, walk in walks.items():
        node = walk.nodes[0] if walk.nodes else {}
        values = [node.get("additions"), node.get("deletions"), node.get("changedFilesIfAvailable")]
        if walk.error or not all(isinstance(v, int) for v in values):
            leftover.extend(by_sha[sha])
            continue
        for commit in by_sha[sha]:
            commit.additions, commit.deletions, commit.files_changed = values
            commit.stats_fetched = True
    if leftover:
        log.info("measuring %d commit(s) through the REST API", len({c.sha for c in leftover}))
        await enrich_commits_with_stats_rest(repo_client, leftover, coverage, failed=failed)
