#!/usr/bin/env python3
"""GitHub contribution report generator (CLI entry point).

Examples
--------
    # Single account
    python github_report.py --user octocat

    # Multiple accounts
    python github_report.py --user alice --user bob

    # Quick run (default branch only)
    python github_report.py --user octocat --default-branch-only

Set GITHUB_TOKEN_<LOGIN> (or just GITHUB_TOKEN) in your environment or .env
file before running.  See .env.example and README.md for details.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from github_contrib import __version__
from github_contrib.config import (
    DEFAULT_CONCURRENCY,
    DEFAULT_OUTPUT_DIR,
    DEFAULT_USER_TOKEN_ENV,
    AppConfig,
    ConfigError,
    build_config,
    build_offline_config,
)
from github_contrib.logging_config import get_logger, setup_logging

KNOWN_USERS = list(DEFAULT_USER_TOKEN_ENV)


def _positive_int(value: str) -> int:
    """argparse type: accept only integers >= 1."""
    try:
        ivalue = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}")
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {ivalue}")
    return ivalue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="github_report.py",
        description="Generate a complete GitHub contribution report (CSV + Excel + charts).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--user",
        action="append",
        metavar="LOGIN",
        help=(
            "GitHub login to report on (repeatable). Works for ANY user, not just "
            f"the built-in defaults ({', '.join(KNOWN_USERS)}). The token is read "
            "from (in order) the built-in mapping, GITHUB_TOKEN_<LOGIN>, or "
            "GITHUB_TOKEN. Defaults to the built-in users."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Report on all known users (the default when no --user is given).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for the generated files.",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=DEFAULT_CONCURRENCY,
        help="Maximum number of concurrent API requests.",
    )
    parser.add_argument(
        "--all-branches",
        action="store_true",
        help="Scan commits on every branch. This is the DEFAULT; the flag is kept "
        "for explicitness.",
    )
    parser.add_argument(
        "--default-branch-only",
        action="store_true",
        help="Faster mode: scan only each repo's default branch (may miss commits "
        "that live only on unmerged feature branches).",
    )
    parser.add_argument(
        "--skip-forks",
        action="store_true",
        help="Do not scan commits/PRs in forked repositories.",
    )
    parser.add_argument(
        "--no-prs",
        action="store_true",
        help="Skip pull request collection.",
    )
    parser.add_argument(
        "--no-commits",
        action="store_true",
        help="Skip commit collection.",
    )
    parser.add_argument(
        "--no-charts",
        action="store_true",
        help="Skip matplotlib chart / dashboard generation.",
    )
    parser.add_argument(
        "--repo",
        action="append",
        metavar="OWNER/NAME",
        default=None,
        help=(
            "Force-include a specific repository (repeatable), e.g. acme/work-app. "
            "Useful for private/work repos auto-discovery might miss. "
            "Also reads the EXTRA_REPOS environment variable."
        ),
    )
    parser.add_argument(
        "--org",
        action="append",
        metavar="ORG",
        default=None,
        help=(
            "Force-include every accessible repository in an organization "
            "(repeatable). Also reads the EXTRA_ORGS environment variable."
        ),
    )
    parser.add_argument(
        "--author-email",
        action="append",
        metavar="EMAIL",
        dest="author_email",
        help=(
            "Also collect commits by this git author email address (repeatable). "
            "Use this when commits were made with a work or personal email that is "
            "NOT linked to the GitHub account — the most common reason private/work "
            "commits go missing. Also reads AUTHOR_EMAILS from the environment."
        ),
    )
    parser.add_argument(
        "--exclude-owner",
        action="append",
        metavar="LOGIN",
        dest="exclude_owner",
        default=None,
        help=(
            "Exclude every repository OWNED by this GitHub login from the outputs "
            "(repeatable). Also reads EXCLUDE_OWNERS from the environment."
        ),
    )
    parser.add_argument(
        "--exclude-own-repos",
        action="store_true",
        help=(
            "Drop repositories owned by the tracked login(s) themselves (personal "
            "repos) so the report shows only work done in other accounts / "
            "organizations (i.e. company work). By default all repos are included."
        ),
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        help=(
            "Only report work from this date on (YYYY-MM-DD, inclusive, in --timezone; "
            "or a full ISO 8601 timestamp). Commits count by author date, pull "
            "requests by the date they were opened."
        ),
    )
    parser.add_argument(
        "--until",
        metavar="DATE",
        help="Only report work up to this date (YYYY-MM-DD, inclusive, in --timezone).",
    )
    parser.add_argument(
        "--timezone",
        metavar="TZ",
        default="UTC",
        help=(
            "Time zone for --since/--until and for days, weeks and months in the "
            "report: an IANA name (Asia/Kolkata) or an offset (+05:30)."
        ),
    )
    parser.add_argument(
        "--pdf",
        action="store_true",
        help=(
            "Also render report.html to report.pdf using headless Edge/Chrome "
            "(no extra Python dependencies; a Chromium-based browser must be installed)."
        ),
    )
    parser.add_argument(
        "--regen",
        action="store_true",
        help=(
            "Rebuild every report artifact (HTML, Markdown, Excel, charts, PDF with "
            "--pdf) from the CSVs of a previous run in the output directory. No "
            "network access or token required. The source CSVs are left untouched."
        ),
    )
    parser.add_argument(
        "--no-commit-stats",
        action="store_true",
        help="Skip fetching per-commit line stats (additions/deletions/files changed).",
    )
    parser.add_argument(
        "--rest-api",
        action="store_true",
        help=(
            "Collect with the REST API only (the previous engine). Much slower: it "
            "needs thousands of requests for an active account and usually waits "
            "for GitHub's hourly limit. By default the GraphQL API is used, with "
            "REST as the fallback for anything it cannot read."
        ),
    )
    parser.add_argument(
        "--no-search-discovery",
        action="store_true",
        help="Disable commit-search-based repository discovery.",
    )
    parser.add_argument(
        "--no-org-repos",
        action="store_true",
        help="Do not enumerate every repository inside your organizations.",
    )
    parser.add_argument(
        "--max-repos",
        type=_positive_int,
        default=None,
        help="Limit the number of repositories scanned (useful for testing).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def resolve_users(args: argparse.Namespace) -> list[str]:
    """Determine which users to report on from the CLI flags."""
    if args.user:
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        users: list[str] = []
        for u in args.user:
            if u not in seen:
                seen.add(u)
                users.append(u)
        return users
    # --all or default (KNOWN_USERS may be empty when no defaults are configured)
    return list(KNOWN_USERS)


def _configure_event_loop() -> None:
    """On Windows, the Selector event loop avoids noisy aiohttp shutdown errors."""
    if sys.platform.startswith("win"):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:  # pragma: no cover - non-fatal
            pass


def _print_final_summary(stats, config: AppConfig) -> None:
    s = stats.summary_dict
    print()
    print("=" * 60)
    print("  GitHub contribution report complete")
    print("=" * 60)
    print(f"  Users               : {s.get('tracked_users') or ', '.join(config.target_logins)}")
    print(f"  Period              : {s.get('report_period', 'all time')} ({s.get('report_timezone', 'UTC')})")
    print(f"  Commits             : {s.get('total_lifetime_commits', 0)}")
    print(f"  Pull requests       : {s.get('total_pull_requests', 0)} "
          f"(merged {s.get('merged_pull_requests', 0)})")
    print(f"  Repos accessible    : {s.get('repositories_accessible', 0)}")
    print(f"  Repos contributed   : {s.get('repositories_contributed_to', 0)}")
    print(f"  Orgs contributed    : {s.get('organizations_contributed_to', 0)}")
    print(f"  First contribution  : {s.get('first_contribution_date') or 'n/a'}")
    print(f"  Latest contribution : {s.get('latest_contribution_date') or 'n/a'}")
    gaps = int(s.get("data_completeness_warnings") or 0)
    print(f"  Data completeness   : {f'{gaps} warning(s) - see the report' if gaps else 'no gaps detected'}")
    print("-" * 60)
    print(f"  Output directory    : {config.output_dir.resolve()}")
    print(f"  HTML report         : {(config.output_dir / 'report.html').resolve()}")
    print(f"  Markdown report     : {(config.output_dir / 'report.md').resolve()}")
    print(f"  Excel workbook      : {(config.output_dir / 'github_contributions.xlsx').resolve()}")
    if config.make_charts:
        print(f"  Charts              : {config.charts_dir.resolve()}")
    print("=" * 60)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.user and args.all:
        parser.error("--user and --all are mutually exclusive; choose one.")

    _configure_event_loop()
    users = resolve_users(args)

    if not users and not args.regen:
        parser.error(
            "No users specified. Use --user LOGIN (repeatable) to choose which "
            "GitHub account(s) to report on, then set GITHUB_TOKEN_LOGIN (or "
            "GITHUB_TOKEN) in your environment or .env file.\n\n"
            "  Example: python github_report.py --user octocat"
        )

    try:
        if args.regen:
            # No token needed: the CSVs of a previous run are the data source.
            config = build_offline_config(
                selected_logins=users,
                output_dir=args.output,
                make_charts=not args.no_charts,
                log_level=args.log_level,
                exclude_own_repos=args.exclude_own_repos,
                exclude_owners=args.exclude_owner,
                since=args.since,
                until=args.until,
                timezone_name=args.timezone,
            )
        else:
            config = build_config(
                selected_logins=users,
                output_dir=args.output,
                concurrency=args.concurrency,
                scan_all_branches=not args.default_branch_only,
                skip_forks=args.skip_forks,
                collect_commits=not args.no_commits,
                collect_prs=not args.no_prs,
                make_charts=not args.no_charts,
                max_repos=args.max_repos,
                log_level=args.log_level,
                use_search_discovery=not args.no_search_discovery,
                enumerate_org_repos=not args.no_org_repos,
                extra_repos=args.repo,
                extra_orgs=args.org,
                author_emails=args.author_email or [],
                exclude_own_repos=args.exclude_own_repos,
                exclude_owners=args.exclude_owner,
                fetch_commit_stats=not args.no_commit_stats,
                use_graphql=not args.rest_api,
                since=args.since,
                until=args.until,
                timezone_name=args.timezone,
            )
    except ConfigError as exc:
        # Logging may not be configured yet; print plainly to stderr.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    config.output_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(config.log_level, config.log_file)
    log = get_logger("cli")
    log.info("github-contrib %s starting for: %s", __version__, ", ".join(users) or "(regen)")

    # Import here so a misconfiguration above fails fast without heavy imports.
    try:
        if args.regen:
            from github_contrib.offline import load_collected_from_csv
            from github_contrib.report import generate_outputs

            data = load_collected_from_csv(config.output_dir)
            stats = generate_outputs(data, config, export_source_csvs=False)
        else:
            from github_contrib.report import run

            stats = run(config)
    except KeyboardInterrupt:
        log.error("Interrupted by user.")
        return 130
    except Exception as exc:  # noqa: BLE001 - top-level guard for a clean exit code
        log.exception("Report generation failed: %s", exc)
        return 1

    pdf_path = None
    if args.pdf:
        from github_contrib.pdfexport import export_pdf

        pdf_path = export_pdf(config.output_dir / "report.html")

    _print_final_summary(stats, config)
    if args.pdf:
        location = pdf_path.resolve() if pdf_path else "FAILED - see log; print report.html to PDF manually"
        print(f"  PDF report          : {location}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
