"""Commit collection.

For each repository we ask the API for the commits authored by each tracked
login — and by each extra author email — on every branch
(``GET /repos/{owner}/{repo}/commits?author=<login|email>&sha=<branch>``).
Branches whose head is the same commit share their history, so each distinct
head is scanned once (the default branch first, so a commit that landed there
is attributed to it).

Repositories the user is not affiliated with (upstream projects found through
the Search API) only have their default branch scanned: the user's other work
there lives in their pull requests, which :func:`collect_pr_commits` reads
directly — that also recovers commits whose branch was deleted.

Every failure is recorded as a data-completeness note: a repository that could
not be read must never look like a repository without contributions.
"""

from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable

from .client import GitHubClient, GitHubError
from .logging_config import get_logger
from .models import CommitRecord, Coverage, PullRequestRecord, RepoRecord, parse_github_datetime

log = get_logger("commits")

#: The pull-request commits endpoint lists at most this many commits.
PR_COMMITS_LIMIT = 250
#: Answers that mean commits exist but can't be read (vs 409: an empty repository).
_UNREADABLE = {
    404: "not found: deleted, renamed, or no longer open to the token",
    451: "unavailable for legal reasons",
}


def _commit_from_payload(
    payload: dict[str, Any],
    repo: RepoRecord,
    branch: str,
    fallback_login: str,
) -> CommitRecord:
    commit = payload.get("commit") or {}
    author = commit.get("author") or {}
    committer = commit.get("committer") or {}
    gh_author = payload.get("author") or {}
    message = str(commit.get("message") or "")
    first_line = message.splitlines()[0] if message else ""
    return CommitRecord(
        repository=repo.name,
        full_name=repo.full_name,
        owner=repo.owner,
        organization=repo.organization,
        sha=str(payload.get("sha", "")),
        message=message,
        message_first_line=first_line,
        author_login=str((gh_author or {}).get("login") or fallback_login),
        author_name=str(author.get("name") or ""),
        author_email=str(author.get("email") or ""),
        committer_name=str(committer.get("name") or ""),
        committer_email=str(committer.get("email") or ""),
        authored_date=parse_github_datetime(author.get("date")),
        committed_date=parse_github_datetime(committer.get("date")),
        branch=branch,
        url=str(payload.get("html_url", "")),
        parent_count=len(payload.get("parents") or []),
    )


async def _branches_to_scan(
    client: GitHubClient,
    repo: RepoRecord,
    scan_all_branches: bool,
    coverage: Coverage,
) -> list[str]:
    """Branch names to scan: one per distinct head commit, default branch first."""
    if not (scan_all_branches and repo.affiliated):
        return [repo.default_branch]
    heads: dict[str, str] = {}  # branch -> head sha
    try:
        async for payload in client.paginate(f"/repos/{repo.full_name}/branches"):
            if isinstance(payload, dict) and payload.get("name"):
                head = (payload.get("commit") or {}).get("sha")
                heads[str(payload["name"])] = str(head or payload["name"])
    except GitHubError as exc:
        coverage.warn(
            f"{repo.full_name}: could not list branches ({exc}); only the default "
            "branch was scanned, so commits that exist only on other branches are missing."
        )
        return [repo.default_branch]

    ordered = sorted(heads, key=lambda name: (name != repo.default_branch, name))
    branches: list[str] = []
    seen_heads: set[str] = set()
    for name in ordered:
        if heads[name] not in seen_heads:
            seen_heads.add(heads[name])
            branches.append(name)
    return branches or [repo.default_branch]


async def collect_commits_for_repo(
    client: GitHubClient,
    repo: RepoRecord,
    target_logins: list[str],
    *,
    scan_all_branches: bool = False,
    author_emails: list[str] | None = None,
    coverage: Coverage | None = None,
    branches: list[str] | None = None,
    store: Any = None,
    priority: int = 0,
) -> list[CommitRecord]:
    """Collect commits authored by ``target_logins`` (and ``author_emails``) in ``repo``.

    The API's ``?author=`` parameter accepts both a GitHub login and a raw
    email address. Login queries return commits whose author email is linked
    to that account; email queries catch commits made with an address that is
    *not* on the GitHub profile — the most common reason work commits go missing.

    Commits are de-duplicated by SHA within the repository (a commit reachable
    from several branches is reported once, against the first branch scanned).
    ``branches`` names the branches to read (those GraphQL couldn't) instead
    of listing them all.

    With a ``store`` (a :class:`~github_contrib.commitstore.CommitStore`) the
    commits are written there as pages arrive - each branch scan under its
    own priority from ``priority`` on, so the store keeps the same commit
    for each SHA as the list would - and an empty list is returned.
    """
    coverage = coverage if coverage is not None else Coverage()
    if not branches:
        branches = await _branches_to_scan(client, repo, scan_all_branches, coverage)

    # (author_key passed to ?author=, fallback_login used when the payload has
    #  no author login — i.e. the email is not linked to any GitHub account)
    primary_login = target_logins[0] if target_logins else ""
    author_keys: list[tuple[str, str]] = [(login, login) for login in target_logins]
    author_keys += [(email, primary_login) for email in author_emails or []]

    async def scan(author_key: str, fallback_login: str, branch: str, known: set[str]) -> list[CommitRecord]:
        """One author's commits on one branch, as records, without the ones
        ``known`` already holds: each branch lists its whole history, so
        keeping raw pages would hold the same commits once per branch."""
        params = {"author": author_key, "sha": branch}
        found: list[CommitRecord] = []
        mine: set[str] = set()

        def empty(status: int) -> None:
            # 409 is an empty repository; 404 / 451 mean the commits exist but
            # can't be read, which must never look like "no contributions".
            if status in _UNREADABLE:
                coverage.warn(
                    f"{repo.full_name}: GitHub answered {status} ({_UNREADABLE[status]}) for the commits "
                    f"on '{branch}'; any contributions there are missing."
                )

        try:
            async for payload in client.paginate(
                f"/repos/{repo.full_name}/commits", params=params, on_empty=empty
            ):
                if not isinstance(payload, dict):
                    continue
                sha = str(payload.get("sha", ""))
                if sha and sha not in known and sha not in mine:
                    mine.add(sha)
                    found.append(_commit_from_payload(payload, repo, branch, fallback_login))
        except GitHubError as exc:
            coverage.warn(
                f"{repo.full_name}: commits by {author_key} on branch '{branch}' could not "
                f"be fully read ({exc})."
            )
        return found

    if store is not None:
        await _scan_into_store(
            client, repo, branches, author_keys, coverage, store, priority
        )
        return []

    seen: set[str] = set()
    results: list[CommitRecord] = []

    def merge(batches: list[list[CommitRecord]]) -> None:
        for batch in batches:
            for record in batch:
                if record.sha not in seen:
                    seen.add(record.sha)
                    results.append(record)

    # The first branch (the default one) first: most commits are on it, and
    # what it holds is then skipped as the other branches are read.
    first, others = branches[0], branches[1:]
    merge(await asyncio.gather(*(scan(key, fallback, first, set()) for key, fallback in author_keys)))
    if others:
        known = set(seen)
        merge(await asyncio.gather(*(
            scan(key, fallback, branch, known) for key, fallback in author_keys for branch in others
        )))

    if results:
        log.debug("%s: %d commit(s) by tracked users", repo.full_name, len(results))
    return results


async def _scan_into_store(
    client: GitHubClient,
    repo: RepoRecord,
    branches: list[str],
    author_keys: list[tuple[str, str]],
    coverage: Coverage,
    store: Any,
    priority: int,
) -> None:
    """collect_commits_for_repo, writing to ``store`` 500 commits at a time."""

    async def scan(author_key: str, fallback_login: str, branch: str, rank: int, skip_known: bool) -> None:
        params = {"author": author_key, "sha": branch}
        batch: list[CommitRecord] = []

        def empty(status: int) -> None:
            if status in _UNREADABLE:
                coverage.warn(
                    f"{repo.full_name}: GitHub answered {status} ({_UNREADABLE[status]}) for the commits "
                    f"on '{branch}'; any contributions there are missing."
                )

        try:
            async for payload in client.paginate(
                f"/repos/{repo.full_name}/commits", params=params, on_empty=empty
            ):
                if not isinstance(payload, dict):
                    continue
                sha = str(payload.get("sha", ""))
                # Other branches: the default branch's commits are already stored.
                if sha and not (skip_known and store.has(repo.full_name, sha)):
                    batch.append(_commit_from_payload(payload, repo, branch, fallback_login))
                    if len(batch) >= 500:
                        store.add(batch, rank)
                        batch = []
        except GitHubError as exc:
            coverage.warn(
                f"{repo.full_name}: commits by {author_key} on branch '{branch}' could not "
                f"be fully read ({exc})."
            )
        finally:
            if batch:
                store.add(batch, rank)

    first, others = branches[0], branches[1:]
    rank = priority
    jobs = []
    for key, fallback in author_keys:
        jobs.append(scan(key, fallback, first, rank, False))
        rank += 1
    await asyncio.gather(*jobs)
    jobs = []
    for key, fallback in author_keys:
        for branch in others:
            jobs.append(scan(key, fallback, branch, rank, True))
            rank += 1
    await asyncio.gather(*jobs)


def _authored_by(payload: dict[str, Any], logins: set[str], emails: set[str]) -> bool:
    login = str((payload.get("author") or {}).get("login") or "").lower()
    email = str(((payload.get("commit") or {}).get("author") or {}).get("email") or "").lower()
    return login in logins or (bool(email) and email in emails)


async def collect_pr_commits(
    client: GitHubClient,
    repo: RepoRecord,
    pr: PullRequestRecord,
    target_logins: list[str],
    author_emails: list[str] | None,
    coverage: Coverage,
) -> tuple[list[str], list[CommitRecord]]:
    """The SHAs of ``pr``'s commits, plus those authored by the tracked users.

    Reads the pull request itself, so commits whose branch was deleted after
    the pull request was merged or closed are recovered too.
    """
    logins = {login.lower() for login in target_logins}
    emails = {email.lower() for email in author_emails or []}
    primary_login = target_logins[0] if target_logins else ""
    branch = pr.head_branch or f"pull/{pr.number}"
    shas: list[str] = []
    records: list[CommitRecord] = []
    try:
        async for payload in client.paginate(f"/repos/{repo.full_name}/pulls/{pr.number}/commits"):
            if not isinstance(payload, dict) or not payload.get("sha"):
                continue
            shas.append(str(payload["sha"]))
            if _authored_by(payload, logins, emails):
                records.append(_commit_from_payload(payload, repo, branch, primary_login))
    except GitHubError as exc:
        coverage.warn(f"{repo.full_name} PR #{pr.number}: commits could not be read ({exc}).")
    if len(shas) >= PR_COMMITS_LIMIT:
        coverage.warn(
            f"{repo.full_name} PR #{pr.number}: GitHub lists only the first "
            f"{PR_COMMITS_LIMIT} commits of a pull request; later commits that exist "
            "only in this pull request may be missing."
        )
    return shas, records


async def _commit_stats(
    client: GitHubClient, full_name: str, sha: str
) -> tuple[int, int, int] | None:
    """``(additions, deletions, files)`` for one commit, or ``None`` if unreadable.

    GitHub lists 300 files per page of a commit (up to 3000), so the file
    count follows the pagination links; the totals in ``stats`` cover the
    whole commit.
    """
    data, headers, _status = await client.request("GET", f"/repos/{full_name}/commits/{sha}")
    if not isinstance(data, dict):
        return None
    stats = data.get("stats") or {}
    files = len(data.get("files") or [])
    next_url = client._parse_next_link(headers.get("Link") if headers else None)
    while next_url:
        page, headers, status = await client.request("GET", next_url)
        if not isinstance(page, dict):
            raise GitHubError(f"file list of {full_name}@{sha[:7]} interrupted (status {status})")
        files += len(page.get("files") or [])
        next_url = client._parse_next_link(headers.get("Link") if headers else None)
    return int(stats.get("additions") or 0), int(stats.get("deletions") or 0), files


async def enrich_commits_with_stats(
    repo_client: dict[str, GitHubClient],
    commits: list[CommitRecord],
    coverage: Coverage | None = None,
    *,
    gather: Callable[[list[Awaitable[None]]], Awaitable[Any]] | None = None,
    failed: list[str] | None = None,
) -> None:
    """Fill in additions / deletions / files changed (one request per commit).

    Merge commits are skipped: their diff against the first parent repeats
    work from the merged branch (often other people's), so counting it would
    inflate the line totals. The same SHA found in several repositories (a
    fork and its upstream) is fetched once. Failures are counted and reported
    instead of silently leaving zeros. ``failed`` collects the commits that
    couldn't be measured instead (the caller notes them once, for every call).
    """
    coverage = coverage if coverage is not None else Coverage()
    fallback: GitHubClient | None = next(iter(repo_client.values()), None)
    if fallback is None:
        return

    by_sha: dict[str, list[CommitRecord]] = {}
    for commit in commits:
        if commit.sha and not commit.is_merge:
            by_sha.setdefault(commit.sha, []).append(commit)

    gathered = failed
    failed = []

    async def _one(sha: str, group: list[CommitRecord]) -> None:
        first = group[0]
        client = repo_client.get(first.full_name) or fallback
        try:
            result = await _commit_stats(client, first.full_name, sha)
        except GitHubError as exc:
            log.debug("line stats for %s@%s failed: %s", first.full_name, sha[:7], exc)
            result = None
        if result is None:
            failed.append(f"{first.full_name}@{sha[:7]}")
            return
        for commit in group:
            commit.additions, commit.deletions, commit.files_changed = result
            commit.stats_fetched = True

    coros = [_one(sha, group) for sha, group in by_sha.items()]
    await (gather(coros) if gather is not None else asyncio.gather(*coros))
    if gathered is not None:
        gathered.extend(failed)
    elif failed:
        coverage.warn(stats_failure_note(failed))


def stats_failure_note(failed: list[str]) -> str:
    """The completeness note for commits whose lines couldn't be measured."""
    sample = ", ".join(sorted(failed)[:5])
    return (
        f"Line statistics could not be read for {len(failed)} commit(s) "
        f"(e.g. {sample}); their added/deleted lines are not counted."
    )
