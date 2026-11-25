"""Statistics computation using pandas.

Everything is computed from real (timezone-aware) datetimes; string conversion
for export happens later in :mod:`github_contrib.exporters`.
"""

from __future__ import annotations

import heapq
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from .analytics import WEEKDAYS, WORK_TYPES, Analytics
from .logging_config import get_logger
from .models import CollectedData, CommitRecord

log = get_logger("statistics")

TOP_N_REPOS = 25
#: Years and months without commits are listed (as 0) across at most this
#: many years: a commit dated decades off by a broken clock shouldn't add
#: hundreds of empty rows.
_MAX_FILLED_YEARS = 30


def _month_range(first: str, last: str) -> list[str]:
    """Every "YYYY-MM" from ``first`` to ``last``, inclusive."""
    year, month = int(first[:4]), int(first[5:7])
    end = (int(last[:4]), int(last[5:7]))
    out: list[str] = []
    while (year, month) <= end:
        out.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


@dataclass(slots=True)
class Statistics:
    """All derived tables plus the headline summary."""

    commits: CommitTable  # every commit, read row by row
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
    # Insights & analytics (see analytics.py; attached by attach_analytics).
    analytics: Analytics | None = None
    highlights: pd.DataFrame = field(default_factory=pd.DataFrame)
    quarterly: pd.DataFrame = field(default_factory=pd.DataFrame)
    work_mix: pd.DataFrame = field(default_factory=pd.DataFrame)
    pr_delivery: pd.DataFrame = field(default_factory=pd.DataFrame)
    working_rhythm: pd.DataFrame = field(default_factory=pd.DataFrame)
    technologies: pd.DataFrame = field(default_factory=pd.DataFrame)
    reviews: pd.DataFrame = field(default_factory=pd.DataFrame)  # code reviews given (reviews.csv)


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
    "additions", "deletions", "files_changed", "stats_fetched", "parent_count",
]
_PR_COLS = [
    "repository", "full_name", "organization", "number", "title",
    "author_login", "state", "effective_state", "merged", "created_at",
    "updated_at", "closed_at", "merged_at", "base_branch", "head_branch", "url",
    "merge_commit_sha", "commit_shas", "additions", "deletions", "changed_files",
]
_REPO_COLS = [
    "full_name", "name", "owner", "organization", "is_private", "is_fork",
    "is_archived", "default_branch", "language", "stargazers", "forks",
    "pushed_at", "created_at", "discovered_via", "affiliated", "description", "html_url",
    "languages", "technologies", "stack_read",
]
_REVIEW_COLS = [
    "reviewer", "full_name", "owner", "organization", "pr_number", "pr_title", "pr_author",
    "state", "submitted_at", "url",
]
_ORG_COLS = [
    "login", "name", "is_member", "repos_contributed", "commit_count",
    "pr_count", "merged_pr_count", "repo_names", "url",
]


class CommitTable:
    """The report's commits as a table read row by row - never held whole.

    It stands where a DataFrame of every commit used to be: that took about
    1 KB per commit, and an account can have any number of commits. The
    exporters and the HTML report stream its rows; ``source`` is the list or
    :class:`~github_contrib.commitstore.CommitStore` they come from.
    """

    columns: list[str] = _COMMIT_COLS

    def __init__(self, source) -> None:
        self.source = source

    def __len__(self) -> int:
        return len(self.source)

    @property
    def empty(self) -> bool:
        return len(self.source) == 0

    def rows(self):
        """Each commit as a list of values in :attr:`columns` order."""
        for commit in self.source:
            row = commit.to_row()
            yield [row[column] for column in _COMMIT_COLS]


#: Weekday names in English, whatever the server's locale (as pandas gives them).
_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _object_frame(columns: dict[str, list]) -> pd.DataFrame:
    """A DataFrame whose text columns are plain Python objects (as built from
    a Series of objects), so tables made from counts match the old ones."""
    return pd.DataFrame({
        name: pd.Series(values, dtype="object") if values and isinstance(values[0], str) else values
        for name, values in columns.items()
    })


def compute_statistics(data: CollectedData) -> Statistics:
    """Compute every statistic table from collected data.

    The commits are read in streaming passes that keep counts per
    repository, year, month, weekday, email and the like: memory grows with
    those, not with the number of commits.
    """
    prs_df = _records_to_df(data.pull_requests, _PR_COLS)
    # A size that wasn't recorded stays blank (and recorded ones stay "12", not "12.0").
    for column in ("additions", "deletions", "changed_files"):
        prs_df[column] = pd.array(
            [None if pd.isna(v) else int(v) for v in prs_df[column].tolist()], dtype="Int64"
        )
    repos_df = _records_to_df(data.repos, _REPO_COLS)
    orgs_df = _records_to_df(data.organizations, _ORG_COLS)

    repo_counts: Counter[tuple[str, str]] = Counter()
    email_counts: Counter[str] = Counter()
    year_counts: Counter[int] = Counter()
    month_counts: Counter[str] = Counter()
    weekday_counts: Counter[str] = Counter()
    zone = None  # years, months and weekdays in the zone of the data (the first dated commit's)
    first_dt = last_dt = None
    n_commits = 0
    for commit in data.commits:
        n_commits += 1
        repo_counts[(commit.full_name, commit.organization or "(personal)")] += 1
        email_counts[commit.author_email or "(unknown)"] += 1
        moment = commit.authored_date
        if moment is None:
            continue
        if zone is None:
            zone = moment.tzinfo
        local = moment.astimezone(zone) if zone is not None else moment
        if first_dt is None or local < first_dt:
            first_dt = local
        if last_dt is None or local > last_dt:
            last_dt = local
        year_counts[local.year] += 1
        month_counts[f"{local.year:04d}-{local.month:02d}"] += 1
        weekday_counts[_WEEKDAY_NAMES[local.weekday()]] += 1

    # -- commits per repository -------------------------------------------
    if n_commits:
        keys = sorted(repo_counts)  # as groupby orders them
        per_repo = _object_frame({
            "full_name": [k[0] for k in keys],
            "organization": [k[1] for k in keys],
            "commits": [repo_counts[k] for k in keys],
        }).sort_values("commits", ascending=False, ignore_index=True)
    else:
        per_repo = pd.DataFrame(columns=["full_name", "organization", "commits"])

    # -- commits per year / month -----------------------------------------
    if year_counts:
        # Quiet years and months are listed with 0: left out, a chart would
        # draw June and October as neighbours.
        first_year, last_year = min(year_counts), max(year_counts)
        years = range(first_year, last_year + 1) if last_year - first_year <= _MAX_FILLED_YEARS else sorted(year_counts)
        per_year = pd.DataFrame({"year": list(years), "commits": [int(year_counts.get(y, 0)) for y in years]})
        if last_year - first_year <= _MAX_FILLED_YEARS:
            month_keys = _month_range(min(month_counts), max(month_counts))
        else:  # a bogus date decades away: list only the months with commits
            month_keys = sorted(month_counts)
        per_month = pd.DataFrame({"month": month_keys, "commits": [int(month_counts.get(m, 0)) for m in month_keys]})
    else:
        per_year = pd.DataFrame(columns=["year", "commits"])
        per_month = pd.DataFrame(columns=["month", "commits"])

    # -- commits by weekday (Mon..Sun) ------------------------------------
    if year_counts:
        by_weekday = pd.DataFrame(
            {"weekday": list(_WEEKDAY_NAMES), "commits": [int(weekday_counts.get(day, 0)) for day in _WEEKDAY_NAMES]}
        )
    else:
        by_weekday = pd.DataFrame(columns=["weekday", "commits"])

    # -- commits by author email ------------------------------------------
    if n_commits:
        # Counts in order of first appearance, sorted as value_counts sorts them.
        by_email = (
            pd.Series(list(email_counts.values()), index=pd.Index(list(email_counts), dtype="object"))
            .sort_values(ascending=False)
            .rename_axis("author_email").reset_index(name="commits")
        )
    else:
        by_email = pd.DataFrame(columns=["author_email", "commits"])

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
    summary_dict = _summary_dict(data, first_dt, last_dt)
    summary_df = pd.DataFrame(
        [{"metric": k, "value": v} for k, v in summary_dict.items()]
    )

    return Statistics(
        commits=CommitTable(data.commits),
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
        reviews=_records_to_df(data.reviews, _REVIEW_COLS),
    )


def attach_analytics(stats: Statistics, analytics: Analytics) -> None:
    """Keep ``analytics`` on ``stats`` (for the reports), and as tables for
    the Excel workbook."""
    stats.analytics = analytics
    stats.highlights = pd.DataFrame({"highlight": analytics.highlights}, dtype="object")
    stats.quarterly = pd.DataFrame(
        [
            {
                "quarter": q.label,
                "partial": q.partial,
                "commits": q.commits,
                "change_vs_previous_pct": round(q.change * 100, 1) if q.change is not None else None,
                "prs_opened": q.prs_opened,
                "prs_merged": q.prs_merged,
                "code_reviews": q.reviews,
                "repositories": q.repositories,
                "active_days": q.active_days,
                "lines_added": q.additions,
                "lines_deleted": q.deletions,
                "shipped": "; ".join(
                    f"{text} ({repo}{', ' + ref if ref else ''})" for text, repo, ref in q.shipped
                ),
            }
            for q in analytics.quarters
        ]
    )
    mix = analytics.work_mix
    stats.work_mix = pd.DataFrame(
        [
            {"work_type": kind, "commits": mix.counts[kind], "share_pct": round(mix.share(kind) * 100, 1)}
            for kind in WORK_TYPES
        ]
        + [{"work_type": "Merge commits (not counted in the shares)", "commits": mix.merges, "share_pct": None}]
    )
    rows: list[dict[str, object]] = []
    delivery = analytics.delivery
    if delivery is not None:
        rows += [{"measure": "Time to merge", "bucket": b, "pull_requests": n} for b, n in delivery.time_buckets]
        rows += [{"measure": "Size of merged PRs", "bucket": b, "pull_requests": n} for b, n in delivery.size_buckets]
    stats.pr_delivery = pd.DataFrame(rows, columns=["measure", "bucket", "pull_requests"])
    rhythm = analytics.rhythm
    if rhythm is not None:
        stats.working_rhythm = pd.DataFrame(
            [[WEEKDAYS[day], *rhythm.heatmap[day]] for day in range(7)],
            columns=["weekday", *(f"{hour:02d}:00" for hour in range(24))],
        )
    stack = analytics.stack
    stats.technologies = pd.DataFrame(
        [
            {
                "category": use.category,
                "technology": use.name,
                "repositories": len(use.repositories),
                "commits_in_those_repositories": use.commits,
                "last_commit": use.last_used.isoformat() if use.last_used else "",
                "repository_names": ", ".join(use.repositories),
            }
            for use in ([*stack.languages, *stack.technologies] if stack is not None else [])
        ],
        columns=["category", "technology", "repositories", "commits_in_those_repositories",
                 "last_commit", "repository_names"],
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


def _summary_dict(data: CollectedData, first_dt, last_dt) -> dict[str, object]:
    """The headline numbers (one streaming pass over the commits)."""
    contributed_repos = {p.full_name for p in data.pull_requests}
    contributed_orgs = {p.organization for p in data.pull_requests if p.organization}
    member_or_contrib_orgs = {o.login for o in data.organizations if o.login}

    merged = sum(1 for p in data.pull_requests if p.merged)
    open_prs = sum(1 for p in data.pull_requests if p.state == "open")

    n_commits = total_additions = total_deletions = total_files_changed = measured = 0
    emails: set[str] = set()
    for c in data.commits:
        n_commits += 1
        contributed_repos.add(c.full_name)
        if c.organization:
            contributed_orgs.add(c.organization)
        if c.author_email:
            emails.add(c.author_email)
        total_additions += c.additions
        total_deletions += c.deletions
        total_files_changed += c.files_changed
        if c.stats_fetched and not c.is_merge:
            measured += 1
    has_line_stats = total_additions > 0 or total_deletions > 0

    # The accounts the report covers - also those without work in the period -
    # else (older collections) whoever authored the work.
    targets = [str(login) for login in data.meta.get("target_logins") or [] if str(login).strip()]
    authors = sorted({c.author_login for c in data.commits if c.author_login}
                     | {p.author_login for p in data.pull_requests if p.author_login})
    d: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tracked_users": ", ".join(dict.fromkeys(targets) if targets else authors),
        "total_lifetime_commits": n_commits,
        "total_pull_requests": len(data.pull_requests),
        "merged_pull_requests": merged,
        "open_pull_requests": open_prs,
        "closed_unmerged_pull_requests": len(data.pull_requests) - merged - open_prs,
        "repositories_accessible": len(data.repos),
        "repositories_contributed_to": len(contributed_repos),
        "organizations_contributed_to": len(contributed_orgs),
        "organizations_total": len(member_or_contrib_orgs),
        "distinct_author_emails": len(emails),
        "first_contribution_date": _iso_date(first_dt),
        "latest_contribution_date": _iso_date(last_dt),
    }
    if has_line_stats:
        d["total_lines_added"] = total_additions
        d["total_lines_deleted"] = total_deletions
        d["net_lines"] = total_additions - total_deletions
        d["total_files_changed"] = total_files_changed
        # Only commits whose lines were counted (merge commits never are).
        d["avg_lines_added_per_commit"] = round(total_additions / measured, 1) if measured else 0.0
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
    # Only the ``top`` largest are kept as they stream past (a tie goes to the
    # commit seen first): a min-heap on (size, -order) drops the smallest,
    # and of equal ones the latest.
    best: list[tuple[int, int, CommitRecord]] = []
    for order, c in enumerate(data.commits):
        if not (c.additions or c.deletions):
            continue
        heapq.heappush(best, (c.additions + c.deletions, -order, c))
        if len(best) > top:
            heapq.heappop(best)
    commits_with_stats = [c for _size, _neg, c in sorted(best, key=lambda item: -item[1])]
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
    return df.sort_values("total_changes", ascending=False, ignore_index=True, kind="stable").head(top)


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
