"""Typed data models for the collected GitHub data.

All datetimes are stored as timezone-aware :class:`datetime.datetime` objects
(UTC).  Conversion to strings happens only at export time so that statistics
can operate on real datetimes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from typing import Any

from .logging_config import get_logger


def parse_github_datetime(value: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp returned by the GitHub API.

    Returns ``None`` for missing/empty values.  The result is always timezone
    aware (UTC).
    """
    if not value:
        return None
    text = value.strip()
    if not text:
        return None
    # Python's fromisoformat handles a trailing 'Z' from 3.11+, but normalise
    # defensively so behaviour is identical across patch releases.
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _iso(value: Any) -> Any:
    """Render datetimes as ISO strings, leave everything else untouched."""
    if isinstance(value, datetime):
        return value.isoformat()
    return value


@dataclass(slots=True)
class RepoRecord:
    """A repository accessible to at least one tracked account."""

    full_name: str
    name: str
    owner: str
    organization: str  # owner login when the owner is an organization, else ""
    is_private: bool = False
    is_fork: bool = False
    is_archived: bool = False
    default_branch: str = "main"
    html_url: str = ""
    description: str = ""
    language: str = ""
    stargazers: int = 0
    forks: int = 0
    pushed_at: datetime | None = None
    created_at: datetime | None = None
    discovered_via: set[str] = field(default_factory=set)
    # True when a tracked account can reach the repo as owner, collaborator or
    # organization member (or it was named with --repo / --org). Repos found
    # only through the Search API are upstream projects the user contributed
    # to by pull request: only their default branch and the user's pull
    # requests hold the user's commits, so their other branches are not scanned.
    affiliated: bool = True

    def to_row(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "name": self.name,
            "owner": self.owner,
            "organization": self.organization,
            "is_private": self.is_private,
            "is_fork": self.is_fork,
            "is_archived": self.is_archived,
            "default_branch": self.default_branch,
            "language": self.language,
            "stargazers": self.stargazers,
            "forks": self.forks,
            "pushed_at": _iso(self.pushed_at),
            "created_at": _iso(self.created_at),
            "discovered_via": ",".join(sorted(self.discovered_via)),
            "affiliated": self.affiliated,
            "description": self.description,
            "html_url": self.html_url,
        }


@dataclass(slots=True)
class CommitRecord:
    """A single commit authored by a tracked user in a repository."""

    repository: str
    full_name: str
    owner: str
    organization: str
    sha: str
    message: str
    message_first_line: str
    author_login: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    authored_date: datetime | None
    committed_date: datetime | None
    branch: str
    url: str
    # Line-level stats — populated by a second-pass enrichment after collection.
    # ``stats_fetched`` tells a genuine zero apart from "not fetched"; merge
    # commits are never fetched (their diff repeats other people's work).
    additions: int = 0
    deletions: int = 0
    files_changed: int = 0
    stats_fetched: bool = False
    # Number of parent commits: 0 = unknown (older data), 2+ = merge commit.
    parent_count: int = 0

    @property
    def is_merge(self) -> bool:
        return self.parent_count > 1

    def to_row(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "full_name": self.full_name,
            "owner": self.owner,
            "organization": self.organization,
            "sha": self.sha,
            "author_login": self.author_login,
            "author_name": self.author_name,
            "author_email": self.author_email,
            "committer_name": self.committer_name,
            "committer_email": self.committer_email,
            "authored_date": _iso(self.authored_date),
            "committed_date": _iso(self.committed_date),
            "branch": self.branch,
            "message_first_line": self.message_first_line,
            "message": self.message,
            "url": self.url,
            "additions": self.additions,
            "deletions": self.deletions,
            "files_changed": self.files_changed,
            "stats_fetched": self.stats_fetched,
            "parent_count": self.parent_count,
        }


@dataclass(slots=True)
class PullRequestRecord:
    """A pull request opened by a tracked user."""

    repository: str
    full_name: str
    organization: str
    number: int
    title: str
    author_login: str
    state: str  # "open" | "closed"
    merged: bool
    created_at: datetime | None
    updated_at: datetime | None
    closed_at: datetime | None
    merged_at: datetime | None
    base_branch: str
    head_branch: str
    url: str
    # The commit GitHub created when merging (a squash/rebase/merge commit).
    merge_commit_sha: str = ""
    # SHAs of the pull request's own commits (fetched when every branch is
    # scanned); lets squash-merged originals be counted once.
    commit_shas: list[str] = field(default_factory=list)

    @property
    def effective_state(self) -> str:
        """open / merged / closed (merged takes precedence over closed)."""
        if self.merged:
            return "merged"
        return self.state

    def to_row(self) -> dict[str, Any]:
        return {
            "repository": self.repository,
            "full_name": self.full_name,
            "organization": self.organization,
            "number": self.number,
            "title": self.title,
            "author_login": self.author_login,
            "state": self.state,
            "effective_state": self.effective_state,
            "merged": self.merged,
            "created_at": _iso(self.created_at),
            "updated_at": _iso(self.updated_at),
            "closed_at": _iso(self.closed_at),
            "merged_at": _iso(self.merged_at),
            "base_branch": self.base_branch,
            "head_branch": self.head_branch,
            "url": self.url,
            "merge_commit_sha": self.merge_commit_sha,
            "commit_shas": " ".join(self.commit_shas),
        }


@dataclass(slots=True)
class OrgRecord:
    """An organization the tracked users belong to or have contributed to."""

    login: str
    name: str = ""
    url: str = ""
    is_member: bool = False
    repos_contributed: int = 0
    repo_names: list[str] = field(default_factory=list)
    commit_count: int = 0
    pr_count: int = 0
    merged_pr_count: int = 0

    def to_row(self) -> dict[str, Any]:
        return {
            "login": self.login,
            "name": self.name,
            "is_member": self.is_member,
            "repos_contributed": self.repos_contributed,
            "commit_count": self.commit_count,
            "pr_count": self.pr_count,
            "merged_pr_count": self.merged_pr_count,
            "repo_names": ",".join(sorted(self.repo_names)),
            "url": self.url,
        }


@dataclass(slots=True)
class CollectedData:
    """The complete result of a collection run."""

    repos: list[RepoRecord] = field(default_factory=list)
    commits: list[CommitRecord] = field(default_factory=list)
    pull_requests: list[PullRequestRecord] = field(default_factory=list)
    organizations: list[OrgRecord] = field(default_factory=list)
    # Everything that may make the data incomplete (failed requests, partial
    # search results, ...). Shown in the report so gaps are never silent.
    notes: list[str] = field(default_factory=list)
    # How the data was collected (options, period, time) — saved next to the
    # CSVs as collection.json so --regen describes the report accurately.
    meta: dict[str, Any] = field(default_factory=dict)


class Coverage:
    """Collects data-completeness notes during a run (logged at WARNING)."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def warn(self, message: str) -> None:
        if message not in self.notes:
            self.notes.append(message)
            get_logger("coverage").warning(message)


__all__ = [
    "parse_github_datetime",
    "RepoRecord",
    "CommitRecord",
    "PullRequestRecord",
    "OrgRecord",
    "CollectedData",
    "Coverage",
    "asdict",
    "fields",
]
