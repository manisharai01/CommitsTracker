"""Pull request collection.

We list every PR in a repository (``state=all``) and keep those opened by a
tracked user.  This works for private repositories too (the Search API can be
flakier for private data and has a much smaller rate-limit budget).
"""

from __future__ import annotations

from typing import Any

from .client import GitHubClient, GitHubError
from .logging_config import get_logger
from .models import PullRequestRecord, RepoRecord, parse_github_datetime

log = get_logger("pull_requests")


def _pr_from_payload(payload: dict[str, Any], repo: RepoRecord) -> PullRequestRecord:
    user = payload.get("user") or {}
    base = payload.get("base") or {}
    head = payload.get("head") or {}
    merged_at = parse_github_datetime(payload.get("merged_at"))
    return PullRequestRecord(
        repository=repo.name,
        full_name=repo.full_name,
        organization=repo.organization,
        number=int(payload.get("number") or 0),
        title=str(payload.get("title") or ""),
        author_login=str(user.get("login") or ""),
        state=str(payload.get("state") or ""),
        merged=merged_at is not None,
        created_at=parse_github_datetime(payload.get("created_at")),
        updated_at=parse_github_datetime(payload.get("updated_at")),
        closed_at=parse_github_datetime(payload.get("closed_at")),
        merged_at=merged_at,
        base_branch=str(base.get("ref") or ""),
        head_branch=str(head.get("ref") or ""),
        url=str(payload.get("html_url", "")),
    )


async def collect_prs_for_repo(
    client: GitHubClient,
    repo: RepoRecord,
    target_logins: list[str],
) -> list[PullRequestRecord]:
    """Collect open/closed/merged PRs opened by ``target_logins`` in ``repo``."""
    targets = {login.lower() for login in target_logins}
    results: list[PullRequestRecord] = []
    params = {"state": "all", "sort": "created", "direction": "desc"}
    try:
        async for payload in client.paginate(
            f"/repos/{repo.full_name}/pulls", params=params
        ):
            if not isinstance(payload, dict):
                continue
            user = payload.get("user") or {}
            login = str(user.get("login") or "").lower()
            if login in targets:
                results.append(_pr_from_payload(payload, repo))
    except GitHubError as exc:
        log.debug("Skipping PRs for %s: %s", repo.full_name, exc)
    if results:
        log.debug("%s: %d PR(s) by tracked users", repo.full_name, len(results))
    return results
