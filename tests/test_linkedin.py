"""LinkedIn summary tests - no network needed.

Run directly:

    python tests/test_linkedin.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib.linkedin import LINKEDIN_LIMIT, build_linkedin_post, period_phrase  # noqa: E402

METRICS = {
    "total_lifetime_commits": "412",
    "total_pull_requests": "57",
    "merged_pull_requests": "51",
    "repositories_contributed_to": "18",
    "projects_contributed_to": "6",
    "private_repositories_contributed_to": "14",
    "total_lines_added": "48210",
    "total_lines_deleted": "12904",
    "total_files_changed": "3120",
    "active_days": "96",
    "longest_daily_streak": "9",
    "busiest_month": "2026-03 (88 commits)",
    "primary_languages": "TypeScript, Kotlin, Unknown, Python, Go",
    "report_period_start": "2026-01-01",
    "report_period_end": "2026-06-30",
    "first_contribution_date": "2026-01-02T10:00:00+05:30",
    "latest_contribution_date": "2026-06-29T18:00:00+05:30",
}


def test_period_phrases():
    d = date
    assert period_phrase(d(2026, 1, 1), d(2026, 6, 30)) == "During H1 2026"
    assert period_phrase(d(2026, 7, 1), d(2026, 12, 31)) == "During H2 2026"
    assert period_phrase(d(2026, 7, 1), d(2026, 9, 30)) == "During Q3 2026"
    assert period_phrase(d(2025, 1, 1), d(2025, 12, 31)) == "During 2025"
    assert period_phrase(d(2026, 2, 1), d(2026, 2, 28)) == "During February 2026"
    assert period_phrase(d(2024, 2, 1), d(2024, 2, 29)) == "During February 2024"  # leap year
    assert period_phrase(d(2026, 4, 7), d(2026, 10, 6)) == "Between 7 Apr and 6 Oct 2026"
    assert period_phrase(d(2025, 12, 15), d(2026, 3, 3)) == "Between 15 Dec 2025 and 3 Mar 2026"
    assert period_phrase(d(2026, 3, 1), None) == "Since 1 Mar 2026"
    assert period_phrase(None, d(2026, 3, 1)) == "Up to 1 Mar 2026"
    # All time: the months of the first and latest contribution.
    assert period_phrase(None, None, d(2026, 4, 21), d(2026, 9, 3)) == "From April to September 2026"
    assert period_phrase(None, None, d(2024, 4, 21), d(2026, 9, 3)) == "From April 2024 to September 2026"
    assert period_phrase(None, None, d(2026, 4, 2), d(2026, 4, 28)) == "In April 2026"
    print("ok  test_period_phrases")


def test_full_post():
    post = build_linkedin_post(METRICS)
    assert post.startswith(
        "During H1 2026, I contributed to 18 repositories across 6 engineering projects, "
        "shipping 412 commits and 57 pull requests (51 merged). Most of it happened in private codebases."
    ), post
    for line in (
        "• +48,210 / −12,904 lines of code across 3,120 file changes",
        "• 96 active coding days, with a longest streak of 9 days",
        "• Busiest month: March 2026 (88 commits)",
        "• Main stack: TypeScript, Kotlin and Python",
    ):
        assert line in post, line
    assert "Unknown" not in post
    assert post.endswith("#SoftwareEngineering #TypeScript #Kotlin #Python")
    assert len(post) <= LINKEDIN_LIMIT
    print("ok  test_full_post")


def test_post_edge_cases():
    # No activity: nothing to post.
    assert build_linkedin_post({"total_lifetime_commits": "0"}) == ""
    assert build_linkedin_post({}) == ""

    # One repository, all PRs merged, no line stats, all work in one month.
    small = {
        "total_lifetime_commits": "20", "total_pull_requests": "1", "merged_pull_requests": "1",
        "repositories_contributed_to": "1", "projects_contributed_to": "1",
        "active_days": "7", "longest_daily_streak": "1", "busiest_month": "2026-07 (20 commits)",
        "report_period_start": "2026-07-01", "report_period_end": "2026-07-31",
        "primary_languages": "C#, C++",
    }
    post = build_linkedin_post(small)
    assert post.startswith("During July 2026, I contributed to one repository, shipping 20 commits and "
                           "1 merged pull request."), post
    assert "lines of code" not in post and "Busiest month" not in post and "streak" not in post
    assert "7 active coding days" in post and post.endswith("#CSharp #Cpp")

    # Every repository in one project; pull requests not collected.
    one_project = {**small, "total_pull_requests": "0", "merged_pull_requests": "0",
                   "repositories_contributed_to": "3", "projects_contributed_to": "1",
                   "private_repositories_contributed_to": "3"}
    post = build_linkedin_post(one_project)
    assert "3 repositories of one engineering project, shipping 20 commits." in post, post
    assert "pull request" not in post and "All of it happened in private codebases." in post

    # As many projects as repositories: no "across" clause.
    each = {**small, "repositories_contributed_to": "4", "projects_contributed_to": "4"}
    assert "contributed to 4 repositories, shipping" in build_linkedin_post(each)

    # Older summaries without a repository count.
    assert build_linkedin_post({"total_lifetime_commits": "2"}).startswith("Recently, I shipped 2 commits.")
    print("ok  test_post_edge_cases")


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


def main() -> int:
    failures = 0
    for test in _all_tests():
        try:
            test()
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"FAIL {test.__name__}: {exc!r}")
            import traceback
            traceback.print_exc()
    total = len(_all_tests())
    print(f"\n{total - failures}/{total} tests passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
