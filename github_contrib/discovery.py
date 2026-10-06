"""Repository and organization discovery.

A single call to ``GET /user/repos`` with the right ``affiliation`` and
``visibility`` query parameters returns personal, private, organization and
collaborator repositories for the authenticated user, so we lean on that and
then enrich with organization metadata from ``GET /user/orgs``.

The Search API then adds repositories the account is not affiliated with
(upstream projects the user contributed to): those holding commits by the
tracked logins or author emails, and those where they opened pull requests.
Search serves at most 1000 results per query, so :func:`search_all` splits the
date range until each slice fits — no result is dropped silently.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .client import GitHubClient, GitHubError
from .config import Account
from .logging_config import get_logger
from .models import Coverage, OrgRecord, RepoRecord, parse_github_datetime

log = get_logger("discovery")

#: Search windows default to every possible git date: history imported from
#: older systems can predate GitHub, and skewed clocks produce future dates.
SEARCH_START = datetime(1970, 1, 1, tzinfo=timezone.utc)
SEARCH_END = datetime(2099, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
SEARCH_CAP = 1000


def _repo_from_payload(
    payload: dict[str, Any], discovered_by: str, affiliated: bool = True
) -> RepoRecord:
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
        affiliated=affiliated,
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
    *,
    affiliated: bool = False,
) -> RepoRecord | None:
    """Fetch a single repository's metadata by ``owner/name``."""
    try:
        payload = await client.get_json(f"/repos/{full_name}")
    except GitHubError as exc:
        log.debug("could not fetch repo %s: %s", full_name, exc)
        return None
    if not isinstance(payload, dict) or not payload.get("full_name"):
        return None
    return _repo_from_payload(payload, discovered_by, affiliated)


def _search_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


async def search_all(
    client: GitHubClient,
    path: str,
    base_query: str,
    date_field: str,
    start: datetime,
    end: datetime,
    coverage: Coverage,
    what: str,
) -> list[dict[str, Any]]:
    """Every result of ``base_query`` with ``date_field`` in ``[start, end]``.

    A query matching more than 1000 results is split into two halves of its
    date range, recursively, so every slice can be read completely. Gaps that
    cannot be avoided (more than 1000 results within a single second, or
    GitHub reporting a timed-out search) are recorded as completeness notes.
    """
    results: list[dict[str, Any]] = []
    pending = [(start.replace(microsecond=0), end.replace(microsecond=0))]
    while pending:
        lo, hi = pending.pop()
        query = f"{base_query} {date_field}:{_search_stamp(lo)}..{_search_stamp(hi)}"
        items, total, incomplete = await client.search(path, query, split_above=SEARCH_CAP)
        if total > SEARCH_CAP and hi - lo > timedelta(seconds=1):
            mid = (lo + (hi - lo) / 2).replace(microsecond=0)
            pending.append((mid + timedelta(seconds=1), hi))
            pending.append((lo, mid))
            continue
        if incomplete:
            coverage.warn(f"GitHub search timed out while listing {what}; some may be missing.")
        if total > len(items):
            coverage.warn(
                f"GitHub search returned {len(items)} of {total} {what} between "
                f"{_search_stamp(lo)} and {_search_stamp(hi)}; the rest could not be listed."
            )
        results.extend(items)
    return results


async def discover_repos_via_search(
    client: GitHubClient,
    logins: list[str],
    emails: list[str],
    start: datetime,
    end: datetime,
    coverage: Coverage,
) -> set[str]:
    """Repositories holding commits by ``logins`` or ``emails`` *that this token
    can see*, via the commit Search API.

    This catches repositories ``/user/repos`` does not list (upstream projects
    the user contributed to). The Search API indexes default branches only;
    other branches are covered by repository scanning and pull requests.
    """
    queries = [(f"author:{login}", f"commits by {login}") for login in logins]
    queries += [(f"author-email:{email}", f"commits by {email}") for email in emails]
    names: set[str] = set()
    for base_query, what in queries:
        try:
            items = await search_all(
                client, "/search/commits", base_query, "author-date", start, end, coverage, what
            )
        except GitHubError as exc:
            coverage.warn(
                f"Searching {what} failed ({exc}); repositories found only through "
                "search may be missing."
            )
            continue
        found = {str((item.get("repository") or {}).get("full_name") or "") for item in items}
        found.discard("")
        if found:
            log.info("search found %d repo(s) with %s", len(found), what)
        names |= found
    return names


async def search_pull_requests(
    client: GitHubClient,
    login: str,
    start: datetime,
    end: datetime,
    coverage: Coverage,
) -> list[dict[str, Any]]:
    """Every pull request opened by ``login`` that this token can see, as
    issue-search items (see :func:`issue_repo_full_name`)."""
    try:
        return await search_all(
            client, "/search/issues", f"type:pr author:{login}", "created",
            start, end, coverage, f"pull requests by {login}",
        )
    except GitHubError as exc:
        coverage.warn(
            f"Searching pull requests by {login} failed ({exc}); pull requests in "
            "repositories the account is not a member of may be missing."
        )
        return []


def issue_repo_full_name(item: dict[str, Any]) -> str:
    """``owner/name`` of an issue-search item, from its ``repository_url``."""
    url = str(item.get("repository_url") or "")
    marker = "/repos/"
    return url.split(marker, 1)[1] if marker in url else ""


async def discover_org_repos(
    client: GitHubClient,
    org_login: str,
    discovered_by: str,
    coverage: Coverage | None = None,
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
        message = f"Could not list the repositories of organization {org_login} ({exc})."
        if coverage is not None:
            coverage.warn(message)
        else:
            log.warning(message)
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
            current.affiliated = current.affiliated or repo.affiliated
