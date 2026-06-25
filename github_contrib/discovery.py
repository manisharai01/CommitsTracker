"""Repository and organization discovery.

A single call to ``GET /user/repos`` with the right ``affiliation`` and
``visibility`` query parameters returns personal, private, organization and
collaborator repositories for the authenticated user, so we lean on that and
then enrich with organization metadata from ``GET /user/orgs``.
"""

from __future__ import annotations

from typing import Any

from .client import GitHubClient, GitHubError
from .config import Account
from .logging_config import get_logger
from .models import OrgRecord, RepoRecord, parse_github_datetime

log = get_logger("discovery")


def _repo_from_payload(payload: dict[str, Any], discovered_by: str) -> RepoRecord:
    owner = payload.get("owner") or {}
    owner_login = str(owner.get("login", ""))
    owner_type = str(owner.get("type", ""))
    organization = owner_login if owner_type == "Organization" else ""
    return RepoRecord(
        full_name=str(payload.get("full_name", "")),
        name=str(payload.get("name", "")),
        owner=owner_login,
        organization=organization,
        is_private=bool(payload.get("private", False)),
        is_fork=bool(payload.get("fork", False)),
        is_archived=bool(payload.get("archived", False)),
        default_branch=str(payload.get("default_branch") or "main"),
        html_url=str(payload.get("html_url", "")),
        description=str(payload.get("description") or ""),
        language=str(payload.get("language") or ""),
        stargazers=int(payload.get("stargazers_count") or 0),
        forks=int(payload.get("forks_count") or 0),
        pushed_at=parse_github_datetime(payload.get("pushed_at")),
        created_at=parse_github_datetime(payload.get("created_at")),
        discovered_via={discovered_by},
    )


async def discover_repositories(
    client: GitHubClient,
    account: Account,
    *,
    max_repos: int | None = None,
) -> list[RepoRecord]:
    """Discover all repositories accessible to ``account``."""
    params = {
        "visibility": "all",
        "affiliation": "owner,collaborator,organization_member",
        "sort": "pushed",
        "direction": "desc",
    }
    repos: list[RepoRecord] = []
    async for payload in client.paginate("/user/repos", params=params, max_items=max_repos):
        if not isinstance(payload, dict):
            continue
        repos.append(_repo_from_payload(payload, account.login))
    log.info("[%s] discovered %d accessible repositories", account.login, len(repos))
    return repos


async def discover_organizations(
    client: GitHubClient,
    account: Account,
) -> list[OrgRecord]:
    """Discover the organizations ``account`` is a member of."""
    orgs: list[OrgRecord] = []
    async for payload in client.paginate("/user/orgs"):
        if not isinstance(payload, dict):
            continue
        orgs.append(
            OrgRecord(
                login=str(payload.get("login", "")),
                name=str(payload.get("description") or payload.get("login") or ""),
                url=str(payload.get("url", "")),
                is_member=True,
            )
        )
    log.info("[%s] member of %d organization(s)", account.login, len(orgs))
    return orgs


async def fetch_repo(
    client: GitHubClient,
    full_name: str,
    discovered_by: str,
) -> RepoRecord | None:
    """Fetch a single repository's metadata by ``owner/name``."""
    try:
        payload = await client.get_json(f"/repos/{full_name}")
    except GitHubError as exc:
        log.debug("could not fetch repo %s: %s", full_name, exc)
        return None
    if not isinstance(payload, dict) or not payload.get("full_name"):
        return None
    return _repo_from_payload(payload, discovered_by)


async def discover_via_search_commits(
    client: GitHubClient,
    query_login: str,
    *,
    max_items: int = 1000,
) -> list[str]:
    """Return distinct repository ``full_name``s in which ``query_login``
    authored commits *that this token can see*, via the commit Search API.

    This catches repositories that ``/user/repos`` may not list (the Search
    index is contribution-based). Search is rate-limited (~30 req/min) and
    capped at 1000 results, so it augments — never replaces — repo listing.
    """
    seen: set[str] = set()
    params = {"q": f"author:{query_login}", "sort": "author-date", "order": "desc"}
    try:
        async for item in client.paginate(
            "/search/commits", params=params, max_items=max_items
        ):
            repo = (item or {}).get("repository") or {}
            full = repo.get("full_name")
            if full:
                seen.add(str(full))
    except GitHubError as exc:
        log.debug("search/commits failed for author=%s: %s", query_login, exc)
    if seen:
        log.info("search found %d repo(s) with commits by %s", len(seen), query_login)
    return sorted(seen)


async def discover_org_repos(
    client: GitHubClient,
    org_login: str,
    discovered_by: str,
) -> list[RepoRecord]:
    """Enumerate every repository in ``org_login`` the token can access."""
    repos: list[RepoRecord] = []
    try:
        async for payload in client.paginate(
            f"/orgs/{org_login}/repos", params={"type": "all", "sort": "pushed"}
        ):
            if isinstance(payload, dict) and payload.get("full_name"):
                repos.append(_repo_from_payload(payload, discovered_by))
    except GitHubError as exc:
        log.debug("could not list repos for org %s: %s", org_login, exc)
    if repos:
        log.info("[%s] org '%s' contributed %d repo(s)", discovered_by, org_login, len(repos))
    return repos


def merge_repositories(
    existing: dict[str, RepoRecord],
    new_repos: list[RepoRecord],
) -> None:
    """Merge ``new_repos`` into ``existing`` (keyed by full_name), unioning the
    ``discovered_via`` sets so we know which tokens can reach each repo."""
    for repo in new_repos:
        if not repo.full_name:
            continue
        current = existing.get(repo.full_name)
        if current is None:
            existing[repo.full_name] = repo
        else:
            current.discovered_via |= repo.discovered_via
