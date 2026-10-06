"""Report-time filtering of collected data.

Everything here runs on collected (or ``--regen``-loaded) data before any
statistic is computed, so every output tells the same story:

1. **Owner exclusions** — a personal access token discovers *everything* the
   account can reach, including its own personal repositories. When the report
   documents work for a company, ``apply_owner_exclusions`` drops repositories
   (with their commits / pull requests) owned by excluded logins.
2. **Reporting period** — ``apply_date_range`` keeps commits by author date and
   pull requests by creation date inside ``[since, until]``.
3. **One count per piece of work** — ``dedupe_contributions`` removes copies:
   the same commit (SHA) in a fork and its upstream, rebased or cherry-picked
   copies of a commit on other branches, and the original commits of a pull
   request that was squash-merged (the squash commit stands for them).
4. **Time zone** — ``localize`` expresses every date in the report's time zone,
   so days, weeks and months match the author's calendar.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, tzinfo

from .config import AppConfig
from .logging_config import get_logger
from .models import CollectedData, CommitRecord, PullRequestRecord, RepoRecord
from .organizations import aggregate_organizations

log = get_logger("filters")


def _owner_of(full_name: str, owner: str = "") -> str:
    """Lower-cased owner login for a repo (falls back to the full_name prefix)."""
    if owner:
        return owner.lower()
    return full_name.split("/", 1)[0].lower() if "/" in full_name else full_name.lower()


def _member_orgs(data: CollectedData, excluded: set[str] = frozenset()) -> list:
    return [o for o in data.organizations if o.is_member and o.login.lower() not in excluded]


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
    organizations = aggregate_organizations(repos, commits, prs, _member_orgs(data, owners))

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
    filtered = replace(data, repos=repos, commits=commits, pull_requests=prs,
                       organizations=organizations)
    return filtered, removed


def apply_date_range(
    data: CollectedData,
    since: datetime | None,
    until: datetime | None,
) -> tuple[CollectedData, dict[str, int]]:
    """Keep commits authored and pull requests opened within ``[since, until]``.

    Both bounds are inclusive and timezone aware. Records without a date
    cannot be placed in the period and are dropped while a period is set.
    """
    if since is None and until is None:
        return data, {"commits": 0, "pull_requests": 0}

    def inside(moment: datetime | None) -> bool:
        return (
            moment is not None
            and (since is None or moment >= since)
            and (until is None or moment <= until)
        )

    commits = [c for c in data.commits if inside(c.authored_date)]
    prs = [p for p in data.pull_requests if inside(p.created_at)]
    removed = {
        "commits": len(data.commits) - len(commits),
        "pull_requests": len(data.pull_requests) - len(prs),
    }
    organizations = aggregate_organizations(data.repos, commits, prs, _member_orgs(data))
    return replace(data, commits=commits, pull_requests=prs, organizations=organizations), removed


@dataclass(slots=True)
class DedupeResult:
    """How many commit records were folded into another record."""

    same_commit: int = 0  # one SHA found in several repositories (fork + upstream, mirror)
    rebased_copies: int = 0  # rebased / cherry-picked copies of a commit on other branches
    squashed: int = 0  # original commits of a squash-merged pull request

    @property
    def total(self) -> int:
        return self.same_commit + self.rebased_copies + self.squashed


_FAR_FUTURE = datetime.max.replace(tzinfo=timezone.utc)


def dedupe_contributions(data: CollectedData) -> tuple[CollectedData, DedupeResult]:
    """Count every piece of work once.

    * **Same commit** — one SHA in several repositories (a fork and its
      upstream, a mirror) is the same commit; the copy in a non-fork
      repository, on its default branch, is kept.
    * **Rebased / cherry-picked copies** — within one repository, commits with
      the same author, the same author timestamp (to the second) and the same
      subject line are copies of one change (rebase and cherry-pick preserve
      all three, so do GitHub's "Rebase and merge"); the copy on the default
      branch, else the earliest committed one, is kept.
    * **Squash merges** — when a merged pull request's squash commit is
      present, the pull request's original commits are folded into it,
      matching what landed (and GitHub's own contribution count).
    """
    result = DedupeResult()
    repos = {r.full_name: r for r in data.repos}

    def on_default(commit: CommitRecord) -> bool:
        repo = repos.get(commit.full_name)
        return repo is not None and commit.branch == repo.default_branch

    def is_fork(commit: CommitRecord) -> bool:
        repo = repos.get(commit.full_name)
        return bool(repo and repo.is_fork)

    def preference(commit: CommitRecord) -> tuple:
        return (
            is_fork(commit),
            not on_default(commit),
            commit.committed_date or _FAR_FUTURE,
            commit.full_name,
            commit.sha,
        )

    commits = list(data.commits)

    # 1. The same SHA in several repositories.
    best: dict[str, CommitRecord] = {}
    for commit in commits:
        if not commit.sha:
            continue
        current = best.get(commit.sha)
        if current is None or preference(commit) < preference(current):
            best[commit.sha] = commit
    kept = [c for c in commits if not c.sha or best[c.sha] is c]
    result.same_commit = len(commits) - len(kept)
    commits = kept

    # 2. Rebased / cherry-picked copies within a repository.
    def identity(commit: CommitRecord) -> tuple | None:
        if commit.authored_date is None:
            return None
        author = (commit.author_email or commit.author_login).lower()
        return (
            commit.full_name,
            author,
            commit.authored_date.replace(microsecond=0),
            commit.message_first_line.strip(),
        )

    best_copy: dict[tuple, CommitRecord] = {}
    for commit in commits:
        key = identity(commit)
        if key is None:
            continue
        current = best_copy.get(key)
        if current is None or preference(commit) < preference(current):
            best_copy[key] = commit
    kept = [c for c in commits if identity(c) is None or best_copy[identity(c)] is c]
    result.rebased_copies = len(commits) - len(kept)
    commits = kept

    # 3. Originals of squash-merged pull requests.
    present = {(c.full_name, c.sha): c for c in commits}
    folded: set[tuple[str, str]] = set()
    for pr in data.pull_requests:
        squash = present.get((pr.full_name, pr.merge_commit_sha)) if pr.merge_commit_sha else None
        if (
            squash is None
            or not pr.commit_shas
            or squash.parent_count != 1  # a real merge commit keeps the originals in history
            or pr.merge_commit_sha in pr.commit_shas  # fast-forward: the originals landed as-is
        ):
            continue
        folded |= {(pr.full_name, sha) for sha in pr.commit_shas if (pr.full_name, sha) in present}
    kept = [c for c in commits if (c.full_name, c.sha) not in folded]
    result.squashed = len(commits) - len(kept)
    commits = kept

    if result.total:
        log.info(
            "counted each change once: -%d same commit in another repo, -%d rebased/"
            "cherry-picked copies, -%d squash-merged originals",
            result.same_commit, result.rebased_copies, result.squashed,
        )
    organizations = aggregate_organizations(
        data.repos, commits, data.pull_requests, _member_orgs(data)
    )
    return replace(data, commits=commits, organizations=organizations), result


def _local(moment: datetime | None, tz: tzinfo) -> datetime | None:
    return moment.astimezone(tz) if moment is not None else None


def localize(data: CollectedData, tz: tzinfo) -> CollectedData:
    """``data`` with every timestamp expressed in ``tz`` (same instants)."""
    commits = [
        replace(c, authored_date=_local(c.authored_date, tz), committed_date=_local(c.committed_date, tz))
        for c in data.commits
    ]
    prs: list[PullRequestRecord] = [
        replace(
            p,
            created_at=_local(p.created_at, tz),
            updated_at=_local(p.updated_at, tz),
            closed_at=_local(p.closed_at, tz),
            merged_at=_local(p.merged_at, tz),
        )
        for p in data.pull_requests
    ]
    repos: list[RepoRecord] = [
        replace(r, pushed_at=_local(r.pushed_at, tz), created_at=_local(r.created_at, tz))
        for r in data.repos
    ]
    return replace(data, commits=commits, pull_requests=prs, repos=repos)


@dataclass(slots=True)
class ReportScope:
    """What the report covers and how it was compiled (shown in the report)."""

    since: datetime | None = None
    until: datetime | None = None
    timezone: str = "UTC"
    excluded_owners: list[str] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    outside_period: dict[str, int] = field(default_factory=dict)
    duplicates: DedupeResult = field(default_factory=DedupeResult)
    merge_commits: int = 0
    commits_without_line_stats: int = 0
    collection: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @property
    def has_period(self) -> bool:
        return self.since is not None or self.until is not None


def prepare_report_data(
    data: CollectedData, config: AppConfig
) -> tuple[CollectedData, ReportScope]:
    """Run every report-time step in order and describe what was done."""
    owners = excluded_owner_set(config, data)
    data, excluded = apply_owner_exclusions(data, owners)
    data, outside = apply_date_range(data, config.since, config.until)
    data, duplicates = dedupe_contributions(data)
    data = localize(data, config.tz)

    collection = dict(data.meta)
    stats_requested = bool(collection.get("fetch_commit_stats", config.fetch_commit_stats))
    scope = ReportScope(
        since=config.since,
        until=config.until,
        timezone=config.timezone_name,
        excluded_owners=sorted(owners),
        excluded=excluded,
        outside_period=outside,
        duplicates=duplicates,
        merge_commits=sum(1 for c in data.commits if c.is_merge),
        commits_without_line_stats=sum(
            1 for c in data.commits if stats_requested and not c.is_merge and not c.stats_fetched
        ),
        collection=collection,
        notes=list(data.notes),
    )
    return data, scope
