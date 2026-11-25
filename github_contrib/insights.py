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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime

from .fanout import fan_out
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


def _count_theme(message: str, words: Counter[str], types: Counter[str]) -> None:
    """Add one commit message to the keyword and commit-type counts."""
    first_line = (message.splitlines()[0] if message else "").strip()
    if not first_line:
        return
    # Skip auto-generated merge commits - they only add noise (branch names,
    # usernames, "pull request") and describe no real work.
    low = first_line.lower()
    if low.startswith(("merge pull request", "merge branch", "merge remote-tracking", "merged pull")):
        return
    match = _CONV_RE.match(first_line)
    if match and match.group(1).lower() in _CONV_TYPES:
        types[match.group(1).lower()] += 1
        first_line = first_line[match.end():]  # drop the "feat:" prefix
    for word in _WORD_RE.findall(first_line.lower()):
        if word not in _STOPWORDS:
            words[word] += 1


def extract_themes(messages: Iterable[str], top: int = 8) -> tuple[list[str], dict[str, int]]:
    """Return (top keywords, conventional-commit-type counts)."""
    words: Counter[str] = Counter()
    types: Counter[str] = Counter()
    for message in messages:
        _count_theme(message, words, types)
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
    # Line-level impact (0 when --no-commit-stats was used)
    total_additions: int = 0
    total_deletions: int = 0
    total_files_changed: int = 0
    most_impactful_commit: str = ""  # "message (±N lines)"
    technologies: list[str] = field(default_factory=list)  # found in the repository (stack.py)

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
        lines = ""
        if self.total_additions or self.total_deletions:
            lines = f", +{self.total_additions:,}/−{self.total_deletions:,} lines"
        return f"{self.commits} commit(s){branch}{pr}{lines}{span}"


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


# ---------------------------------------------------------------------------
# Executive summary (promotion-ready narrative, fully data-driven)
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ExecSummary:
    """A promotion-ready narrative of the tracked user's contribution.

    Everything is derived deterministically from the collected commits and
    pull requests — no manual editing or AI required.
    """

    overview: list[str] = field(default_factory=list)       # short paragraphs
    key_projects: list[RepoWork] = field(default_factory=list)
    features: list[tuple[str, str]] = field(default_factory=list)  # (repo, feature)
    strengths: list[tuple[str, str]] = field(default_factory=list)  # (label, value)


def _fmt_day(value: object) -> str:
    text = str(value or "")
    return text[:10] if text else ""


def _span_months(first: str, last: str) -> int:
    """Months from one ISO date to another, by elapsed time and rounded
    (2 Jan to 30 Dec is 12 months, 31 Jan to 1 Mar is 1); 0 when unknown."""
    try:
        f = datetime.fromisoformat(first.replace("Z", "+00:00"))
        l = datetime.fromisoformat(last.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return 0
    days = (l.date() - f.date()).days + 1  # both days count
    return max(0, round(days / 30.4375))


def build_exec_summary(insights: Insights, summary: dict[str, object]) -> ExecSummary:
    """Build the executive summary from insights + the headline summary dict."""
    works = sorted(insights.repo_work, key=lambda w: (-w.commits, w.full_name))
    ins = insights.to_summary_dict()

    users = str(summary.get("tracked_users") or "the tracked user")
    commits = int(summary.get("total_lifetime_commits") or 0)
    repos = int(summary.get("repositories_contributed_to") or 0)
    prs = int(summary.get("total_pull_requests") or 0)
    merged = int(summary.get("merged_pull_requests") or 0)
    owners = sorted({w.full_name.split("/", 1)[0] for w in works})
    first = _fmt_day(summary.get("first_contribution_date"))
    last = _fmt_day(summary.get("latest_contribution_date"))
    months = _span_months(first, last)

    overview: list[str] = []
    if not commits and not prs:
        overview.append(f"No commits or pull requests by {users} fall in this report's period.")
    else:
        if first and last:
            span = f"On {first}" if first == last else f"Between {first} and {last}"
        else:
            span = "In this report's period"
        if months >= 2:
            span += f" ({months} months)"
        where = (
            f"{repos} repositor{'y' if repos == 1 else 'ies'} owned by {len(owners)} "
            f"account{'' if len(owners) == 1 else 's'} or organization{'' if len(owners) == 1 else 's'}"
        )
        opened = f"{prs} pull request{'' if prs == 1 else 's'} of which {merged} {'was' if merged == 1 else 'were'} merged"
        if commits:
            sentence = f"{span}, {users} delivered {commits:,} commit{'' if commits == 1 else 's'} across {where}"
            if prs:
                sentence += f", and opened {opened}"
        else:  # pull requests only
            sentence = f"{span}, {users} opened {opened}, across {where}"
        overview.append(sentence + ".")

    added = int(summary.get("total_lines_added") or 0)
    deleted = int(summary.get("total_lines_deleted") or 0)
    files = int(summary.get("total_files_changed") or 0)
    if added or deleted:
        overview.append(
            f"That work amounts to {added:,} lines added and {deleted:,} lines removed "
            f"across {files:,} file touches — a net footprint of {added - deleted:+,} lines."
        )

    active_days = int(ins.get("active_days") or 0)
    streak = int(ins.get("longest_daily_streak") or 0)
    per_week = ins.get("avg_commits_per_active_week") or 0
    if active_days:
        overview.append(
            f"Delivery was sustained: {active_days} active coding days, a longest streak "
            f"of {streak} consecutive day(s), and an average of {per_week} commits per active week."
        )

    # Features delivered: PR titles first (most descriptive), round-robin across
    # projects so a single busy repo does not crowd out the others; then
    # humanised branch topics to fill remaining slots.
    features: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _take(get_items, cap: int) -> None:
        for i in range(8):
            for w in works:
                if len(features) >= cap:
                    return
                items = get_items(w)
                if i < len(items):
                    key = items[i].lower()
                    if key not in seen:
                        seen.add(key)
                        features.append((w.full_name.split("/", 1)[-1], items[i]))

    _take(lambda w: [h for h in w.highlights if "(PR #" in h], 14)
    _take(lambda w: [h for h in w.highlights if "(PR #" not in h], 14)

    strengths: list[tuple[str, str]] = []
    # Detected technologies (analytics / stack.py), else the repositories' main languages.
    languages_used = str(summary.get("languages_used") or "")
    frameworks_used = str(summary.get("frameworks_used") or "")
    data_and_infra = str(summary.get("data_and_infrastructure_used") or "")
    if languages_used or frameworks_used:
        strengths.append(("Technologies", " · ".join(x for x in (languages_used, frameworks_used) if x)))
        if data_and_infra:
            strengths.append(("Data & infrastructure", data_and_infra))
    elif insights.languages:
        strengths.append((
            "Technologies",
            ", ".join(f"{lang} ({c} commits)" for lang, _r, c in insights.languages[:6]
                      if lang != "Unknown") or ins.get("primary_languages", ""),
        ))
    # Other people's accounts and organizations: the tracked accounts' own
    # repositories aren't a team they contributed to.
    tracked = {name.strip().lower() for name in users.split(",")}
    teams = [owner for owner in owners if owner.lower() not in tracked]
    if teams:
        shown = ", ".join(teams[:12]) + (f" and {len(teams) - 12} more" if len(teams) > 12 else "")
        strengths.append(("Teams / accounts contributed to", shown))
    if ins.get("busiest_month"):
        strengths.append(("Peak delivery month", str(ins["busiest_month"])))
    private = sum(1 for w in works if w.is_private)
    if works:
        strengths.append((
            "Project mix",
            f"{len(works)} repositories ({private} private / {len(works) - private} public)",
        ))
    branches = sum(len(w.branches) for w in works)
    if branches > len(works):
        strengths.append(("Branch coverage", f"work landed on {branches} branches"))

    return ExecSummary(
        overview=overview,
        key_projects=works[:8],
        features=features,
        strengths=strengths,
    )


def _compute_activity(commits: Iterable[CommitRecord]) -> dict[str, object]:
    day_set: set[date] = set()
    weekday: Counter[str] = Counter()
    month: Counter[str] = Counter()
    weeks: set[tuple[int, int]] = set()
    n_commits = 0
    for c in commits:
        n_commits += 1
        moment = c.authored_date
        if moment is None:
            continue
        day_set.add(moment.date())
        weekday[moment.strftime("%A")] += 1
        month[moment.strftime("%Y-%m")] += 1
        weeks.add(moment.isocalendar()[:2])
    days = sorted(day_set)
    longest = current = 0
    prev: date | None = None
    for day in days:
        if prev is not None and (day - prev).days == 1:
            current += 1
        else:
            current = 1
        longest = max(longest, current)
        prev = day

    avg_per_week = (n_commits / len(weeks)) if weeks else 0.0

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


class _RepoTally:
    """One repository's commits, summed up as they stream past (its branch
    names, authors, keyword counts: never the commits themselves)."""

    __slots__ = ("commits", "branches", "authors", "first", "last", "words", "types",
                 "additions", "deletions", "files", "biggest")

    def __init__(self) -> None:
        self.commits = 0
        self.branches: set[str] = set()
        self.authors: set[str] = set()
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.words: Counter[str] = Counter()
        self.types: Counter[str] = Counter()
        self.additions = self.deletions = self.files = 0
        self.biggest: CommitRecord | None = None  # the first of the largest

    def add(self, c: CommitRecord) -> None:
        self.commits += 1
        if c.branch:
            self.branches.add(c.branch)
        if c.author_login:
            self.authors.add(c.author_login)
        moment = c.authored_date
        if moment is not None:
            if self.first is None or moment < self.first:
                self.first = moment
            if self.last is None or moment > self.last:
                self.last = moment
        _count_theme(c.message, self.words, self.types)
        self.additions += c.additions
        self.deletions += c.deletions
        self.files += c.files_changed
        if self.biggest is None or c.additions + c.deletions > self.biggest.additions + self.biggest.deletions:
            self.biggest = c


def _build_repo_work(
    repo: RepoRecord,
    commits: Iterable[CommitRecord] | _RepoTally,
    prs: list[PullRequestRecord],
) -> RepoWork:
    if isinstance(commits, _RepoTally):
        tally = commits
    else:
        tally = _RepoTally()
        for commit in commits:
            tally.add(commit)
    default_branch = repo.default_branch
    branches = sorted(tally.branches)
    authors = sorted(tally.authors)

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

    themes = [w for w, _ in tally.words.most_common(8)]
    conv_types = dict(tally.types.most_common())

    total_additions = tally.additions
    total_deletions = tally.deletions
    total_files_changed = tally.files

    most_impactful = ""
    if tally.biggest is not None:
        big = tally.biggest
        if big.additions or big.deletions:
            total_ch = big.additions + big.deletions
            msg = (big.message_first_line or big.sha[:8])[:60]
            most_impactful = f"{msg} (±{total_ch:,} lines)"

    return RepoWork(
        full_name=repo.full_name,
        organization=repo.organization,
        is_private=repo.is_private,
        language=repo.language or "",
        commits=tally.commits,
        pull_requests=len(prs),
        merged_pull_requests=sum(1 for p in prs if p.merged),
        first_activity=tally.first,
        last_activity=tally.last,
        branches=branches,
        authors=authors,
        highlights=highlights[:12],
        themes=themes,
        conv_types=conv_types,
        total_additions=total_additions,
        total_deletions=total_deletions,
        total_files_changed=total_files_changed,
        most_impactful_commit=most_impactful,
        technologies=list(repo.technologies),
    )


def _tally_by_repo(commits: Iterable[CommitRecord]) -> dict[str, _RepoTally]:
    tallies: dict[str, _RepoTally] = {}
    for commit in commits:
        tally = tallies.get(commit.full_name)
        if tally is None:
            tally = tallies[commit.full_name] = _RepoTally()
        tally.add(commit)
    return tallies


def compute_insights(data: CollectedData) -> Insights:
    """Compute activity insights and per-repo work narratives."""
    # One read of the commits: the activity, and a tally per repository.
    activity, commits_by_repo = fan_out(data.commits, [_compute_activity, _tally_by_repo])
    prs_by_repo: dict[str, list[PullRequestRecord]] = {}
    for pr in data.pull_requests:
        prs_by_repo.setdefault(pr.full_name, []).append(pr)

    repos_by_name = {r.full_name: r for r in data.repos}
    commit_counts: Counter[str] = Counter(
        {name: tally.commits for name, tally in commits_by_repo.items()}
    )

    repo_work: list[RepoWork] = []
    for full_name in sorted(
        set(commits_by_repo) | set(prs_by_repo),
        key=lambda n: (-(commits_by_repo[n].commits if n in commits_by_repo else 0), n),
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
                commits_by_repo.get(full_name) or _RepoTally(),
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
