"""A LinkedIn-ready summary of a contribution report.

Built only from the report's headline metrics (``contribution_summary.csv``),
so every number in the post matches the report. Repository names and
pull-request titles are never used: a public post must not leak confidential
company work. The text is a first-person draft meant to be edited before
posting.

Example::

    During H1 2026, I contributed to 18 repositories across 6 engineering
    projects, shipping 412 commits and 57 pull requests (51 merged).
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Mapping

#: LinkedIn's limit for the text of a post.
LINKEDIN_LIMIT = 3000

_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")
_ABBR = tuple(month[:3] for month in _MONTHS)
_TAGS = {"C++": "Cpp", "C#": "CSharp", "F#": "FSharp", "Objective-C": "ObjectiveC",
         "Jupyter Notebook": "Jupyter", "Vim Script": "Vim"}


def _int(metrics: Mapping[str, object], key: str) -> int:
    text = str(metrics.get(key) or "0").replace(",", "")
    try:
        return int(float(text))
    except ValueError:
        return 0


def _date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _count(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else (many or one + 's')}"


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _last_day(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


def _day(moment: date, with_year: bool = True) -> str:
    text = f"{moment.day} {_ABBR[moment.month - 1]}"
    return f"{text} {moment.year}" if with_year else text


def period_phrase(
    start: date | None, end: date | None, first: date | None = None, last: date | None = None
) -> str:
    """How the post opens: "During H1 2026", "During July 2026", "Between …".

    Calendar periods get their name (year, half, quarter, month); any other
    range is spelled out with dates, so the post stays true whenever it is
    published. Without a period, the months of the first and latest
    contribution are used.
    """
    if start is None and end is None:
        if first is None or last is None:
            return "Recently"
        if (first.year, first.month) == (last.year, last.month):
            return f"In {_MONTHS[first.month - 1]} {first.year}"
        if first.year == last.year:
            return f"From {_MONTHS[first.month - 1]} to {_MONTHS[last.month - 1]} {last.year}"
        return f"From {_MONTHS[first.month - 1]} {first.year} to {_MONTHS[last.month - 1]} {last.year}"
    if start is not None and end is not None:
        year = start.year
        if start == date(year, 1, 1) and end == date(year, 12, 31):
            return f"During {year}"
        if start == date(year, 1, 1) and end == date(year, 6, 30):
            return f"During H1 {year}"
        if start == date(year, 7, 1) and end == date(year, 12, 31):
            return f"During H2 {year}"
        for quarter in range(4):
            if start == date(year, 3 * quarter + 1, 1) and end == _last_day(year, 3 * quarter + 3):
                return f"During Q{quarter + 1} {year}"
        if start.day == 1 and end == _last_day(year, start.month):
            return f"During {_MONTHS[start.month - 1]} {year}"
        if start.year == end.year:
            return f"Between {_day(start, with_year=False)} and {_day(end)}"
        return f"Between {_day(start)} and {_day(end)}"
    if start is not None:
        return f"Since {_day(start)}"
    return f"Up to {_day(end)}"  # type: ignore[arg-type]


def _busiest_month(value: object) -> str:
    """'2026-07 (20 commits)' -> 'July 2026 (20 commits)'."""
    match = re.match(r"^(\d{4})-(\d{2})\s*(\(.*\))?$", str(value or "").strip())
    if not match or not 1 <= int(match.group(2)) <= 12:
        return ""
    name = f"{_MONTHS[int(match.group(2)) - 1]} {match.group(1)}"
    return f"{name} {match.group(3)}" if match.group(3) else name


def _hashtag(language: str) -> str:
    return "#" + (_TAGS.get(language) or re.sub(r"[^A-Za-z0-9]", "", language))


def build_linkedin_post(metrics: Mapping[str, object]) -> str:
    """A first-person LinkedIn post from a report's summary metrics.

    Returns ``""`` when the report has no activity to talk about.
    """
    commits = _int(metrics, "total_lifetime_commits")
    prs = _int(metrics, "total_pull_requests")
    merged = _int(metrics, "merged_pull_requests")
    repos = _int(metrics, "repositories_contributed_to")
    projects = _int(metrics, "projects_contributed_to")
    private = _int(metrics, "private_repositories_contributed_to")
    if not (commits or prs):
        return ""

    period = period_phrase(
        _date(metrics.get("report_period_start")),
        _date(metrics.get("report_period_end")),
        _date(metrics.get("first_contribution_date")),
        _date(metrics.get("latest_contribution_date")),
    )
    where = "one repository" if repos == 1 else f"{repos:,} repositories"
    if 1 < projects < repos:
        where += f" across {projects:,} engineering projects"
    elif projects == 1 and repos > 1:
        where += " of one engineering project"
    shipped: list[str] = []
    if commits:
        shipped.append(_count(commits, "commit"))
    if prs and merged == prs:
        shipped.append(_count(prs, "merged pull request"))
    elif prs:
        shipped.append(_count(prs, "pull request") + (f" ({merged:,} merged)" if merged else ""))
    if repos:
        opening = f"{period}, I contributed to {where}, shipping {' and '.join(shipped)}."
    else:  # older summaries may lack the repository count
        opening = f"{period}, I shipped {' and '.join(shipped)}."
    if repos and private == repos:
        opening += " All of it happened in private codebases."
    elif repos and private * 2 >= repos:
        opening += " Most of it happened in private codebases."

    highlights: list[str] = []
    added = _int(metrics, "total_lines_added")
    deleted = _int(metrics, "total_lines_deleted")
    files = _int(metrics, "total_files_changed")
    if added or deleted:
        line = f"+{added:,} / −{deleted:,} lines of code"
        highlights.append(line + (f" across {files:,} file changes" if files else ""))
    active = _int(metrics, "active_days")
    streak = _int(metrics, "longest_daily_streak")
    if active:
        line = _count(active, "active coding day")
        highlights.append(line + (f", with a longest streak of {streak} days" if streak > 1 else ""))
    busiest = _busiest_month(metrics.get("busiest_month"))
    if busiest and f"({commits} commits)" not in busiest:  # not when one month holds everything
        highlights.append(f"Busiest month: {busiest}")
    languages = [
        lang.strip() for lang in str(metrics.get("primary_languages") or "").split(",")
        if lang.strip() and lang.strip() != "Unknown"
    ][:3]
    if languages:
        highlights.append(f"Main stack: {_join(languages)}")

    parts = [opening]
    if highlights:
        parts.append("Highlights:\n" + "\n".join(f"• {item}" for item in highlights))
    parts.append("Proud of what we shipped together, and excited for what's next.")
    parts.append(" ".join(["#SoftwareEngineering", *(_hashtag(lang) for lang in languages)]))
    return "\n\n".join(parts)[:LINKEDIN_LIMIT]
