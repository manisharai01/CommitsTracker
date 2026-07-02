"""Report-time filtering of collected data.

The main use case: a personal access token discovers *everything* the account
can reach — including the account's own personal repositories.  When the report
is meant to document work done for a company (repos hosted in organizations or
on colleagues' accounts), the personal repos owned by the tracked login itself
should not appear.  ``apply_owner_exclusions`` drops every repository (and its
commits / pull requests) whose owner login is in the exclusion set, then
re-aggregates the per-organization figures so all outputs stay consistent.
"""

from __future__ import annotations

from .config import AppConfig
from .logging_config import get_logger
from .models import CollectedData
from .organizations import aggregate_organizations

log = get_logger("filters")


def _owner_of(full_name: str, owner: str = "") -> str:
    """Lower-cased owner login for a repo (falls back to the full_name prefix)."""
    if owner:
        return owner.lower()
    return full_name.split("/", 1)[0].lower() if "/" in full_name else full_name.lower()


def excluded_owner_set(config: AppConfig, data: CollectedData) -> set[str]:
    """The set of repo-owner logins (lower-cased) to drop from the outputs.

    * ``config.exclude_owners`` entries are always excluded.
    * With ``config.exclude_own_repos`` the tracked logins themselves are
      excluded (their personal repos).  When no target logins are known (e.g.
      ``--regen`` without ``--user``) the author logins found in the collected
      commits/PRs are used instead — those are the tracked users by construction.
    """
    owners = {o.lower() for o in config.exclude_owners if o}
    if config.exclude_own_repos:
        if config.target_logins:
            owners |= {login.lower() for login in config.target_logins}
        else:
            owners |= {c.author_login.lower() for c in data.commits if c.author_login}
            owners |= {p.author_login.lower() for p in data.pull_requests if p.author_login}
    return owners


def apply_owner_exclusions(
    data: CollectedData, owners: set[str]
) -> tuple[CollectedData, dict[str, int]]:
    """Return ``data`` without any repository owned by a login in ``owners``.

    Organizations are re-aggregated from the filtered commits/PRs so their
    counts match the rest of the report.  The second return value reports how
    much was removed (for logging / the CLI summary).
    """
    if not owners:
        return data, {"repos": 0, "commits": 0, "pull_requests": 0}

    repos = [r for r in data.repos if _owner_of(r.full_name, r.owner) not in owners]
    commits = [c for c in data.commits if _owner_of(c.full_name, c.owner) not in owners]
    prs = [p for p in data.pull_requests if _owner_of(p.full_name) not in owners]

    member_orgs = [
        o for o in data.organizations if o.is_member and o.login.lower() not in owners
    ]
    organizations = aggregate_organizations(repos, commits, prs, member_orgs)

    removed = {
        "repos": len(data.repos) - len(repos),
        "commits": len(data.commits) - len(commits),
        "pull_requests": len(data.pull_requests) - len(prs),
    }
    if any(removed.values()):
        log.info(
            "excluded personal repos owned by %s: -%d repo(s), -%d commit(s), -%d PR(s)",
            ", ".join(sorted(owners)),
            removed["repos"],
            removed["commits"],
            removed["pull_requests"],
        )
    filtered = CollectedData(
        repos=repos, commits=commits, pull_requests=prs, organizations=organizations
    )
    return filtered, removed
