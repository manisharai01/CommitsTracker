"""Code-review tests - a fake GitHub serves the contribution history.

Run directly:

    python tests/test_reviews.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# Make the package importable when run as a plain script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib.analytics import compute_analytics  # noqa: E402
from github_contrib.config import AppConfig  # noqa: E402
from github_contrib.exporters import export_csvs  # noqa: E402
from github_contrib.filters import prepare_report_data  # noqa: E402
from github_contrib.htmlreport import render_html  # noqa: E402
from github_contrib.insights import compute_insights  # noqa: E402
from github_contrib.linkedin import build_linkedin_post  # noqa: E402
from github_contrib.models import CollectedData, CommitRecord, Coverage, RepoRecord, ReviewRecord  # noqa: E402
from github_contrib.offline import load_collected_from_csv  # noqa: E402
from github_contrib.reviews import collect_reviews  # noqa: E402
from github_contrib.statistics import attach_analytics, compute_statistics  # noqa: E402

UTC = timezone.utc


def _node(when: str, number: int, author: str, state: str = "APPROVED", repo: str = "acme/app",
          owner_type: str = "Organization") -> dict:
    owner = repo.split("/")[0]
    return {
        "occurredAt": when,
        "pullRequestReview": {"state": state, "url": f"https://github.com/{repo}/pull/{number}#r{when}"},
        "pullRequest": {"number": number, "title": f"PR {number}", "url": f"https://github.com/{repo}/pull/{number}",
                        "author": {"login": author},
                        "repository": {"nameWithOwner": repo, "owner": {"login": owner, "__typename": owner_type}}},
    }


class FakeGitHub:
    """Answers reviews.py's queries: createdAt, and review contributions per window."""

    def __init__(self, created: dict[str, str], reviews: dict[str, list[dict]], *, restricted=None, broken=()):
        self.created = created
        self.reviews = reviews
        self.restricted = restricted or {}
        self.broken = set(broken)  # (login, from-year) windows that fail
        self.queries: list[str] = []

    async def graphql(self, query: str):
        self.queries.append(query)
        data, errors = {}, []
        for line in query.strip().splitlines()[1:-1]:
            alias, login, body = re.match(r'^(w\d+): user\(login: ("[^"]+")\) \{ (.*) \}$', line).groups()
            login = json.loads(login)
            if body == "createdAt":
                data[alias] = {"createdAt": self.created[login]}
                continue
            start, end = re.search(r'from: "([^"]+)", to: "([^"]+)"', body).groups()
            if (login, start[:4]) in self.broken:
                data[alias] = None
                errors.append({"type": "SERVICE_UNAVAILABLE", "path": [alias], "message": "try later"})
                continue
            first = int(re.search(r"first: (\d+)", body).group(1))
            after = int((re.search(r'after: "(\d+)"', body) or [None, "0"])[1])
            inside = [n for n in self.reviews.get(login, []) if start <= n["occurredAt"] < end]
            page = inside[after:after + first]
            more = after + first < len(inside)
            data[alias] = {"contributionsCollection": {
                "restrictedContributionsCount": self.restricted.get(login, 0),
                "pullRequestReviewContributions": {
                    "totalCount": len(inside), "nodes": page,
                    "pageInfo": {"hasNextPage": more, "endCursor": str(after + first) if more else None},
                },
            }}
        return data, errors


def test_reviews_are_read_year_by_year_with_each_accounts_token():
    # 120 reviews in one window: more than a page (100) holds.
    many = [_node(f"2025-0{1 + i // 28}-{i % 28 + 1:02d}T10:00:00Z", 100 + i, "teammate") for i in range(120)]
    fake = FakeGitHub(
        created={"neha": "2024-06-01T00:00:00Z", "neha2": "2026-01-01T00:00:00Z"},
        reviews={
            "neha": many + [
                _node("2024-07-01T09:00:00Z", 1, "teammate", "CHANGES_REQUESTED"),
                _node("2026-02-01T09:00:00Z", 2, "neha2"),           # her other account's PR: not a review of others
                _node("2026-02-02T09:00:00Z", 3, "Neha"),            # her own (any case): left out
                _node("2026-02-03T09:00:00Z", 4, "other", "COMMENTED", repo="solo/lib", owner_type="User"),
            ],
            "neha2": [_node("2026-03-01T09:00:00Z", 5, "teammate")],
        },
        restricted={"neha2": 7},
    )
    coverage = Coverage()
    now = datetime(2026, 6, 1, tzinfo=UTC)
    got = asyncio.run(collect_reviews({"neha": fake, "neha2": fake}, ["neha", "neha2"], since=None, until=None,
                                      coverage=coverage, inflight=2, now=now))
    by_pr = {r.pr_number: r for r in got}
    assert len(got) == 120 + 3 and set(by_pr) >= {1, 4, 5} and not {2, 3} & set(by_pr), sorted(by_pr)
    assert by_pr[1].state == "CHANGES_REQUESTED" and by_pr[1].reviewer == "neha"
    assert by_pr[4].organization == "" and by_pr[4].owner == "solo" and by_pr[1].organization == "acme"
    windows = [q for q in fake.queries if "contributionsCollection" in q]
    assert any('from: "2024-06-01T00:00:00Z", to: "2025-06-01T00:00:00Z"' in q for q in windows), "a year at a time"
    assert any("after:" in q for q in windows), "long lists are paged"
    assert any(n.startswith("[neha2] 7 contribution(s) in private repositories are hidden") for n in coverage.notes)

    # A period: only its windows; a window that fails is a note, not silence.
    fake.broken = {("neha", "2025")}
    coverage = Coverage()
    got = asyncio.run(collect_reviews(
        {"neha": fake}, ["neha"], since=datetime(2025, 1, 1, tzinfo=UTC), until=datetime(2026, 2, 28, tzinfo=UTC),
        coverage=coverage, inflight=2, now=now))
    # 2025's window failed; 2026's has PRs 2 (neha2 isn't tracked in this run) and 4 (3 is her own).
    assert sorted(r.pr_number for r in got) == [2, 4], [r.pr_number for r in got]
    assert any("code reviews from 01 Jan 2025" in n and "could not be read" in n for n in coverage.notes), coverage.notes

    class Broken:
        async def graphql(self, query):
            raise RuntimeError("boom")

    coverage = Coverage()
    assert asyncio.run(collect_reviews({"neha": Broken()}, ["neha"], since=None, until=None,
                                       coverage=coverage, inflight=1, now=now)) == []
    assert coverage.notes, "a failure is reported"
    print("ok  test_reviews_are_read_year_by_year_with_each_accounts_token")


def _review(number: int, when: datetime, repo: str = "acme/app", author: str = "teammate",
            state: str = "APPROVED") -> ReviewRecord:
    owner = repo.split("/")[0]
    return ReviewRecord(reviewer="neha", full_name=repo, owner=owner, organization=owner, pr_number=number,
                        pr_title=f"Fix {number}", pr_author=author, state=state, submitted_at=when,
                        url=f"https://github.com/{repo}/pull/{number}#r1")


def _commit(sha: str, when: datetime, repo: str = "acme/app") -> CommitRecord:
    owner, name = repo.split("/")
    return CommitRecord(
        repository=name, full_name=repo, owner=owner, organization=owner, sha=sha, message="feat: x",
        message_first_line="feat: x", author_login="neha", author_name="N", author_email="n@x.com",
        committer_name="N", committer_email="n@x.com", authored_date=when, committed_date=when, branch="main",
        url="", parent_count=1,
    )


def test_reviews_follow_the_reports_period_exclusions_and_time_zone():
    reviews = [
        _review(1, datetime(2026, 3, 31, 20, 0, tzinfo=UTC)),          # 1 Apr 01:30 in India: Q2 there
        _review(2, datetime(2026, 5, 1, tzinfo=UTC), state="CHANGES_REQUESTED"),
        _review(3, datetime(2026, 5, 2, tzinfo=UTC), repo="neha/side", author="friend"),  # her own repo
        _review(4, datetime(2025, 1, 1, tzinfo=UTC)),                  # before the period
    ]
    data = CollectedData(repos=[RepoRecord(full_name="acme/app", name="app", owner="acme", organization="acme")],
                         commits=[_commit("a", datetime(2026, 4, 10, tzinfo=UTC))],
                         reviews=reviews, meta={"target_logins": ["neha"]})
    config = AppConfig(accounts=[], target_logins=["neha"], exclude_own_repos=True,
                       since=datetime(2026, 1, 1, tzinfo=UTC), timezone_name="Asia/Kolkata", tz=ZoneInfo("Asia/Kolkata"))
    filtered, _scope = prepare_report_data(data, config)
    assert sorted(r.pr_number for r in filtered.reviews) == [1, 2], "period and own-repo exclusion apply"
    assert filtered.reviews[0].submitted_at.utcoffset().total_seconds() == 5.5 * 3600, "in the report's time zone"
    a = compute_analytics(filtered)
    assert a.reviews.total == 2 and a.reviews.pull_requests == 2 and a.reviews.authors == 1
    assert a.reviews.states == [("Approved", 1), ("Changes requested", 1)]
    assert [(q.label, q.reviews) for q in a.quarters] == [("2026 Q2", 2)], "review 1 lands in Q2 in India"
    print("ok  test_reviews_follow_the_reports_period_exclusions_and_time_zone")


def test_reviews_reach_the_csv_report_summary_and_linkedin():
    t = datetime(2026, 4, 1, tzinfo=UTC)
    reviews = [_review(n, t.replace(day=n), author=f"mate{n % 3}") for n in range(1, 11)]
    reviews.append(_review(11, t.replace(day=12), state="CHANGES_REQUESTED", repo="acme/api"))
    data = CollectedData(commits=[_commit(f"c{i}", t.replace(day=1 + i)) for i in range(5)], reviews=reviews,
                         meta={"target_logins": ["neha"]})
    a = compute_analytics(data)
    assert any(h.startswith("Reviewed 11 pull requests by 4 teammates across 2 repositories (10 approvals, "
                            "1 change requests).") for h in a.highlights), a.highlights
    summary = a.summary()
    assert (summary["pull_request_reviews"], summary["prs_reviewed"], summary["teammates_reviewed"]) == (11, 11, 4)
    stats = compute_statistics(data)
    attach_analytics(stats, a)
    with tempfile.TemporaryDirectory() as tmp:
        page = render_html(stats, compute_insights(data), {**stats.summary_dict, **summary}, Path(tmp),
                           include_charts=False)
        assert "<h3>Code reviews</h3>" in page and "<th>Reviews</th>" in page and "Repositories reviewed most" in page
        export_csvs(Path(tmp), stats)
        loaded = load_collected_from_csv(Path(tmp)).reviews
        assert [(r.pr_number, r.state, r.submitted_at) for r in loaded] == [
            (r.pr_number, r.state, r.submitted_at) for r in reviews]
    # Older runs have no reviews.csv: no reviews, and no warning about it.
    with tempfile.TemporaryDirectory() as tmp:
        export_csvs(Path(tmp), compute_statistics(replace(data, reviews=[])))
        (Path(tmp) / "reviews.csv").unlink()
        assert load_collected_from_csv(Path(tmp)).reviews == []
    post = build_linkedin_post({"total_lifetime_commits": "5", "prs_reviewed": "11", "teammates_reviewed": "4"})
    assert "• Reviewed 11 pull requests from 4 teammates" in post, post
    print("ok  test_reviews_reach_the_csv_report_summary_and_linkedin")


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
