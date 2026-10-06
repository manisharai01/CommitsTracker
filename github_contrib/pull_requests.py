"""Pull request collection.

In repositories the user is affiliated with we list every PR (``state=all``)
and keep those opened by a tracked user: this works for private repositories
and is authoritative. Pull requests in other repositories (upstream projects)
come from the Search API — listing every PR of a large public project would
cost thousands of requests — and each is completed with one detail request.
"""

from __future__ import annotations

from typing import Any

from .client import GitHubClient, GitHubError
from .logging_config import get_logger
from .models import Coverage, PullRequestRecord, RepoRecord, parse_github_datetime

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
        merge_commit_sha=str(payload.get("merge_commit_sha") or "") if merged_at else "",
    )


def _pr_from_search_item(item: dict[str, Any], repo: RepoRecord) -> PullRequestRecord:
    """A pull request from an issue-search item (no branch or merge-commit data)."""
    merged_at = parse_github_datetime((item.get("pull_request") or {}).get("merged_at"))
    return PullRequestRecord(
        repository=repo.name,
        full_name=repo.full_name,
        organization=repo.organization,
        number=int(item.get("number") or 0),
        title=str(item.get("title") or ""),
        author_login=str((item.get("user") or {}).get("login") or ""),
        state=str(item.get("state") or ""),
        merged=merged_at is not None,
        created_at=parse_github_datetime(item.get("created_at")),
        updated_at=parse_github_datetime(item.get("updated_at")),
        closed_at=parse_github_datetime(item.get("closed_at")),
        merged_at=merged_at,
        base_branch="",
        head_branch="",
        url=str(item.get("html_url", "")),
    )


async def collect_prs_for_repo(
    client: GitHubClient,
    repo: RepoRecord,
    target_logins: list[str],
    coverage: Coverage | None = None,
) -> list[PullRequestRecord]:
    """Collect open/closed/merged PRs opened by ``target_logins`` in ``repo``."""
    targets = {login.lower() for login in target_logins}
    results: list[PullRequestRecord] = []
    params = {"state": "all", "sort": "created", "direction": "desc"}
    try:
        async for payload in client.paginate(f"/repos/{repo.full_name}/pulls", params=params):
            if not isinstance(payload, dict):
                continue
            login = str((payload.get("user") or {}).get("login") or "").lower()
            if login in targets:
                results.append(_pr_from_payload(payload, repo))
    except GitHubError as exc:
        message = f"{repo.full_name}: pull requests could not be fully read ({exc})."
        if coverage is not None:
            coverage.warn(message)
        else:
            log.warning(message)
    if results:
        log.debug("%s: %d PR(s) by tracked users", repo.full_name, len(results))
    return results


async def collect_pr_from_search(
    client: GitHubClient,
    repo: RepoRecord,
    item: dict[str, Any],
    coverage: Coverage,
) -> PullRequestRecord:
    """Complete one issue-search item into a full pull-request record."""
    number = int(item.get("number") or 0)
    try:
        payload = await client.get_json(f"/repos/{repo.full_name}/pulls/{number}")
    except GitHubError as exc:
        payload = None
        log.debug("detail of %s#%d failed: %s", repo.full_name, number, exc)
    if isinstance(payload, dict) and payload.get("number"):
        return _pr_from_payload(payload, repo)
    coverage.warn(
        f"{repo.full_name} PR #{number}: details could not be read; its branch and "
        "merge commit are unknown."
    )
    return _pr_from_search_item(item, repo)
