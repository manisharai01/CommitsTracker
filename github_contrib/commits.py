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
) -> list[CommitRecord]:
    """Collect commits authored by ``target_logins`` (and ``author_emails``) in ``repo``.

    The API's ``?author=`` parameter accepts both a GitHub login and a raw
    email address. Login queries return commits whose author email is linked
    to that account; email queries catch commits made with an address that is
    *not* on the GitHub profile — the most common reason work commits go missing.

    Commits are de-duplicated by SHA within the repository (a commit reachable
    from several branches is reported once, against the first branch scanned).
    """
    coverage = coverage if coverage is not None else Coverage()
    branches = await _branches_to_scan(client, repo, scan_all_branches, coverage)

    # (author_key passed to ?author=, fallback_login used when the payload has
    #  no author login — i.e. the email is not linked to any GitHub account)
    primary_login = target_logins[0] if target_logins else ""
    author_keys: list[tuple[str, str]] = [(login, login) for login in target_logins]
    author_keys += [(email, primary_login) for email in author_emails or []]

    async def scan(author_key: str, branch: str) -> list[dict[str, Any]]:
        params = {"author": author_key, "sha": branch}
        found: list[dict[str, Any]] = []
        try:
            async for payload in client.paginate(f"/repos/{repo.full_name}/commits", params=params):
                if isinstance(payload, dict):
                    found.append(payload)
        except GitHubError as exc:
            coverage.warn(
                f"{repo.full_name}: commits by {author_key} on branch '{branch}' could not "
                f"be fully read ({exc})."
            )
        return found

    jobs = [(key, fallback, branch) for key, fallback in author_keys for branch in branches]
    pages = await asyncio.gather(*(scan(key, branch) for key, _fallback, branch in jobs))

    seen: set[str] = set()
    results: list[CommitRecord] = []
    for (_key, fallback_login, branch), payloads in zip(jobs, pages):
        for payload in payloads:
            sha = str(payload.get("sha", ""))
            if sha and sha not in seen:
                seen.add(sha)
                results.append(_commit_from_payload(payload, repo, branch, fallback_login))

    if results:
        log.debug("%s: %d commit(s) by tracked users", repo.full_name, len(results))
    return results


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
) -> None:
    """Fill in additions / deletions / files changed (one request per commit).

    Merge commits are skipped: their diff against the first parent repeats
    work from the merged branch (often other people's), so counting it would
    inflate the line totals. The same SHA found in several repositories (a
    fork and its upstream) is fetched once. Failures are counted and reported
    instead of silently leaving zeros.
    """
    coverage = coverage if coverage is not None else Coverage()
    fallback: GitHubClient | None = next(iter(repo_client.values()), None)
    if fallback is None:
        return

    by_sha: dict[str, list[CommitRecord]] = {}
    for commit in commits:
        if commit.sha and not commit.is_merge:
            by_sha.setdefault(commit.sha, []).append(commit)

    failed: list[str] = []

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
    if failed:
        sample = ", ".join(sorted(failed)[:5])
        coverage.warn(
            f"Line statistics could not be read for {len(failed)} commit(s) "
            f"(e.g. {sample}); their added/deleted lines are not counted."
        )
