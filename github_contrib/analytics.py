"""Report analytics: how the work was delivered, what kind of work it was,
when it happened and how it grew.

Computed from the report's own commits and pull requests — after exclusions,
the period, count-once and the time zone (see filters.prepare_report_data) —
so it tells the same story as every other number in the report. The metrics
follow what team analytics products measure (time to merge, pull-request
size, work mix, working rhythm, quarter-over-quarter trends) but stay with
what the data proves: every figure is a count, share, median or percentile
defined next to it in the report. No scores, no guessing.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .models import CollectedData, CommitRecord, PullRequestRecord, ReviewRecord

# ---------------------------------------------------------------------------
# Work mix: what kind of work each commit was, from its message
# ---------------------------------------------------------------------------

#: Kinds of work, in display order.
WORK_TYPES: tuple[str, ...] = (
    "New features",
    "Improvements",
    "Bug fixes",
    "Refactoring & cleanup",
    "Tests",
    "Documentation",
    "Build, CI & chores",
    "Other",
)

_CONVENTIONAL_RE = re.compile(r"^([a-z]+)(?:\([^)]*\))?!?:\s*", re.IGNORECASE)
_CONVENTIONAL_TYPES = {
    "feat": "New features", "feature": "New features", "add": "New features",
    "fix": "Bug fixes", "bugfix": "Bug fixes", "hotfix": "Bug fixes", "bug": "Bug fixes",
    "perf": "Improvements", "improve": "Improvements", "improvement": "Improvements",
    "enhance": "Improvements", "enhancement": "Improvements", "ui": "Improvements", "ux": "Improvements",
    "refactor": "Refactoring & cleanup", "style": "Refactoring & cleanup",
    "cleanup": "Refactoring & cleanup", "clean": "Refactoring & cleanup",
    "test": "Tests", "tests": "Tests",
    "docs": "Documentation", "doc": "Documentation",
    "build": "Build, CI & chores", "ci": "Build, CI & chores", "chore": "Build, CI & chores",
    "deps": "Build, CI & chores", "release": "Build, CI & chores", "revert": "Build, CI & chores",
    "config": "Build, CI & chores", "infra": "Build, CI & chores", "ops": "Build, CI & chores",
    # Changelog-style prefixes: "Added: …", "Fixed: …".
    "added": "New features", "implemented": "New features", "new": "New features",
    "fixed": "Bug fixes", "updated": "Improvements", "update": "Improvements",
    "removed": "Refactoring & cleanup",
}
#: Ticket ids and bracketed tags in front of a message: "[ABC-12] ", "ABC-12: ", "#42 ".
_TICKET_RE = re.compile(r"^\s*(?:\[[^\]]*\]|[A-Z][A-Z0-9]+-\d+:?|#\d+:?)\s*")
_MERGE_RE = re.compile(
    r"^(merge (pull request|branch|remote-tracking|tag|commit)|merged (in|pr|pull request)\b)", re.IGNORECASE
)
#: Keyword rules for messages without a known conventional type; the first
#: match wins, so the order settles mixed messages ("fix test" is a fix,
#: "deploy test commit" is a chore).
_WORK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Build, CI & chores", re.compile(r"^revert")),
    # Before the fixes: "resolved merge conflicts", "add issue templates".
    ("Build, CI & chores", re.compile(
        r"\b(merge conflicts?|conflicts? resolved|(issue|pull request|pr|bug report|feature request) "
        r"templates?|codeowners)\b")),
    ("Bug fixes", re.compile(
        r"\b(fix\w*|bugs?|bugfix\w*|hotfix\w*|resolv\w*|solved|crash\w*|issues?|broken|regression)\b")),
    ("Build, CI & chores", re.compile(
        r"\b(deploy\w*|ci|cd|pipelines?|workflows?|docker\w*|releas\w*|bump\w*|dependenc\w*|deps|"
        r"package(?:-lock)?|versions?|configs?|configuration|env|gitignore|lint\w*|upgrad\w*|chore\w*)\b")),
    ("Documentation", re.compile(
        r"\b(readme\w*|docs?|documentation|documented|comments?|changelog|jsdoc|docstrings?)\b")),
    ("Tests", re.compile(r"\b(tests?|testing|tested|specs?|e2e|coverage|mocks?|fixtures?)\b")),
    ("Refactoring & cleanup", re.compile(
        r"\b(refactor\w*|clean\w*|restructur\w*|reorganiz\w*|renam\w*|simplif\w*|remov\w*|delet\w*|"
        r"unused|tidy|moved?|extract\w*)\b")),
    ("New features", re.compile(
        r"^(add\w*|implement\w*|creat\w*|introduc\w*|new|build|built|integrat\w*|support\w*|initial|"
        r"setup|set up|enabl\w*|launch\w*)\b|\b(features?|functionality|integration|integrated|"
        r"implemented|added)\b")),
    ("Improvements", re.compile(
        r"\b(updat\w*|improv\w*|enhanc\w*|chang\w*|modif\w*|adjust\w*|tweak\w*|refin\w*|polish\w*|"
        r"redesign\w*|ui|ux|design\w*|styl\w*|responsive|optimi[sz]\w*|perf\w*|faster|align\w*|layout)\b")),
)


def classify_commit(first_line: str) -> str | None:
    """The kind of work a commit message describes, or None for a merge commit."""
    text = (first_line or "").strip()
    if _MERGE_RE.match(text):
        return None
    text = _TICKET_RE.sub("", text)
    match = _CONVENTIONAL_RE.match(text)
    if match:
        kind = _CONVENTIONAL_TYPES.get(match.group(1).lower())
        if kind:
            return kind
        # An unknown type ("wip:", "Done:"): judge the rest, else the word with it.
        rest = text[match.end():]
        kind = _by_words(rest)
        return kind if kind != "Other" else _by_words(f"{match.group(1)} {rest}")
    return _by_words(text)


def _by_words(text: str) -> str:
    low = text.lower()
    for kind, rule in _WORK_RULES:
        if rule.search(low):
            return kind
    return "Other"


@dataclass(slots=True)
class WorkMix:
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(WORK_TYPES, 0))
    merges: int = 0  # merge commits: not a kind of work, left out of the shares

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    def share(self, kind: str) -> float:
        return self.counts.get(kind, 0) / self.total if self.total else 0.0


def _work_mix(commits: list[CommitRecord]) -> WorkMix:
    mix = WorkMix()
    for commit in commits:
        kind = None if commit.is_merge else classify_commit(commit.message_first_line)
        if kind is None:
            mix.merges += 1
        else:
            mix.counts[kind] += 1
    return mix


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated ``q``-th percentile of sorted ``values`` (None when empty)."""
    if not values:
        return None
    position = (len(values) - 1) * q / 100
    low = int(position)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (position - low)


def human_duration(hours: float | None) -> str:
    """3 min, 5.5 h, 2.1 days."""
    if hours is None:
        return "n/a"
    minutes = hours * 60
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes:.0f} min"
    if hours < 48:
        return f"{hours:.1f} h".replace(".0 h", " h")
    return f"{hours / 24:.1f} days".replace(".0 days", " days")


def within(hours: float) -> str:
    """"within 5.5 h", "within a minute" (never "within under a minute")."""
    return "within a minute" if hours * 60 < 1 else f"within {human_duration(hours)}"


def _pct(part: float, whole: float) -> float | None:
    return part / whole if whole else None


def _when(moment: datetime | None) -> float:
    """A sort key for an optional (time-zone aware) moment: missing ones last."""
    return moment.timestamp() if moment else float("inf")


# ---------------------------------------------------------------------------
# Delivery: pull requests from opening to merge
# ---------------------------------------------------------------------------

#: (label, upper bound in hours) for time to merge.
MERGE_TIME_BUCKETS: tuple[tuple[str, float], ...] = (
    ("Within 1 hour", 1),
    ("1–24 hours", 24),
    ("1–3 days", 72),
    ("3–7 days", 168),
    ("1–4 weeks", 672),
    ("Over 4 weeks", float("inf")),
)
#: (label, upper bound in lines changed, exclusive) for pull-request size.
PR_SIZE_BUCKETS: tuple[tuple[str, float], ...] = (
    ("XS · under 10 lines", 10),
    ("S · 10–99", 100),
    ("M · 100–499", 500),
    ("L · 500–999", 1000),
    ("XL · 1,000+", float("inf")),
)


@dataclass(slots=True)
class Delivery:
    opened: int = 0
    merged: int = 0
    closed_unmerged: int = 0
    still_open: int = 0
    merge_rate: float | None = None  # merged / (merged + closed without merging)
    hours_to_merge: list[float] = field(default_factory=list)  # sorted, merged PRs with both times
    median_hours: float | None = None
    p90_hours: float | None = None
    within_day: int = 0
    within_week: int = 0
    time_buckets: list[tuple[str, int]] = field(default_factory=list)
    sized: int = 0  # merged pull requests whose size is recorded
    median_size: float | None = None
    size_buckets: list[tuple[str, int]] = field(default_factory=list)
    biggest: list[PullRequestRecord] = field(default_factory=list)


def _bucketize(values: list[float], buckets: tuple[tuple[str, float], ...], inclusive: bool) -> list[tuple[str, int]]:
    counts = Counter()
    for value in values:
        for label, bound in buckets:
            if value < bound or (inclusive and value == bound):
                counts[label] += 1
                break
    return [(label, counts[label]) for label, _bound in buckets]


def _delivery(prs: list[PullRequestRecord]) -> Delivery | None:
    if not prs:
        return None
    merged = [p for p in prs if p.merged]
    closed = [p for p in prs if not p.merged and p.state == "closed"]
    hours = sorted(
        max((p.merged_at - p.created_at).total_seconds() / 3600, 0.0)
        for p in merged
        if p.merged_at and p.created_at
    )
    sizes = sorted(float(p.size) for p in merged if p.size is not None)
    biggest = sorted(
        (p for p in merged if p.size is not None),
        key=lambda p: (-(p.size or 0), _when(p.merged_at)),
    )[:5]
    return Delivery(
        opened=len(prs),
        merged=len(merged),
        closed_unmerged=len(closed),
        still_open=len(prs) - len(merged) - len(closed),
        merge_rate=_pct(len(merged), len(merged) + len(closed)),
        hours_to_merge=hours,
        median_hours=percentile(hours, 50),
        p90_hours=percentile(hours, 90),
        within_day=sum(1 for h in hours if h <= 24),
        within_week=sum(1 for h in hours if h <= 168),
        time_buckets=_bucketize(hours, MERGE_TIME_BUCKETS, inclusive=True) if hours else [],
        sized=len(sizes),
        median_size=percentile(sizes, 50),
        size_buckets=_bucketize(sizes, PR_SIZE_BUCKETS, inclusive=False) if sizes else [],
        biggest=biggest,
    )


# ---------------------------------------------------------------------------
# Working rhythm and consistency
# ---------------------------------------------------------------------------

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(slots=True)
class Rhythm:
    heatmap: list[list[int]]  # [weekday 0=Mon][hour 0..23] -> commits, in the report's time zone
    total: int
    peak_start: int  # first hour of the busiest three-hour window
    peak_share: float
    weekend_share: float
    active_weeks: int
    weeks_in_span: int
    best_30_start: date
    best_30_end: date
    best_30_commits: int
    span_start: date | None = None  # the days whose weeks are counted
    span_end: date | None = None
    span_is_period: bool = False  # the report period's (else first to latest commit)

    @property
    def consistency(self) -> float:
        return self.active_weeks / self.weeks_in_span if self.weeks_in_span else 0.0


def _rhythm(
    commits: list[CommitRecord], period_start: date | None = None, period_end: date | None = None
) -> Rhythm | None:
    """``period_start`` / ``period_end``: the report period (its end at most
    today); weeks are counted across it, else from the first to the latest
    commit."""
    moments = sorted(c.authored_date for c in commits if c.authored_date)
    if not moments:
        return None
    heat = [[0] * 24 for _ in range(7)]
    for moment in moments:
        heat[moment.weekday()][moment.hour] += 1
    by_hour = [sum(day[hour] for day in heat) for hour in range(24)]
    # The busiest three consecutive hours (wrapping past midnight).
    peak_start = max(range(24), key=lambda h: (by_hour[h] + by_hour[(h + 1) % 24] + by_hour[(h + 2) % 24], -h))
    peak = by_hour[peak_start] + by_hour[(peak_start + 1) % 24] + by_hour[(peak_start + 2) % 24]
    weekend = sum(sum(heat[d]) for d in (5, 6))

    days = [m.date() for m in moments]
    span_start = min(days[0], period_start) if period_start else days[0]
    span_end = max(days[-1], period_end) if period_end else days[-1]
    first_monday = span_start - timedelta(days=span_start.weekday())
    last_monday = span_end - timedelta(days=span_end.weekday())
    weeks_in_span = (last_monday - first_monday).days // 7 + 1
    active_weeks = len({d - timedelta(days=d.weekday()) for d in days})

    # The 30-day window (from a commit's day) holding the most commits.
    best = (days[0], days[0], 0)
    end = 0
    for start in range(len(days)):
        while end < len(days) and days[end] <= days[start] + timedelta(days=29):
            end += 1
        if end - start > best[2]:
            best = (days[start], days[end - 1], end - start)
    return Rhythm(
        heatmap=heat,
        total=len(moments),
        peak_start=peak_start,
        peak_share=peak / len(moments),
        weekend_share=weekend / len(moments),
        active_weeks=active_weeks,
        weeks_in_span=weeks_in_span,
        best_30_start=best[0],
        best_30_end=best[1],
        best_30_commits=best[2],
        span_start=span_start,
        span_end=span_end,
        span_is_period=bool(period_start or period_end),
    )


# ---------------------------------------------------------------------------
# Quarter by quarter
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Quarter:
    year: int
    number: int
    start: date
    end: date
    commits: int = 0
    prs_opened: int = 0
    prs_merged: int = 0
    reviews: int = 0  # code reviews given
    repositories: int = 0
    active_days: int = 0
    additions: int = 0
    deletions: int = 0
    partial: bool = False  # the report covers only part of it
    change: float | None = None  # commits vs the previous quarter (both complete)
    shipped: list[tuple[str, str, str]] = field(default_factory=list)  # (what, repository, "PR #12" or "")

    @property
    def label(self) -> str:
        return f"{self.year} Q{self.number}"


def _quarter_key(day: date) -> tuple[int, int]:
    return day.year, (day.month - 1) // 3 + 1


def _quarter_bounds(year: int, number: int) -> tuple[date, date]:
    start = date(year, 3 * (number - 1) + 1, 1)
    following = date(year + 1, 1, 1) if number == 4 else date(year, 3 * number + 1, 1)
    return start, following - timedelta(days=1)


def clean_title(title: str) -> str:
    """A pull-request title or commit subject without its ticket id or
    conventional prefix, capitalised: "feat(ui): add dark mode" -> "Add dark mode"."""
    text = _TICKET_RE.sub("", " ".join((title or "").split()))
    match = _CONVENTIONAL_RE.match(text)
    if match:
        text = text[match.end():]
    text = text.strip()
    return (text[:1].upper() + text[1:]) if text else " ".join((title or "").split())


def _shipped(quarter_prs: list[PullRequestRecord], quarter_commits: list[CommitRecord], top: int = 5) -> list[tuple[str, str, str]]:
    """What a quarter delivered: its merged pull requests, largest first (by
    lines changed, or by its own commits when the size isn't recorded), else
    the commit messages that describe features, improvements or fixes. A
    one-word title ("Commit", "Testdb") says nothing and is skipped."""
    items: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    def add(text: str, repo: str, ref: str) -> None:
        key = text.lower()
        if len(text.split()) >= 2 and key not in seen and len(items) < top:
            seen.add(key)
            items.append((text, repo.split("/", 1)[-1], ref))

    ranked = sorted(
        quarter_prs,
        key=lambda p: (p.size is None, -(p.size or len(p.commit_shas)), _when(p.merged_at or p.created_at)),
    )
    for pr in ranked:
        add(clean_title(pr.title), pr.full_name, f"PR #{pr.number}")
    if not items:
        order = {kind: i for i, kind in enumerate(("New features", "Improvements", "Bug fixes"))}
        candidates = [
            (order[kind], -_when(c.authored_date), c)  # most recent first within a kind
            for c in quarter_commits
            if not c.is_merge and (kind := classify_commit(c.message_first_line)) in order
        ]
        for _rank, _recent, commit in sorted(candidates, key=lambda t: (t[0], t[1])):
            add(clean_title(commit.message_first_line), commit.full_name, "")
    return items


def _quarters(
    commits: list[CommitRecord],
    prs: list[PullRequestRecord],
    covered_from: date | None,
    covered_to: date | None,
    reviews: list[ReviewRecord] | tuple = (),
) -> list[Quarter]:
    dated = [c for c in commits if c.authored_date]
    reviewed = [r.submitted_at.date() for r in reviews if r.submitted_at]
    # A pull request opened in the period can be merged after it: that merge
    # isn't part of any quarter the report covers.
    merges = [
        p for p in prs
        if p.merged and p.merged_at and not (covered_to and p.merged_at.date() > covered_to)
    ]
    keys = {_quarter_key(c.authored_date.date()) for c in dated}
    keys |= {_quarter_key(p.created_at.date()) for p in prs if p.created_at}
    keys |= {_quarter_key(p.merged_at.date()) for p in merges}
    keys |= {_quarter_key(day) for day in reviewed}
    if not keys:
        return []
    # All time: the first quarter is when the work began, usually part-way in.
    first_day = min(
        [c.authored_date.date() for c in dated] + [p.created_at.date() for p in prs if p.created_at]
        + [p.merged_at.date() for p in merges] + reviewed
    )
    reviews_in = Counter(_quarter_key(day) for day in reviewed)
    covered_from = covered_from or first_day
    # Every quarter from the first to the last, quiet ones included.
    (year, number), last = min(keys), max(keys)
    quarters: list[Quarter] = []
    while (year, number) <= last:
        start, end = _quarter_bounds(year, number)
        quarters.append(Quarter(year, number, start, end))
        year, number = (year + 1, 1) if number == 4 else (year, number + 1)
    by_key = {(q.year, q.number): q for q in quarters}

    commits_in: dict[tuple[int, int], list[CommitRecord]] = {}
    for commit in dated:
        commits_in.setdefault(_quarter_key(commit.authored_date.date()), []).append(commit)
    merged_in: dict[tuple[int, int], list[PullRequestRecord]] = {}
    for pr in prs:
        if pr.created_at:
            by_key[_quarter_key(pr.created_at.date())].prs_opened += 1
    for pr in merges:
        merged_in.setdefault(_quarter_key(pr.merged_at.date()), []).append(pr)

    previous: Quarter | None = None
    for quarter in quarters:
        key = (quarter.year, quarter.number)
        own = commits_in.get(key, [])
        quarter.commits = len(own)
        quarter.prs_merged = len(merged_in.get(key, []))
        quarter.reviews = reviews_in.get(key, 0)
        quarter.repositories = len({c.full_name for c in own})
        quarter.active_days = len({c.authored_date.date() for c in own})
        measured = [c for c in own if c.stats_fetched and not c.is_merge]
        quarter.additions = sum(c.additions for c in measured)
        quarter.deletions = sum(c.deletions for c in measured)
        quarter.partial = bool(
            (covered_from and covered_from > quarter.start) or (covered_to and covered_to < quarter.end)
        )
        if previous is not None and not previous.partial and not quarter.partial and previous.commits:
            quarter.change = quarter.commits / previous.commits - 1
        quarter.shipped = _shipped(merged_in.get(key, []), own)
        previous = quarter
    return quarters


# ---------------------------------------------------------------------------
# Code footprint
# ---------------------------------------------------------------------------

#: A commit changing fewer lines than this counts as small.
SMALL_COMMIT_LINES = 50


@dataclass(slots=True)
class Footprint:
    measured: int  # commits with line statistics (merge commits excluded)
    median_lines: float
    p90_lines: float
    small_share: float
    additions: int
    deletions: int

    @property
    def removed_per_100_added(self) -> float | None:
        return self.deletions / self.additions * 100 if self.additions else None


def _footprint(commits: list[CommitRecord]) -> Footprint | None:
    measured = [c for c in commits if c.stats_fetched and not c.is_merge]
    if not measured:
        return None
    sizes = sorted(float(c.additions + c.deletions) for c in measured)
    return Footprint(
        measured=len(measured),
        median_lines=percentile(sizes, 50) or 0.0,
        p90_lines=percentile(sizes, 90) or 0.0,
        small_share=sum(1 for s in sizes if s < SMALL_COMMIT_LINES) / len(sizes),
        additions=sum(c.additions for c in measured),
        deletions=sum(c.deletions for c in measured),
    )


# ---------------------------------------------------------------------------
# Project timeline
# ---------------------------------------------------------------------------

#: Repositories shown on the timeline (most commits first).
TIMELINE_REPOS = 12


@dataclass(slots=True)
class TimelineRow:
    repository: str
    first: date
    last: date
    commits: int
    by_month: dict[str, int]  # "2026-03" -> commits, for the bar's shading


@dataclass(slots=True)
class Timeline:
    start: date  # the first commit
    end: date  # the latest commit, or the end of the report period (at most today)
    rows: list[TimelineRow]  # in the order the work in them began
    hidden: int  # repositories with fewer commits, left off the chart


def _timeline(commits: list[CommitRecord], period_end: date | None = None) -> Timeline | None:
    """When each of the busiest repositories was worked on (first to latest commit)."""
    dated = [c for c in commits if c.authored_date]
    if not dated:
        return None
    per_repo: dict[str, list[date]] = {}
    for commit in dated:
        per_repo.setdefault(commit.full_name, []).append(commit.authored_date.date())
    busiest = sorted(per_repo.items(), key=lambda kv: (-len(kv[1]), kv[0]))[:TIMELINE_REPOS]
    rows = [
        TimelineRow(name, min(days), max(days), len(days), dict(Counter(d.strftime("%Y-%m") for d in days)))
        for name, days in busiest
    ]
    rows.sort(key=lambda row: (row.first, -row.commits, row.repository))
    every = [d for days in per_repo.values() for d in days]
    last = max(every)
    return Timeline(min(every), max(last, period_end) if period_end else last, rows, len(per_repo) - len(rows))


# ---------------------------------------------------------------------------
# Code reviews given (reviews.py)
# ---------------------------------------------------------------------------

#: Review outcomes, in display order, as GitHub names them and as shown.
REVIEW_STATES: tuple[tuple[str, str], ...] = (
    ("APPROVED", "Approved"),
    ("CHANGES_REQUESTED", "Changes requested"),
    ("COMMENTED", "Commented"),
    ("DISMISSED", "Dismissed"),
)


@dataclass(slots=True)
class Reviews:
    total: int
    states: list[tuple[str, int]]  # (label, reviews) in REVIEW_STATES order
    pull_requests: int  # distinct pull requests reviewed
    authors: int  # distinct people whose pull requests were reviewed
    repositories: list[tuple[str, int]]  # repository, reviews; most first


def _reviews(data: CollectedData) -> Reviews | None:
    reviews = data.reviews
    if not reviews:
        return None
    states = Counter(r.state for r in reviews)
    known = {state for state, _label in REVIEW_STATES}
    labelled = [(label, states[state]) for state, label in REVIEW_STATES if states[state]]
    other = sum(n for state, n in states.items() if state not in known)
    if other:
        labelled.append(("Other", other))
    repos = Counter(r.full_name for r in reviews)
    return Reviews(
        total=len(reviews),
        states=labelled,
        pull_requests=len({(r.full_name, r.pr_number) for r in reviews}),
        authors=len({r.pr_author.lower() for r in reviews if r.pr_author}),
        repositories=sorted(repos.items(), key=lambda kv: (-kv[1], kv[0])),
    )


# ---------------------------------------------------------------------------
# Technologies (read by stack.py into each repository record)
# ---------------------------------------------------------------------------

#: A language counts for a repository from this share of its code (or when
#: it is the repository's main language).
LANGUAGE_SHARE = 0.10
#: Technology categories that are frameworks and libraries (vs data / infra / tests).
FRAMEWORK_CATEGORIES = ("Frontend", "Backend", "Mobile", "AI & data")
#: GitHub "languages" that are build files, already shown as technologies.
_NOT_LANGUAGES = {"Dockerfile", "Makefile", "Procfile", "HCL", "Nix"}


@dataclass(slots=True)
class TechUse:
    name: str
    category: str  # "Language" for languages
    repositories: list[str] = field(default_factory=list)
    commits: int = 0  # the accounts' commits in those repositories
    last_used: date | None = None  # their latest commit there
    size: int = 0  # languages: bytes of it in those repositories (breaks ties)


@dataclass(slots=True)
class Stack:
    languages: list[TechUse]
    technologies: list[TechUse]  # every category, most commits first
    read: int  # repositories with commits whose technologies were read
    unread: int  # ... and those that couldn't be

    def by_category(self) -> list[tuple[str, list[TechUse]]]:
        from .stack import CATEGORIES

        groups = {category: [t for t in self.technologies if t.category == category] for category in CATEGORIES}
        return [(category, uses) for category, uses in groups.items() if uses]

    def names(self, categories: tuple[str, ...], top: int) -> list[str]:
        return [t.name for t in self.technologies if t.category in categories][:top]

    def frameworks(self) -> list[TechUse]:
        """Frameworks and libraries, those projects are built on (React,
        Flutter, NestJS, …) first; most commits first within each."""
        from .stack import CORE_FRAMEWORKS

        chosen = [t for t in self.technologies if t.category in FRAMEWORK_CATEGORIES]
        return sorted(chosen, key=lambda t: t.name not in CORE_FRAMEWORKS)  # stable: keeps the commit order


def _stack(data: CollectedData) -> Stack | None:
    """Languages and technologies across the repositories with commits, each
    weighted by the commits there (None when no repository was read)."""
    from .stack import CATEGORY_OF

    commits_in: Counter[str] = Counter(c.full_name for c in data.commits)
    last_in: dict[str, date] = {}
    for commit in data.commits:
        if commit.authored_date:
            day = commit.authored_date.date()
            last_in[commit.full_name] = max(day, last_in.get(commit.full_name, day))
    repos = {r.full_name: r for r in data.repos}
    read = [repos[name] for name in commits_in if name in repos and repos[name].stack_read]
    if not read:
        return None
    uses: dict[tuple[str, str], TechUse] = {}

    def count(name: str, category: str, repo: str, size: int = 0) -> None:
        use = uses.setdefault((category, name), TechUse(name, category))
        use.repositories.append(repo)
        use.commits += commits_in[repo]
        use.size += size
        if repo in last_in:
            use.last_used = max(last_in[repo], use.last_used or last_in[repo])

    for repo in read:
        total = sum(repo.languages.values())
        main = max(repo.languages, key=repo.languages.get) if repo.languages else ""
        for language, size in repo.languages.items():
            if language not in _NOT_LANGUAGES and total and (language == main or size / total >= LANGUAGE_SHARE):
                count(language, "Language", repo.full_name, size)
        for tech in repo.technologies:
            count(tech, CATEGORY_OF.get(tech, "Integrations"), repo.full_name)

    def ranked(category_is_language: bool) -> list[TechUse]:
        chosen = [u for (category, _n), u in uses.items() if (category == "Language") == category_is_language]
        return sorted(chosen, key=lambda u: (-u.commits, -len(u.repositories), -u.size, u.name.lower()))

    return Stack(
        languages=ranked(True),
        technologies=ranked(False),
        read=len(read),
        unread=len(commits_in) - len(read),
    )


# ---------------------------------------------------------------------------
# Everything together
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Analytics:
    work_mix: WorkMix
    delivery: Delivery | None  # None without pull requests
    rhythm: Rhythm | None  # None without dated commits
    quarters: list[Quarter]
    footprint: Footprint | None  # None without line statistics
    repositories: int
    team_share: float | None  # commits in repositories the tracked accounts don't own
    highlights: list[str]
    stack: Stack | None = None  # None when no repository's technologies were read
    reviews: Reviews | None = None  # None when no code reviews were found
    timeline: Timeline | None = None  # None without dated commits

    def summary(self) -> dict[str, object]:
        """Headline figures for contribution_summary.csv and the Summary sheet."""
        out: dict[str, object] = {}
        d, r, f = self.delivery, self.rhythm, self.footprint
        if d is not None:
            out["pr_merge_rate_pct"] = round(d.merge_rate * 100, 1) if d.merge_rate is not None else ""
            out["median_hours_to_merge"] = round(d.median_hours, 2) if d.median_hours is not None else ""
            out["p90_hours_to_merge"] = round(d.p90_hours, 2) if d.p90_hours is not None else ""
            out["prs_merged_within_1_day"] = d.within_day
            if d.median_size is not None:
                out["median_pr_size_lines"] = round(d.median_size)
        if r is not None:
            out["active_weeks"] = r.active_weeks
            out["weeks_in_span"] = r.weeks_in_span
            out["peak_commit_hours"] = f"{r.peak_start:02d}:00–{(r.peak_start + 3) % 24:02d}:00"
            out["weekend_commits_pct"] = round(r.weekend_share * 100, 1)
            out["best_30_day_commits"] = r.best_30_commits
        if f is not None:
            out["median_commit_size_lines"] = round(f.median_lines)
            out["small_commits_pct"] = round(f.small_share * 100, 1)
        if self.work_mix.total:
            out["work_mix"] = ", ".join(
                f"{kind} {self.work_mix.share(kind) * 100:.0f}%"
                for kind in WORK_TYPES if self.work_mix.counts[kind]
            )
        rv = self.reviews
        if rv is not None:
            out["pull_request_reviews"] = rv.total
            out["prs_reviewed"] = rv.pull_requests
            out["teammates_reviewed"] = rv.authors
        s = self.stack
        if s is not None:
            out["languages_used"] = ", ".join(u.name for u in s.languages[:6])
            out["frameworks_used"] = ", ".join(t.name for t in s.frameworks()[:8])
            out["data_and_infrastructure_used"] = ", ".join(s.names(("Data & storage", "Cloud & DevOps"), 8))
            out["technologies"] = ", ".join(u.name for u in s.technologies)
        return out


def _highlights(a: Analytics, commits: int) -> list[str]:
    """Short, factual statements worth quoting in a review (each only when
    the data is large enough to mean something)."""
    out: list[str] = []
    d, r, f, mix = a.delivery, a.rhythm, a.footprint, a.work_mix
    if d is not None and d.merged >= 3 and d.median_hours is not None:
        line = f"Merged {d.merged:,} pull requests; half of them {within(d.median_hours)} of opening."
        if d.merge_rate is not None and d.merged + d.closed_unmerged >= 5:
            line += f" {d.merge_rate:.0%} of the pull requests closed were merged."
        out.append(line)
    rv = a.reviews
    if rv is not None and rv.total >= 3:
        outcomes = dict(rv.states)
        detail = [f"{outcomes[label]:,} {word}" for label, word in
                  (("Approved", "approvals"), ("Changes requested", "change requests")) if outcomes.get(label)]
        people = f" by {rv.authors} teammate{'' if rv.authors == 1 else 's'}" if rv.authors else ""
        repos = len(rv.repositories)
        out.append(
            f"Reviewed {rv.pull_requests:,} pull requests{people} across {repos} "
            f"repositor{'y' if repos == 1 else 'ies'}" + (f" ({', '.join(detail)})" if detail else "") + "."
        )
    if r is not None and commits >= 10:
        out.append(
            f"Most productive stretch: {r.best_30_commits:,} commits in 30 days "
            f"({r.best_30_start:%d %b %Y} – {r.best_30_end:%d %b %Y})."
        )
    if r is not None and r.weeks_in_span >= 4 and r.consistency >= 0.5:
        out.append(f"Committed code in {r.active_weeks} of {r.weeks_in_span} weeks ({r.consistency:.0%}).")
    busy = [q for q in a.quarters if q.commits]
    if len(busy) >= 2:
        best = max(busy, key=lambda q: (q.commits, q.prs_merged))
        line = f"Busiest quarter: {best.label}, with {best.commits:,} commits"
        out.append(line + (f" and {best.prs_merged} pull requests merged." if best.prs_merged else "."))
        growth = [q for q in a.quarters if q.change is not None and q.change >= 0.1]
        if growth:
            latest = growth[-1]
            out.append(f"Commits grew {latest.change:.0%} in {latest.label} over the quarter before.")
    if mix.total >= 20 and mix.share("Other") <= 0.5:
        built = mix.share("New features") + mix.share("Improvements")
        line = f"{built:.0%} of commits built or improved features"
        if mix.counts["Bug fixes"]:
            line += f"; {mix.share('Bug fixes'):.0%} fixed bugs"
        out.append(line + ".")
    if d is not None and d.biggest:
        pr = d.biggest[0]
        files = f", {pr.changed_files:,} files" if pr.changed_files else ""
        out.append(
            f"Largest merged pull request: “{clean_title(pr.title)}” in {pr.full_name.split('/', 1)[-1]} "
            f"(+{pr.additions or 0:,} / −{pr.deletions or 0:,} lines{files})."
        )
    if f is not None and f.measured >= 20:
        out.append(
            f"Half of all commits changed {f.median_lines:,.0f} lines or fewer; "
            f"{f.small_share:.0%} changed under {SMALL_COMMIT_LINES} lines."
        )
    if a.team_share is not None and a.repositories >= 2 and a.team_share > 0:
        out.append(
            f"{a.team_share:.0%} of commits went into repositories owned by organizations or other "
            f"accounts, across {a.repositories} repositories in total."
        )
    s = a.stack
    if s is not None and s.technologies:
        leading = s.frameworks()[:3] or s.technologies[:3]
        named = [f"{t.name} ({t.commits:,} commits)" for t in leading]
        areas = len({t.category for t in s.technologies})
        out.append(
            f"The repositories worked on use {len(s.technologies)} technologies across {areas} "
            f"area{'' if areas == 1 else 's'}, led by "
            + (named[0] if len(named) == 1 else ", ".join(named[:-1]) + " and " + named[-1]) + "."
        )
    return out


def compute_analytics(
    data: CollectedData,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    now: datetime | None = None,
) -> Analytics:
    """Every analytic for the report's data (already filtered and localized).

    ``since`` / ``until`` are the report period and ``now`` is when the data
    was collected: together they tell which quarters the report covers only
    partly (those get no growth figure) and which weeks count."""
    commits, prs = data.commits, data.pull_requests
    tz = next((c.authored_date.tzinfo for c in commits if c.authored_date), None)

    def local(moment: datetime | None) -> datetime | None:
        if moment is not None and tz is not None and moment.tzinfo is not None:
            return moment.astimezone(tz)
        return moment

    start, until, now = local(since), local(until), local(now)
    # A period still running ends today, not on its last day ("from <date>"
    # runs to today); all time ends with the latest commit.
    if until is not None and now is not None and (until.tzinfo is None) == (now.tzinfo is None):
        period_end: datetime | None = min(until, now)
    else:
        period_end = until if until is not None else (now if start is not None else None)
    end = period_end or now  # what the data covers up to
    period = (start.date() if start else None, period_end.date() if period_end else None)

    tracked = {str(login).lower() for login in data.meta.get("target_logins") or []}
    tracked |= {c.author_login.lower() for c in commits if c.author_login}
    team = sum(1 for c in commits if c.owner.lower() not in tracked)

    analytics = Analytics(
        work_mix=_work_mix(commits),
        delivery=_delivery(prs),
        rhythm=_rhythm(commits, *period),
        quarters=_quarters(
            commits, prs, start.date() if start else None, end.date() if end else None, data.reviews
        ),
        footprint=_footprint(commits),
        repositories=len({c.full_name for c in commits}),
        team_share=_pct(team, len(commits)),
        highlights=[],
        stack=_stack(data),
        reviews=_reviews(data),
        timeline=_timeline(commits, period[1]),
    )
    analytics.highlights = _highlights(analytics, len(commits))
    return analytics
