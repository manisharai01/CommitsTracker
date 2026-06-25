"""Commit collection.

For each repository we ask the API for commits authored by each tracked login
(``GET /repos/{owner}/{repo}/commits?author=<login>``).  Optionally every
branch is scanned; by default only the default branch is examined (which is the
common case and far kinder to rate limits).
"""

from __future__ import annotations

from typing import Any

from .client import GitHubClient, GitHubError
from .logging_config import get_logger
from .models import CommitRecord, RepoRecord, parse_github_datetime

log = get_logger("commits")


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
    )


async def _branches_to_scan(
    client: GitHubClient,
    repo: RepoRecord,
    scan_all_branches: bool,
) -> list[str]:
    if not scan_all_branches:
        return [repo.default_branch]
    branches: list[str] = []
    try:
        async for payload in client.paginate(f"/repos/{repo.full_name}/branches"):
            if isinstance(payload, dict) and payload.get("name"):
                branches.append(str(payload["name"]))
    except GitHubError as exc:
        log.debug("Could not list branches for %s: %s", repo.full_name, exc)
    if not branches:
        branches = [repo.default_branch]
    return branches


async def collect_commits_for_repo(
    client: GitHubClient,
    repo: RepoRecord,
    target_logins: list[str],
    *,
    scan_all_branches: bool = False,
) -> list[CommitRecord]:
    """Collect commits authored by ``target_logins`` in ``repo``.

    Commits are de-duplicated by SHA within the repository (a commit reachable
    from multiple branches is reported once, against the first branch on which
    it is found)."""
    branches = await _branches_to_scan(client, repo, scan_all_branches)
    seen: set[str] = set()
    results: list[CommitRecord] = []

    for login in target_logins:
        for branch in branches:
            params: dict[str, Any] = {"author": login, "sha": branch}
            try:
                async for payload in client.paginate(
                    f"/repos/{repo.full_name}/commits", params=params
                ):
                    if not isinstance(payload, dict):
                        continue
                    sha = str(payload.get("sha", ""))
                    if not sha or sha in seen:
                        continue
                    seen.add(sha)
                    results.append(_commit_from_payload(payload, repo, branch, login))
            except GitHubError as exc:
                # Empty repos, disabled repos, or revoked access - skip quietly.
                log.debug(
                    "Skipping commits for %s (author=%s, branch=%s): %s",
                    repo.full_name,
                    login,
                    branch,
                    exc,
                )
                continue

    if results:
        log.debug("%s: %d commit(s) by tracked users", repo.full_name, len(results))
    return results
