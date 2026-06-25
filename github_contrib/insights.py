"""Deterministic insight & narrative generation (no API key required).

The goal is to *explain the work* a person did even when individual commit
messages are terse or unclear.  We lean on three deterministic signals:

* **Pull request titles** - usually the most human-readable description of a
  unit of work.
* **Branch names** - frequently descriptive (``AddHorsePhoto``,
  ``EmailVerified``); we humanise them into readable phrases.
* **Commit-message keywords** - frequency analysis of the words people actually
  used, with git/boilerplate noise removed, to surface recurring themes.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime

from .logging_config import get_logger
from .models import CollectedData, CommitRecord, PullRequestRecord, RepoRecord

log = get_logger("insights")

# Words that carry no descriptive signal in commit messages.
_STOPWORDS: frozenset[str] = frozenset(
    """
    the a an and or but for nor so yet of to in on at by with from into onto up
    down out over under again further then once here there all any both each few
    more most other some such not only own same than too very can will just don
    should now this that these those is are was were be been being do does did
    have has had it its as if we i you he she they them his her their our your my
    me us add added adds adding update updated updates updating fix fixed fixes
    fixing change changed changes changing remove removed removes removing create
    created creates make made making use used using set get got new old final wip
    test tests testing merge merged branch commit commits initial minor major
    refactor refactored chore chores edit edited modify modified work working done
    file files code codes implement implemented implementation small big first
    feat feats perf docs doc style build ci cd revert pull request requests into
    main master develop dev release stuff things various misc some changes update
    """.split()
)

# Conventional-commit type prefixes (feat:, fix:, docs:, ...).
_CONV_TYPES: frozenset[str] = frozenset(
    "feat fix docs style refactor perf test chore build ci revert".split()
)

_CONV_RE = re.compile(r"^(\w+)(\([^)]*\))?!?:", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']{2,}")
_BRANCH_PREFIX_RE = re.compile(
    r"^(feature|feat|bugfix|bug|fix|hotfix|chore|release|rel|dev|test|task|story)[/_-]",
    re.IGNORECASE,
)


def humanize_branch(name: str) -> str:
    """Turn a branch name into a readable phrase.

    ``AddHorsePhoto`` -> ``Add horse photo``; ``403issue`` -> ``403 issue``;
    ``fe-fat-money-loan`` -> ``Fe fat money loan``.
    """
    if not name:
        return ""
    text = _BRANCH_PREFIX_RE.sub("", name)
    text = re.sub(r"[/_-]+", " ", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)   # camelCase boundary
    text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)    # letter -> digit
    text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)    # digit -> letter
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    return text[0].upper() + text[1:]


def _clean_title(title: str) -> str:
    text = title.strip()
    # Drop a leading conventional-commit type and bracketed ticket ids.
    text = _CONV_RE.sub("", text).strip()
    text = re.sub(r"^\[[^\]]+\]\s*", "", text).strip()
    return text or title.strip()


def extract_themes(messages: list[str], top: int = 8) -> tuple[list[str], dict[str, int]]:
    """Return (top keywords, conventional-commit-type counts)."""
    words: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for message in messages:
        first_line = (message.splitlines()[0] if message else "").strip()
        if not first_line:
            continue
        # Skip auto-generated merge commits - they only add noise (branch names,
        # usernames, "pull request") and describe no real work.
        low = first_line.lower()
        if low.startswith(("merge pull request", "merge branch", "merge remote-tracking", "merged pull")):
            continue
        match = _CONV_RE.match(first_line)
        if match and match.group(1).lower() in _CONV_TYPES:
            types[match.group(1).lower()] += 1
            first_line = first_line[match.end():]  # drop the "feat:" prefix
        for word in _WORD_RE.findall(first_line.lower()):
            if word not in _STOPWORDS:
                words[word] += 1
    return [w for w, _ in words.most_common(top)], dict(types.most_common())


@dataclass(slots=True)
class RepoWork:
    """A human-readable account of the work done in one repository."""

    full_name: str
    organization: str
    is_private: bool
    language: str
    commits: int
    pull_requests: int
    merged_pull_requests: int
    first_activity: datetime | None
    last_activity: datetime | None
    branches: list[str]
    authors: list[str]
    highlights: list[str]  # readable PR titles + humanised branch topics
    themes: list[str]      # recurring keywords
    conv_types: dict[str, int]

    def headline(self) -> str:
        span = ""
        if self.first_activity and self.last_activity:
            span = f", {self.first_activity.date()} → {self.last_activity.date()}"
        pr = ""
        if self.pull_requests:
            pr = f", {self.pull_requests} PR(s) ({self.merged_pull_requests} merged)"
        branch = ""
        if len(self.branches) > 1:
            branch = f" across {len(self.branches)} branches"
        return f"{self.commits} commit(s){branch}{pr}{span}"


@dataclass(slots=True)
class Insights:
    """Top-level deterministic insights for the whole report."""

    total_active_days: int = 0
    longest_streak_days: int = 0
    busiest_day: str = ""
    busiest_day_commits: int = 0
    busiest_month: str = ""
    busiest_month_commits: int = 0
    avg_commits_per_active_week: float = 0.0
    languages: list[tuple[str, int, int]] = field(default_factory=list)  # (lang, repos, commits)
    repo_work: list[RepoWork] = field(default_factory=list)

    def to_summary_dict(self) -> dict[str, object]:
        return {
            "active_days": self.total_active_days,
            "longest_daily_streak": self.longest_streak_days,
            "busiest_day": f"{self.busiest_day} ({self.busiest_day_commits} commits)" if self.busiest_day else "",
            "busiest_month": f"{self.busiest_month} ({self.busiest_month_commits} commits)" if self.busiest_month else "",
            "avg_commits_per_active_week": round(self.avg_commits_per_active_week, 1),
            "primary_languages": ", ".join(lang for lang, _r, _c in self.languages[:5]),
        }


def _compute_activity(commits: list[CommitRecord]) -> dict[str, object]:
    days = sorted({c.authored_date.date() for c in commits if c.authored_date})
    longest = current = 0
    prev: date | None = None
    for day in days:
        if prev is not None and (day - prev).days == 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        prev = day

    weekday = Counter(
        c.authored_date.strftime("%A") for c in commits if c.authored_date
    )
    month = Counter(
        c.authored_date.strftime("%Y-%m") for c in commits if c.authored_date
    )
    weeks = {
        c.authored_date.isocalendar()[:2] for c in commits if c.authored_date
    }
    avg_per_week = (len(commits) / len(weeks)) if weeks else 0.0

    busiest_day, busiest_day_n = (weekday.most_common(1)[0] if weekday else ("", 0))
    busiest_month, busiest_month_n = (month.most_common(1)[0] if month else ("", 0))
    return {
        "active_days": len(days),
        "longest_streak": longest,
        "busiest_day": busiest_day,
        "busiest_day_n": busiest_day_n,
        "busiest_month": busiest_month,
        "busiest_month_n": busiest_month_n,
        "avg_per_week": avg_per_week,
    }


def _compute_languages(
    repos_by_name: dict[str, RepoRecord],
    commits_per_repo: Counter[str],
) -> list[tuple[str, int, int]]:
    lang_repos: Counter[str] = Counter()
    lang_commits: Counter[str] = Counter()
    for full_name, n_commits in commits_per_repo.items():
        repo = repos_by_name.get(full_name)
        lang = (repo.language if repo else "") or "Unknown"
        lang_repos[lang] += 1
        lang_commits[lang] += n_commits
    return [
        (lang, lang_repos[lang], lang_commits[lang])
        for lang, _ in lang_commits.most_common()
    ]


def _build_repo_work(
    repo: RepoRecord,
    commits: list[CommitRecord],
    prs: list[PullRequestRecord],
) -> RepoWork:
    default_branch = repo.default_branch
    branches = sorted({c.branch for c in commits if c.branch})
    authors = sorted({c.author_login for c in commits if c.author_login})
    dates = [c.authored_date for c in commits if c.authored_date]

    # Highlights: readable PR titles first (most descriptive), then branch topics.
    highlights: list[str] = []
    seen: set[str] = set()
    for pr in sorted(prs, key=lambda p: (not p.merged, p.number)):
        title = _clean_title(pr.title)
        key = title.lower()
        if title and key not in seen:
            seen.add(key)
            tag = "merged" if pr.merged else pr.state
            highlights.append(f"{title} (PR #{pr.number}, {tag})")
    for branch in branches:
        if branch == default_branch:
            continue
        phrase = humanize_branch(branch)
        key = phrase.lower()
        if phrase and key not in seen:
            seen.add(key)
            highlights.append(phrase)

    themes, conv_types = extract_themes([c.message for c in commits])

    return RepoWork(
        full_name=repo.full_name,
        organization=repo.organization,
        is_private=repo.is_private,
        language=repo.language or "",
        commits=len(commits),
        pull_requests=len(prs),
        merged_pull_requests=sum(1 for p in prs if p.merged),
        first_activity=min(dates) if dates else None,
        last_activity=max(dates) if dates else None,
        branches=branches,
        authors=authors,
        highlights=highlights[:12],
        themes=themes,
        conv_types=conv_types,
    )


def compute_insights(data: CollectedData) -> Insights:
    """Compute activity insights and per-repo work narratives."""
    activity = _compute_activity(data.commits)

    commits_by_repo: dict[str, list[CommitRecord]] = {}
    for commit in data.commits:
        commits_by_repo.setdefault(commit.full_name, []).append(commit)
    prs_by_repo: dict[str, list[PullRequestRecord]] = {}
    for pr in data.pull_requests:
        prs_by_repo.setdefault(pr.full_name, []).append(pr)

    repos_by_name = {r.full_name: r for r in data.repos}
    commit_counts: Counter[str] = Counter(
        {name: len(cs) for name, cs in commits_by_repo.items()}
    )

    repo_work: list[RepoWork] = []
    for full_name in sorted(
        set(commits_by_repo) | set(prs_by_repo),
        key=lambda n: (-len(commits_by_repo.get(n, [])), n),
    ):
        repo = repos_by_name.get(full_name) or RepoRecord(
            full_name=full_name,
            name=full_name.split("/")[-1],
            owner=full_name.split("/")[0] if "/" in full_name else "",
            organization="",
        )
        repo_work.append(
            _build_repo_work(
                repo,
                commits_by_repo.get(full_name, []),
                prs_by_repo.get(full_name, []),
            )
        )

    insights = Insights(
        total_active_days=int(activity["active_days"]),
        longest_streak_days=int(activity["longest_streak"]),
        busiest_day=str(activity["busiest_day"]),
        busiest_day_commits=int(activity["busiest_day_n"]),
        busiest_month=str(activity["busiest_month"]),
        busiest_month_commits=int(activity["busiest_month_n"]),
        avg_commits_per_active_week=float(activity["avg_per_week"]),
        languages=_compute_languages(repos_by_name, commit_counts),
        repo_work=repo_work,
    )
    log.info(
        "insights: %d active day(s), longest streak %d, %d repo work summaries",
        insights.total_active_days,
        insights.longest_streak_days,
        len(insights.repo_work),
    )
    return insights
