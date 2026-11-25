"""Commit calendar, concise summary (the PDF) and streamed report output.

Run directly:

    python tests/test_report_layout.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib.analytics import (  # noqa: E402
    CALENDAR_MAX_YEARS,
    _commit_calendar,
    compute_analytics,
)
from github_contrib.config import build_offline_config  # noqa: E402
from github_contrib.htmlreport import (  # noqa: E402
    SUMMARY_QUARTERS,
    SUMMARY_REPOS,
    _calendar_svg,
    export_reports,
    render_html,
    render_summary_html,
)
from github_contrib.insights import compute_insights  # noqa: E402
from github_contrib.models import CollectedData, CommitRecord, PullRequestRecord, RepoRecord  # noqa: E402
from github_contrib.pdfexport import pdf_source  # noqa: E402
from github_contrib.report import generate_outputs  # noqa: E402
from github_contrib.statistics import attach_analytics, compute_statistics  # noqa: E402

UTC = timezone.utc


def _commit(sha: str, when: datetime, repo: str = "acme/app", message: str = "feat: add thing") -> CommitRecord:
    owner, name = repo.split("/")
    return CommitRecord(
        repository=name, full_name=repo, owner=owner, organization=owner, sha=sha, message=message,
        message_first_line=message, author_login="neha", author_name="Neha", author_email="n@x.com",
        committer_name="Neha", committer_email="n@x.com", authored_date=when, committed_date=when,
        branch="main", url=f"https://github.com/{repo}/commit/{sha}", additions=3, deletions=1,
        stats_fetched=True, parent_count=1,
    )


def _pr(number: int, when: datetime, repo: str = "acme/app") -> PullRequestRecord:
    owner, name = repo.split("/")
    return PullRequestRecord(
        repository=name, full_name=repo, organization=owner, number=number, title=f"Add feature {number}",
        author_login="neha", state="closed", merged=True, created_at=when, updated_at=when + timedelta(hours=3),
        closed_at=when + timedelta(hours=3), merged_at=when + timedelta(hours=3), base_branch="main",
        head_branch=f"f/{number}", url=f"https://github.com/{repo}/pull/{number}", additions=10, deletions=2,
        changed_files=1,
    )


def _data(commits: list[CommitRecord], prs: list[PullRequestRecord] = ()) -> CollectedData:
    names = sorted({c.full_name for c in commits} | {p.full_name for p in prs})
    return CollectedData(
        repos=[RepoRecord(full_name=n, name=n.split("/")[1], owner=n.split("/")[0], organization=n.split("/")[0])
               for n in names],
        commits=list(commits), pull_requests=list(prs),
        meta={"target_logins": ["neha"], "collected_at": "2026-11-24T12:00:00+00:00"},
    )


def _svg_cells(svg: str) -> list[tuple[int, int]]:
    return [(int(x), int(y)) for x, y in re.findall(r"<rect x='(\d+)' y='(\d+)' width='10'", svg)]


# ---------------------------------------------------------------------------
# Calendar model
# ---------------------------------------------------------------------------


def test_calendar_counts_days_and_starts_weeks_on_monday():
    commits = [
        _commit("a", datetime(2026, 3, 2, 9, tzinfo=UTC)),   # Monday
        _commit("b", datetime(2026, 3, 2, 15, tzinfo=UTC)),
        _commit("c", datetime(2026, 3, 8, 9, tzinfo=UTC)),   # Sunday
    ]
    cal = _commit_calendar(commits, date(2026, 3, 1), date(2026, 3, 31))
    assert cal is not None and len(cal.grids) == 1 and cal.recent is cal.grids[0]
    grid = cal.recent
    assert grid.first == date(2026, 3, 1) and grid.last == date(2026, 3, 31)
    assert grid.days == {date(2026, 3, 2): 2, date(2026, 3, 8): 1}
    assert grid.total == 3 and grid.active_days == 2
    assert grid.start == date(2026, 2, 23) and grid.start.weekday() == 0  # Monday on/before 1 Mar
    # Mon 2 Mar opens the second column; Sun 8 Mar closes it.
    assert grid.column(date(2026, 3, 2)) == 1 and grid.column(date(2026, 3, 8)) == 1
    assert grid.weeks == 6


def test_calendar_draws_exactly_the_period_with_correct_rows():
    commits = [_commit(f"s{i}", datetime(2026, 10, 26, 10, tzinfo=UTC) + timedelta(days=i)) for i in range(29)]
    cal = _commit_calendar(commits, date(2026, 10, 26), date(2026, 11, 23))
    svg = _calendar_svg(cal.recent, cal)
    cells = _svg_cells(svg)
    assert len(cells) == 29, "one square per day of the period, none outside it"
    day = date(2026, 10, 26)
    for x, y in cells:
        assert y == 14 + day.weekday() * 12, f"{day} drawn in the wrong weekday row"
        assert x == 26 + cal.recent.column(day) * 12
        day += timedelta(days=1)
    labels = re.findall(r"<text x='0' y='(\d+)'>(\w+)</text>", svg)
    assert [name for _y, name in labels] == ["Mon", "Wed", "Fri"]
    assert [int(y) for y, _n in labels] == [14 + 0 * 12 + 9, 14 + 2 * 12 + 9, 14 + 4 * 12 + 9]


def test_calendar_month_labels_never_crowd_each_other():
    # A period that starts on the last days of a month: that month's label
    # would collide with the next one, so it is left out.
    commits = [_commit("x", datetime(2025, 12, 15, tzinfo=UTC))]
    cal = _commit_calendar(commits, date(2025, 11, 29), date(2026, 11, 28))
    svg = _calendar_svg(cal.recent, cal)
    xs = [int(x) for x in re.findall(r"<text x='(\d+)' y='9'>", svg)]
    names = re.findall(r"<text x='\d+' y='9'>(\w+)</text>", svg)
    assert names[0] == "Dec" and "Nov" in names[1:], names
    assert all(b - a >= 24 for a, b in zip(xs, xs[1:])), f"labels closer than two columns: {xs}"
    width = int(re.search(r"viewBox='0 0 (\d+) ", svg).group(1))
    assert all(x + 18 <= width for x in xs), "a label runs past the right edge"


def test_calendar_splits_long_spans_by_year_and_keeps_recent_twelve_months():
    commits = [_commit(f"y{year}", datetime(year, 6, 1, tzinfo=UTC)) for year in (2019, 2021, 2024)]
    commits.append(_commit("last", datetime(2024, 11, 20, tzinfo=UTC)))
    cal = _commit_calendar(commits)  # all time: first to latest commit
    assert cal.per_year
    assert [g.first.year for g in cal.grids] == [2024, 2021, 2019], "only years with commits, newest first"
    assert cal.grids[-1].first == date(2019, 6, 1), "the first year starts at the first commit"
    assert cal.grids[0].last == date(2024, 11, 20)
    assert cal.recent.last == date(2024, 11, 20) and (cal.recent.last - cal.recent.first).days == 364
    assert sum(g.total for g in cal.grids) == 4


def test_calendar_caps_the_number_of_years():
    commits = [_commit(f"y{year}", datetime(year, 3, 3, tzinfo=UTC)) for year in range(1990, 2026)]
    cal = _commit_calendar(commits)
    assert len(cal.grids) == CALENDAR_MAX_YEARS
    assert cal.hidden_years == 36 - CALENDAR_MAX_YEARS


def test_calendar_levels_follow_the_busy_days():
    quiet = _commit_calendar([_commit("a", datetime(2026, 1, 5, tzinfo=UTC))])
    assert quiet.thresholds == (1, 2, 3) and quiet.level(0) == 0 and quiet.level(1) == 1 and quiet.level(9) == 4
    busy = [_commit(f"b{i}", datetime(2026, 1, 1 + i % 20, tzinfo=UTC)) for i in range(400)]
    cal = _commit_calendar(busy)
    t1, t2, t3 = cal.thresholds
    assert 1 <= t1 < t2 < t3
    assert [cal.level(n) for n in (0, t1, t2, t3, t3 + 1)] == [0, 1, 2, 3, 4]


def test_calendar_follows_the_report_period_through_analytics():
    commits = [_commit(f"c{i}", datetime(2026, 11, 1, tzinfo=UTC) + timedelta(days=i)) for i in range(20)]
    since = datetime(2026, 10, 26, tzinfo=UTC)
    until = datetime(2026, 11, 24, 23, 59, 59, tzinfo=UTC)
    a = compute_analytics(_data(commits), since=since, until=until, now=datetime(2026, 11, 24, 12, tzinfo=UTC))
    grid = a.commit_calendar.recent
    assert grid.first == date(2026, 10, 26) and grid.last == date(2026, 11, 24)
    assert not a.commit_calendar.per_year


# ---------------------------------------------------------------------------
# Summary (the PDF) and streamed output
# ---------------------------------------------------------------------------


def _big_report_inputs(repos: int = 40, per_repo: int = 30):
    commits, prs = [], []
    start = datetime(2019, 1, 7, 10, tzinfo=UTC)
    n = 0
    for r in range(repos):
        name = f"acme/service-{r:02d}"
        for i in range(per_repo):
            commits.append(_commit(f"{r:02d}{i:04d}", start + timedelta(days=(r * 37 + i * 23) % 2500), name))
            n += 1
        prs.append(_pr(r + 1, start + timedelta(days=r * 50), name))
    data = _data(commits, prs)
    stats = compute_statistics(data)
    insights = compute_insights(data)
    analytics = compute_analytics(data, now=datetime(2026, 11, 24, tzinfo=UTC))
    attach_analytics(stats, analytics)
    return data, stats, insights


def test_summary_is_bounded_and_lists_only_the_top_entries():
    _data_, stats, insights = _big_report_inputs()
    page = render_summary_html(stats, insights, stats.summary_dict)
    assert "GitHub Contribution Summary" in page
    body = page.split("</style>", 1)[1].rsplit("<script>", 1)[0]
    assert "commit-timeline" not in body and "commitlist" not in body, "no commit lists in the summary"
    rows = re.findall(r"<td class='dname'>acme/service-\d\d</td>", page)
    assert len(rows) == SUMMARY_REPOS, "the dashboard lists the top repositories only"
    assert "30 more repositories" in page and "each listed in the full report" in page
    quarters = re.findall(r"<td class='dname'>\d{4} Q\d", page)
    assert len(quarters) <= SUMMARY_QUARTERS
    assert page.count("class='calsvg'") == 1, "the summary shows one calendar: the latest 12 months"
    full = render_html(stats, insights, stats.summary_dict, Path("nonexistent"), include_charts=False)
    assert len(page) < len(full) / 3


def test_export_writes_summary_and_streams_identical_html():
    _data_, stats, insights = _big_report_inputs(repos=5, per_repo=4)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        written = export_reports(out, stats, insights, stats.summary_dict, out / "charts", include_charts=False)
        names = sorted(p.name for p in written)
        assert names == ["report.html", "report.md", "summary.html"]
        assert not list(out.glob("*.partial")), "no temporary files left behind"
        streamed = (out / "report.html").read_text(encoding="utf-8")
        whole = render_html(stats, insights, stats.summary_dict, out / "charts", include_charts=False)
        assert streamed == whole
        assert streamed.endswith("</script></body></html>")
        assert pdf_source(out / "report.html") == out / "summary.html"
        (out / "summary.html").unlink()
        assert pdf_source(out / "report.html") == out / "report.html", "older reports print whole"


def test_regen_end_to_end_writes_every_output():
    commits = [_commit(f"e{i}", datetime(2026, 1, 1, 9, tzinfo=UTC) + timedelta(days=i * 3)) for i in range(60)]
    data = _data(commits, [_pr(1, datetime(2026, 2, 1, tzinfo=UTC))])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        config = build_offline_config(selected_logins=["neha"], output_dir=out, make_charts=False, log_level="WARNING")
        generate_outputs(data, config)
        for name in ("report.html", "summary.html", "report.md", "github_contributions.xlsx",
                     "contribution_summary.csv", "commits.csv"):
            assert (out / name).is_file() and (out / name).stat().st_size > 0, name


def test_regen_for_a_period_rewrites_the_summary_csv():
    import csv

    from github_contrib.offline import load_collected_from_csv

    # Distinct times: identical commits would be counted once (rebased copies).
    commits = [_commit(f"r{i}", datetime(2026, 1 + i % 10, 3, 9, i, tzinfo=UTC)) for i in range(40)]
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        config = build_offline_config(selected_logins=["neha"], output_dir=out, make_charts=False, log_level="WARNING")
        generate_outputs(_data(commits), config)
        before = (out / "commits.csv").read_bytes()
        period = build_offline_config(selected_logins=["neha"], output_dir=out, make_charts=False,
                                      log_level="WARNING", since="2026-03-01", until="2026-03-31")
        generate_outputs(load_collected_from_csv(out), period, export_source_csvs=False)
        with (out / "contribution_summary.csv").open(encoding="utf-8-sig", newline="") as fh:
            metrics = {row["metric"]: row["value"] for row in csv.DictReader(fh)}
        assert metrics["total_lifetime_commits"] == "4", "the summary matches the period's report"
        assert metrics["report_period"] == "2026-03-01 to 2026-03-31"
        assert (out / "commits.csv").read_bytes() == before, "the collected data is left untouched"


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in _all_tests():
        test()
        print("ok ", test.__name__)
