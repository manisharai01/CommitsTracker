"""Statistics computation using pandas.

Everything is computed from real (timezone-aware) datetimes; string conversion
for export happens later in :mod:`github_contrib.exporters`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .logging_config import get_logger
from .models import CollectedData

log = get_logger("statistics")

TOP_N_REPOS = 25


@dataclass(slots=True)
class Statistics:
    """All derived tables plus the headline summary."""

    commits: pd.DataFrame
    pull_requests: pd.DataFrame
    repositories: pd.DataFrame
    organizations: pd.DataFrame
    commits_per_repo: pd.DataFrame
    commits_per_year: pd.DataFrame
    commits_per_month: pd.DataFrame
    commits_by_weekday: pd.DataFrame
    commits_by_email: pd.DataFrame
    commits_by_org: pd.DataFrame
    top_repositories: pd.DataFrame
    pr_summary: pd.DataFrame
    per_user: pd.DataFrame
    summary: pd.DataFrame
    lines_by_repo: pd.DataFrame          # additions/deletions/net per repo
    top_commits_by_impact: pd.DataFrame  # top commits ranked by lines changed
    summary_dict: dict[str, object] = field(default_factory=dict)


def _records_to_df(records: list, columns: list[str]) -> pd.DataFrame:
    """Build a DataFrame from ``.to_row()`` dataclasses with stable columns."""
    rows = [r.to_row() for r in records]
    if not rows:
        return pd.DataFrame(columns=columns)
    df = pd.DataFrame(rows)
    # Guarantee every expected column is present and ordered.
    for col in columns:
        if col not in df.columns:
            df[col] = pd.NA
    return df[columns]


_COMMIT_COLS = [
    "repository", "full_name", "owner", "organization", "sha",
    "author_login", "author_name", "author_email", "committer_name",
    "committer_email", "authored_date", "committed_date", "branch",
    "message_first_line", "message", "url",
    "additions", "deletions", "files_changed",
]
_PR_COLS = [
    "repository", "full_name", "organization", "number", "title",
    "author_login", "state", "effective_state", "merged", "created_at",
    "updated_at", "closed_at", "merged_at", "base_branch", "head_branch", "url",
]
_REPO_COLS = [
    "full_name", "name", "owner", "organization", "is_private", "is_fork",
    "is_archived", "default_branch", "language", "stargazers", "forks",
    "pushed_at", "created_at", "discovered_via", "description", "html_url",
]
_ORG_COLS = [
    "login", "name", "is_member", "repos_contributed", "commit_count",
    "pr_count", "merged_pr_count", "repo_names", "url",
]


def _commit_datetimes(data: CollectedData) -> pd.Series:
    """Series of timezone-aware authored datetimes (NaT for missing)."""
    if not data.commits:
        return pd.Series([], dtype="datetime64[ns, UTC]")
    values = [c.authored_date for c in data.commits]
    return pd.to_datetime(pd.Series(values), utc=True)


def compute_statistics(data: CollectedData) -> Statistics:
    """Compute every statistic table from collected data."""
    commits_df = _records_to_df(data.commits, _COMMIT_COLS)
    prs_df = _records_to_df(data.pull_requests, _PR_COLS)
    repos_df = _records_to_df(data.repos, _REPO_COLS)
    orgs_df = _records_to_df(data.organizations, _ORG_COLS)

    authored = _commit_datetimes(data)
    org_series = pd.Series([c.organization or "(personal)" for c in data.commits], dtype="object")
    email_series = pd.Series([c.author_email or "(unknown)" for c in data.commits], dtype="object")
    repo_series = pd.Series([c.full_name for c in data.commits], dtype="object")
    login_series = pd.Series([c.author_login for c in data.commits], dtype="object")

    # -- commits per repository -------------------------------------------
    if data.commits:
        per_repo = (
            pd.DataFrame({"full_name": repo_series, "organization": org_series})
            .groupby(["full_name", "organization"], dropna=False)
            .size()
            .reset_index(name="commits")
            .sort_values("commits", ascending=False, ignore_index=True)
        )
    else:
        per_repo = pd.DataFrame(columns=["full_name", "organization", "commits"])

    # -- commits per year / month -----------------------------------------
    valid_dates = authored.dropna()
    if not valid_dates.empty:
        per_year = (
            valid_dates.dt.year.astype(int).value_counts().sort_index()
            .rename_axis("year").reset_index(name="commits")
        )
        # strftime keeps the operation on tz-aware datetimes without the
        # "dropping timezone information" warning that to_period() raises.
        months = valid_dates.dt.strftime("%Y-%m")
        per_month = (
            months.value_counts().sort_index()
            .rename_axis("month").reset_index(name="commits")
        )
    else:
        per_year = pd.DataFrame(columns=["year", "commits"])
        per_month = pd.DataFrame(columns=["month", "commits"])

    # -- commits by weekday (Mon..Sun) ------------------------------------
    weekday_order = [
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
    ]
    if not valid_dates.empty:
        wd_counts = valid_dates.dt.day_name().value_counts()
        by_weekday = pd.DataFrame(
            {
                "weekday": weekday_order,
                "commits": [int(wd_counts.get(day, 0)) for day in weekday_order],
            }
        )
    else:
        by_weekday = pd.DataFrame(columns=["weekday", "commits"])

    # -- commits by author email ------------------------------------------
    if data.commits:
        by_email = (
            email_series.value_counts()
            .rename_axis("author_email").reset_index(name="commits")
        )
        by_login = (
            login_series.value_counts()
            .rename_axis("author_login").reset_index(name="commits")
        )
    else:
        by_email = pd.DataFrame(columns=["author_email", "commits"])
        by_login = pd.DataFrame(columns=["author_login", "commits"])

    # -- commits / PRs by organization ------------------------------------
    by_org = _commits_prs_by_org(data)

    # -- top repositories --------------------------------------------------
    top_repos = per_repo.head(TOP_N_REPOS).reset_index(drop=True)

    # -- PR summary --------------------------------------------------------
    pr_summary = _pr_summary(prs_df)

    # -- per-user breakdown ------------------------------------------------
    per_user = _per_user(data)

    # -- line-level impact tables ------------------------------------------
    lines_by_repo = _lines_by_repo(data)
    top_commits = _top_commits_by_impact(data)

    # -- headline summary --------------------------------------------------
    summary_dict = _summary_dict(data, authored)
    summary_df = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in summary_dict.items()]
    )

    return Statistics(
        commits=commits_df,
        pull_requests=prs_df,
        repositories=repos_df,
        organizations=orgs_df,
        commits_per_repo=per_repo,
        commits_per_year=per_year,
        commits_per_month=per_month,
        commits_by_weekday=by_weekday,
        commits_by_email=by_email,
        commits_by_org=by_org,
        top_repositories=top_repos,
        pr_summary=pr_summary,
        per_user=per_user,
        summary=summary_df,
        lines_by_repo=lines_by_repo,
        top_commits_by_impact=top_commits,
        summary_dict=summary_dict,
    )


def _commits_prs_by_org(data: CollectedData) -> pd.DataFrame:
    counts: dict[str, dict[str, int]] = {}

    def bucket(org: str) -> dict[str, int]:
        key = org or "(personal)"
        return counts.setdefault(key, {"commits": 0, "prs": 0, "merged_prs": 0})

    for commit in data.commits:
        bucket(commit.organization)["commits"] += 1
    for pr in data.pull_requests:
        b = bucket(pr.organization)
        b["prs"] += 1
        if pr.merged:
            b["merged_prs"] += 1

    if not counts:
        return pd.DataFrame(columns=["organization", "commits", "prs", "merged_prs"])
    df = pd.DataFrame(
        [{"organization": k, **v} for k, v in counts.items()]
    ).sort_values(["commits", "prs"], ascending=False, ignore_index=True)
    return df


def _pr_summary(prs_df: pd.DataFrame) -> pd.DataFrame:
    total = int(len(prs_df))
    if total == 0:
        merged = open_ = closed = 0
    else:
        merged = int(prs_df["merged"].fillna(False).astype(bool).sum())
        open_ = int((prs_df["state"] == "open").sum())
        closed_unmerged = int(((prs_df["state"] == "closed") & (~prs_df["merged"].fillna(False).astype(bool))).sum())
        closed = closed_unmerged
    rows = [
        {"metric": "total_pull_requests", "value": total},
        {"metric": "open_pull_requests", "value": open_},
        {"metric": "merged_pull_requests", "value": merged},
        {"metric": "closed_unmerged_pull_requests", "value": closed},
    ]
    return pd.DataFrame(rows)


def _per_user(data: CollectedData) -> pd.DataFrame:
    users: dict[str, dict[str, object]] = {}

    def row(login: str) -> dict[str, object]:
        return users.setdefault(
            login,
            {
                "author_login": login,
                "commits": 0,
                "pull_requests": 0,
                "merged_pull_requests": 0,
                "repositories": set(),
                "first_commit": None,
                "last_commit": None,
            },
        )

    for commit in data.commits:
        r = row(commit.author_login or "(unknown)")
        r["commits"] = int(r["commits"]) + 1  # type: ignore[arg-type]
        r["repositories"].add(commit.full_name)  # type: ignore[union-attr]
        dt = commit.authored_date
        if dt is not None:
            if r["first_commit"] is None or dt < r["first_commit"]:  # type: ignore[operator]
                r["first_commit"] = dt
            if r["last_commit"] is None or dt > r["last_commit"]:  # type: ignore[operator]
                r["last_commit"] = dt

    for pr in data.pull_requests:
        r = row(pr.author_login or "(unknown)")
        r["pull_requests"] = int(r["pull_requests"]) + 1  # type: ignore[arg-type]
        if pr.merged:
            r["merged_pull_requests"] = int(r["merged_pull_requests"]) + 1  # type: ignore[arg-type]

    if not users:
        return pd.DataFrame(
            columns=[
                "author_login", "commits", "pull_requests",
                "merged_pull_requests", "repositories",
                "first_commit", "last_commit",
            ]
        )

    rows = []
    for r in users.values():
        rows.append(
            {
                "author_login": r["author_login"],
                "commits": r["commits"],
                "pull_requests": r["pull_requests"],
                "merged_pull_requests": r["merged_pull_requests"],
                "repositories": len(r["repositories"]),  # type: ignore[arg-type]
                "first_commit": _iso_date(r["first_commit"]),
                "last_commit": _iso_date(r["last_commit"]),
            }
        )
    return pd.DataFrame(rows).sort_values("commits", ascending=False, ignore_index=True)


def _summary_dict(data: CollectedData, authored: pd.Series) -> dict[str, object]:
    contributed_repos = {c.full_name for c in data.commits} | {
        p.full_name for p in data.pull_requests
    }
    contributed_orgs = {c.organization for c in data.commits if c.organization} | {
        p.organization for p in data.pull_requests if p.organization
    }
    member_or_contrib_orgs = {o.login for o in data.organizations if o.login}

    merged = sum(1 for p in data.pull_requests if p.merged)
    open_prs = sum(1 for p in data.pull_requests if p.state == "open")

    valid_dates = authored.dropna()
    first_dt = valid_dates.min() if not valid_dates.empty else None
    last_dt = valid_dates.max() if not valid_dates.empty else None

    total_additions = sum(c.additions for c in data.commits)
    total_deletions = sum(c.deletions for c in data.commits)
    total_files_changed = sum(c.files_changed for c in data.commits)
    has_line_stats = total_additions > 0 or total_deletions > 0

    d: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tracked_users": ", ".join(sorted({c.author_login for c in data.commits} | {p.author_login for p in data.pull_requests})),
        "total_lifetime_commits": len(data.commits),
        "total_pull_requests": len(data.pull_requests),
        "merged_pull_requests": merged,
        "open_pull_requests": open_prs,
        "closed_unmerged_pull_requests": len(data.pull_requests) - merged - open_prs,
        "repositories_accessible": len(data.repos),
        "repositories_contributed_to": len(contributed_repos),
        "organizations_contributed_to": len(contributed_orgs),
        "organizations_total": len(member_or_contrib_orgs),
        "distinct_author_emails": len({c.author_email for c in data.commits if c.author_email}),
        "first_contribution_date": _iso_date(first_dt),
        "latest_contribution_date": _iso_date(last_dt),
    }
    if has_line_stats:
        d["total_lines_added"] = total_additions
        d["total_lines_deleted"] = total_deletions
        d["net_lines"] = total_additions - total_deletions
        d["total_files_changed"] = total_files_changed
        avg = round(total_additions / len(data.commits), 1) if data.commits else 0.0
        d["avg_lines_added_per_commit"] = avg
    return d


def _lines_by_repo(data: CollectedData) -> pd.DataFrame:
    """Additions, deletions, net lines and files changed grouped by repository."""
    cols = ["full_name", "organization", "commits", "additions", "deletions", "net_lines", "files_changed"]
    if not data.commits or not any(c.additions or c.deletions for c in data.commits):
        return pd.DataFrame(columns=cols)
    buckets: dict[str, dict[str, object]] = {}
    for c in data.commits:
        key = c.full_name
        b = buckets.setdefault(key, {
            "full_name": key,
            "organization": c.organization or "(personal)",
            "commits": 0, "additions": 0, "deletions": 0, "files_changed": 0,
        })
        b["commits"] = int(b["commits"]) + 1  # type: ignore[arg-type]
        b["additions"] = int(b["additions"]) + c.additions  # type: ignore[arg-type]
        b["deletions"] = int(b["deletions"]) + c.deletions  # type: ignore[arg-type]
        b["files_changed"] = int(b["files_changed"]) + c.files_changed  # type: ignore[arg-type]
    rows = []
    for b in buckets.values():
        rows.append({**b, "net_lines": int(b["additions"]) - int(b["deletions"])})  # type: ignore[arg-type]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values("additions", ascending=False, ignore_index=True)


def _top_commits_by_impact(data: CollectedData, top: int = 20) -> pd.DataFrame:
    """Top commits ranked by total lines changed (additions + deletions)."""
    cols = ["sha", "repository", "author_login", "authored_date",
            "additions", "deletions", "total_changes", "files_changed", "message_first_line", "url"]
    commits_with_stats = [c for c in data.commits if c.additions or c.deletions]
    if not commits_with_stats:
        return pd.DataFrame(columns=cols)
    rows = [
        {
            "sha": c.sha[:8],
            "repository": c.full_name,
            "author_login": c.author_login,
            "authored_date": _iso_date(c.authored_date),
            "additions": c.additions,
            "deletions": c.deletions,
            "total_changes": c.additions + c.deletions,
            "files_changed": c.files_changed,
            "message_first_line": c.message_first_line[:80],
            "url": c.url,
        }
        for c in commits_with_stats
    ]
    df = pd.DataFrame(rows, columns=cols)
    return df.sort_values("total_changes", ascending=False, ignore_index=True).head(top)


def _iso_date(value) -> str:
    if value is None:
        return ""
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return ""
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
