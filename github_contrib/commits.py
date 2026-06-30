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
    author_emails: list[str] | None = None,
) -> list[CommitRecord]:
    """Collect commits authored by ``target_logins`` (and optionally ``author_emails``) in ``repo``.

    The GitHub API's ``?author=`` parameter accepts both a GitHub login and a raw
    email address.  Login-based queries return commits whose author email is linked
    to that GitHub account.  Email-based queries catch commits made with a work or
    personal address that has *not* been added to the GitHub profile — the most
    common reason private/work commits are missing.

    Commits are de-duplicated by SHA within the repository (a commit reachable
    from multiple branches is reported once, against the first branch on which
    it is found).
    """
    branches = await _branches_to_scan(client, repo, scan_all_branches)
    seen: set[str] = set()
    results: list[CommitRecord] = []

    # (author_key passed to ?author=, fallback_login used when the payload has
    #  no gh_author.login — i.e. the email is not linked to any GitHub account)
    primary_login = target_logins[0] if target_logins else ""
    author_keys: list[tuple[str, str]] = [(login, login) for login in target_logins]
    for email in (author_emails or []):
        author_keys.append((email, primary_login))

    for author_key, fallback_login in author_keys:
        for branch in branches:
            params: dict[str, Any] = {"author": author_key, "sha": branch}
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
                    results.append(_commit_from_payload(payload, repo, branch, fallback_login))
            except GitHubError as exc:
                # Empty repos, disabled repos, or revoked access - skip quietly.
                log.debug(
                    "Skipping commits for %s (author=%s, branch=%s): %s",
                    repo.full_name,
                    author_key,
                    branch,
                    exc,
                )
                continue

    if results:
        log.debug("%s: %d commit(s) by tracked users", repo.full_name, len(results))
    return results


async def enrich_commits_with_stats(
    repo_client: dict[str, "GitHubClient"],
    commits: list[CommitRecord],
) -> None:
    """Fetch line-level stats for every commit (one extra API request each).

    Results are written in-place.  Individual failures are silently skipped so
    a single 403/404 never aborts the enrichment pass.  Pass ``repo_client`` as
    a ``{full_name: GitHubClient}`` mapping so each request uses the token that
    already has read access to that repository.
    """
    import asyncio

    fallback: GitHubClient | None = next(iter(repo_client.values()), None)
    if fallback is None:
        return

    async def _one(commit: CommitRecord) -> None:
        client = repo_client.get(commit.full_name) or fallback
        try:
            payload = await client.request(  # type: ignore[union-attr]
                "GET", f"/repos/{commit.full_name}/commits/{commit.sha}"
            )
            if isinstance(payload, dict):
                s = payload.get("stats") or {}
                commit.additions = int(s.get("additions") or 0)
                commit.deletions = int(s.get("deletions") or 0)
                commit.files_changed = len(payload.get("files") or [])
        except Exception:  # noqa: BLE001 - best effort; never abort
            pass

    await asyncio.gather(*[_one(c) for c in commits])
