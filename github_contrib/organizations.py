"""Organization contribution aggregation.

An organization's contribution figures are derived from the commits and pull
requests collected elsewhere, then merged with the membership information found
during discovery so that organizations with zero recorded contributions still
appear (with zero counts)."""

from __future__ import annotations

from collections.abc import Iterable

from .commitstore import CommitStore
from .logging_config import get_logger
from .models import CommitRecord, OrgRecord, PullRequestRecord, RepoRecord

log = get_logger("organizations")


def aggregate_organizations(
    repos: list[RepoRecord],
    commits: Iterable[CommitRecord],
    pull_requests: list[PullRequestRecord],
    member_orgs: list[OrgRecord],
) -> list[OrgRecord]:
    """Build per-organization contribution records."""
    orgs: dict[str, OrgRecord] = {}

    # Seed with membership info.
    for org in member_orgs:
        if not org.login:
            continue
        orgs[org.login] = OrgRecord(
            login=org.login,
            name=org.name,
            url=org.url,
            is_member=True,
        )

    repo_names: dict[str, set[str]] = {}

    def _record(login: str) -> OrgRecord:
        org = orgs.get(login)
        if org is None:
            org = OrgRecord(login=login)
            orgs[login] = org
        repo_names.setdefault(login, set())
        return org

    if isinstance(commits, CommitStore):  # counted by the store, not read
        for login, full_name, n in commits.group_counts(
            "organization", "full_name", where="organization is not null and organization != ''"
        ):
            org = _record(login)
            org.commit_count += n
            repo_names[login].add(full_name)
    else:
        for commit in commits:
            if not commit.organization:
                continue
            org = _record(commit.organization)
            org.commit_count += 1
            repo_names[commit.organization].add(commit.full_name)

    for pr in pull_requests:
        if not pr.organization:
            continue
        org = _record(pr.organization)
        org.pr_count += 1
        if pr.merged:
            org.merged_pr_count += 1
        repo_names[pr.organization].add(pr.full_name)

    for login, names in repo_names.items():
        org = orgs[login]
        org.repo_names = sorted(names)
        org.repos_contributed = len(names)

    result = sorted(
        orgs.values(),
        key=lambda o: (o.commit_count + o.pr_count, o.login),
        reverse=True,
    )
    log.info("aggregated %d organization(s)", len(result))
    return result
