"""Reports for a time range count, label and explain the same work every
section shows - no network.

Run directly:

    python tests/test_period_consistency.py

or with pytest:

    pytest -q tests
"""

from __future__ import annotations

import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from github_contrib.analytics import compute_analytics  # noqa: E402
from github_contrib.config import build_offline_config  # noqa: E402
from github_contrib.filters import prepare_report_data  # noqa: E402
from github_contrib.htmlreport import _metric_pairs  # noqa: E402
from github_contrib.insights import build_exec_summary, compute_insights  # noqa: E402
from github_contrib.linkedin import build_linkedin_post, period_phrase  # noqa: E402
from github_contrib.report import generate_outputs  # noqa: E402
from github_contrib.statistics import compute_statistics  # noqa: E402
from tests.test_report_layout import _commit, _data, _pr  # noqa: E402

UTC = timezone.utc


def _config(tmp: Path, **period):
    return build_offline_config(selected_logins=["neha"], output_dir=tmp, make_charts=False,
                                log_level="WARNING", **period)


def test_a_squash_merge_counts_once_when_its_pull_request_opened_before_the_period():
    originals = [_commit("o1", datetime(2026, 1, 2, 9, tzinfo=UTC), message="wip 1"),
                 _commit("o2", datetime(2026, 1, 2, 10, tzinfo=UTC), message="wip 2")]
    squash = _commit("sq", datetime(2026, 1, 5, 12, tzinfo=UTC), message="Add login (#7)")
    pr = replace(_pr(7, datetime(2025, 12, 28, tzinfo=UTC)), merge_commit_sha="sq", commit_shas=["o1", "o2"])
    data = _data([*originals, squash], [pr])
    with tempfile.TemporaryDirectory() as tmp:
        all_time, _ = prepare_report_data(data, _config(Path(tmp)))
        this_year, scope = prepare_report_data(data, _config(Path(tmp), since="2026-01-01", until="2026-11-24"))
    assert [c.sha for c in all_time.commits] == ["sq"]
    assert [c.sha for c in this_year.commits] == ["sq"], "the same change, counted once in every period"
    assert this_year.pull_requests == [] and scope.duplicates.squashed == 2


def test_quiet_months_and_years_are_listed_with_zero():
    commits = [_commit("a", datetime(2024, 11, 3, tzinfo=UTC)), _commit("b", datetime(2026, 2, 3, tzinfo=UTC))]
    stats = compute_statistics(_data(commits))
    months = stats.commits_per_month["month"].tolist()
    assert months[0] == "2024-11" and months[-1] == "2026-02" and len(months) == 16
    assert stats.commits_per_month["commits"].sum() == 2
    assert stats.commits_per_year.to_dict("records") == [
        {"year": 2024, "commits": 1}, {"year": 2025, "commits": 0}, {"year": 2026, "commits": 1},
    ]


def test_cards_say_what_was_not_collected_and_keep_deletion_only_lines():
    commit = replace(_commit("d", datetime(2026, 3, 3, tzinfo=UTC)), additions=0, deletions=500)
    data = _data([commit])
    data.meta["collect_prs"] = False
    with tempfile.TemporaryDirectory() as tmp:
        prepared, scope = prepare_report_data(data, _config(Path(tmp)))
    stats = compute_statistics(prepared)
    cards = dict(_metric_pairs(stats.summary_dict, compute_insights(prepared), scope))
    assert cards["Pull requests"] == "not collected" and cards["Merged PRs"] == "not collected"
    assert cards["Lines deleted"] == "−500" and cards["Net lines"] == "−500"


def test_pull_requests_merged_after_the_period_are_explained():
    pr = _pr(3, datetime(2025, 12, 20, tzinfo=UTC))  # merged 3 hours later...
    pr = replace(pr, merged_at=datetime(2026, 1, 5, tzinfo=UTC), closed_at=datetime(2026, 1, 5, tzinfo=UTC))
    data = _data([_commit("c", datetime(2025, 12, 21, tzinfo=UTC))], [pr])
    a = compute_analytics(data, since=datetime(2025, 1, 1, tzinfo=UTC),
                          until=datetime(2025, 12, 31, 23, 59, 59, tzinfo=UTC), now=datetime(2026, 11, 24, tzinfo=UTC))
    assert a.merged_after == 1 and sum(q.prs_merged for q in a.quarters) == 0
    from github_contrib.htmlreport import _render_analytics

    assert "merged after it counts in the merged total but in no quarter" in _render_analytics(a)


def test_summary_wording_for_single_days_prs_only_and_empty_periods():
    one_day = build_exec_summary(compute_insights(_data([_commit("x", datetime(2026, 5, 10, tzinfo=UTC))])),
                                 {"tracked_users": "neha", "total_lifetime_commits": 1,
                                  "repositories_contributed_to": 1, "first_contribution_date": "2026-05-10",
                                  "latest_contribution_date": "2026-05-10"})
    assert one_day.overview[0].startswith("On 2026-05-10, neha delivered 1 commit across 1 repository")
    prs_only = build_exec_summary(compute_insights(_data([], [_pr(1, datetime(2026, 5, 1, tzinfo=UTC))])),
                                  {"tracked_users": "neha", "total_pull_requests": 2, "merged_pull_requests": 1,
                                   "repositories_contributed_to": 1})
    assert prs_only.overview[0].startswith("In this report's period, neha opened 2 pull requests of which 1 was merged")
    empty = build_exec_summary(compute_insights(_data([])), {"tracked_users": "neha"})
    assert empty.overview == ["No commits or pull requests by neha fall in this report's period."]
    assert period_phrase(datetime(2026, 5, 10).date(), datetime(2026, 5, 10).date()) == "On 10 May 2026"


def test_a_reviews_only_period_still_gets_a_linkedin_draft():
    post = build_linkedin_post({"pull_request_reviews": 5, "prs_reviewed": 5, "teammates_reviewed": 1,
                                "report_period_start": "2026-10-26", "report_period_end": "2026-11-24"})
    assert post.startswith("Between 26 Oct and 24 Nov 2026, I reviewed 5 pull requests from 1 teammate.")
    assert post.count("Reviewed 5") == 0, "said once, in the opening"


def test_a_report_that_cannot_be_written_fails_and_leaves_no_stale_report():
    from github_contrib import report as report_module

    data = _data([_commit("s", datetime(2026, 1, 1, tzinfo=UTC))])
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp)
        generate_outputs(data, _config(out))
        assert (out / "report.html").is_file() and (out / "summary.html").is_file()

        def broken(*_args, **_kwargs):
            raise RuntimeError("renderer broke")

        original, report_module.export_reports = report_module.export_reports, broken
        try:
            generate_outputs(data, _config(out, since="2026-01-01"))
            raise AssertionError("a report that couldn't be written must fail the run")
        except RuntimeError:
            pass
        finally:
            report_module.export_reports = original
        assert not (out / "report.html").exists() and not (out / "summary.html").exists(), \
            "the earlier report must not pass for this one"


def test_users_line_names_every_account_the_report_covers():
    data = _data([_commit("u", datetime(2026, 1, 1, tzinfo=UTC))])
    data.meta["target_logins"] = ["neha", "neha-work"]
    assert compute_statistics(data).summary_dict["tracked_users"] == "neha, neha-work"


def _all_tests():
    return [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]


if __name__ == "__main__":
    for test in _all_tests():
        test()
        print("ok ", test.__name__)
