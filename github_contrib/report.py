"""High-level orchestration: collect -> compute -> export.

``collect`` is async (it talks to the network); ``generate_outputs`` is sync
(pandas / matplotlib).  ``run`` ties them together for the CLI.

Collection order:

1. validate every token, list the repositories each can reach (personal,
   collaborator, organization) and any ``--repo`` / ``--org`` includes;
2. search for repositories holding commits by the tracked logins and author
   emails, and for pull requests they opened (upstream projects);
3. commits on every branch (default branch only for upstream projects);
4. pull requests — listed in affiliated repositories, completed from search
   results elsewhere — then, when every branch is scanned, the commits of each
   pull request, which recovers work on deleted branches;
5. line statistics for the commits inside the reporting period.

Anything that fails along the way becomes a data-completeness note in the
report instead of silently missing data.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import aiohttp

from .client import GitHubClient, GitHubError
from .commits import collect_commits_for_repo, collect_pr_commits, enrich_commits_with_stats
from .config import AppConfig
from .discovery import (
    SEARCH_END,
    SEARCH_START,
    discover_org_repos,
    discover_organizations,
    discover_repos_via_search,
    discover_repositories,
    fetch_repo,
    issue_repo_full_name,
    merge_repositories,
    search_pull_requests,
)
from .exporters import export_csvs, export_excel, export_summary_report
from .filters import ReportScope, prepare_report_data
from .htmlreport import export_reports
from .insights import compute_insights
from .logging_config import get_logger
from .models import (
    CollectedData,
    CommitRecord,
    Coverage,
    OrgRecord,
    PullRequestRecord,
    RepoRecord,
    parse_github_datetime,
)
from .organizations import aggregate_organizations
from .pull_requests import collect_pr_from_search, collect_prs_for_repo
from .statistics import Statistics, compute_statistics

log = get_logger("report")

#: Collection metadata and completeness notes, saved next to the CSVs.
COLLECTION_FILE = "collection.json"


def _tqdm():
    """Return tqdm's async helper, or a no-op fallback if tqdm is absent."""
    try:
        from tqdm.asyncio import tqdm as atqdm  # type: ignore import-not-found

        return atqdm
    except Exception:  # pragma: no cover - tqdm is a hard dependency in practice
        return None


async def _guard(coro: Awaitable[list], label: str, coverage: Coverage) -> list:
    """Run ``coro``; an unexpected failure becomes a completeness note, never a crash."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - one repo must never abort the run
        coverage.warn(f"Collection failed for {label}: {exc}")
        return []


async def _as_list(coro: Awaitable[Any]) -> list:
    return [await coro]


async def _gather_with_progress(
    coros: list[Awaitable[Any]], desc: str
) -> list[Any]:
    """Await many coroutines, showing a progress bar when tqdm is available."""
    if not coros:
        return []
    atqdm = _tqdm()
    if atqdm is not None:
        return await atqdm.gather(*coros, desc=desc, unit="item")
    return await asyncio.gather(*coros)


def _dedupe_commits(commits: list[CommitRecord]) -> list[CommitRecord]:
    seen: set[tuple[str, str]] = set()
    out: list[CommitRecord] = []
    for c in commits:
        key = (c.full_name, c.sha)
        if c.sha and key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _dedupe_prs(prs: list[PullRequestRecord]) -> list[PullRequestRecord]:
    seen: set[tuple[str, int]] = set()
    out: list[PullRequestRecord] = []
    for p in prs:
        key = (p.full_name, p.number)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _warn_about_token(client: GitHubClient, account, scopes: str | None, coverage: Coverage) -> None:
    """Record actionable notes when a token cannot reach private/org data."""
    kind = client.token_kind
    if kind == "fine-grained":
        coverage.warn(
            f"[{account.login}] {account.token_env} is a fine-grained token: it only reads "
            "repositories owned by the account (or one organization it was granted), so "
            "private repositories owned by other users or organizations are invisible. "
            "Use a classic token with the 'repo' and 'read:org' scopes for full coverage."
        )
    elif kind == "classic":
        granted = {s.strip() for s in (scopes or "").split(",") if s.strip()}
        if "repo" not in granted:
            coverage.warn(
                f"[{account.login}] classic token {account.token_env} lacks the 'repo' "
                "scope; private and collaborator repositories are missing."
            )
        if "read:org" not in granted and "admin:org" not in granted:
            coverage.warn(
                f"[{account.login}] classic token {account.token_env} lacks 'read:org'; "
                "organization membership and some organization repositories may be missing."
            )


def _search_window(config: AppConfig) -> tuple[datetime, datetime]:
    """The author-date window searched for commits (the reporting period)."""
    start = config.since.astimezone(timezone.utc) if config.since else SEARCH_START
    end = config.until.astimezone(timezone.utc) if config.until else SEARCH_END
    return start, end


def _in_period(moment: datetime | None, config: AppConfig) -> bool:
    if config.since is None and config.until is None:
        return True
    return (
        moment is not None
        and (config.since is None or moment >= config.since)
        and (config.until is None or moment <= config.until)
    )


async def _add_found_repos(
    client: GitHubClient,
    account_login: str,
    names: set[str],
    repos: dict[str, RepoRecord],
    coverage: Coverage,
) -> None:
    """Add repositories found by search (upstream projects) to ``repos``."""
    new: list[str] = []
    for full in sorted(names):
        existing = repos.get(full)
        if existing is not None:
            existing.discovered_via.add(account_login)
        else:
            new.append(full)
    records = await asyncio.gather(*(fetch_repo(client, full, account_login) for full in new))
    for full, record in zip(new, records):
        if record is None:
            coverage.warn(f"{full}: found by search, but its details could not be read; not scanned.")
            continue
        merge_repositories(repos, [record])
        log.info("[%s] search discovered new repo %s", account_login, full)


async def _add_manual_includes(
    clients: dict[str, GitHubClient],
    config: AppConfig,
    repos: dict[str, RepoRecord],
    member_orgs: dict[str, OrgRecord],
    coverage: Coverage,
) -> None:
    """Force-include repos/orgs the user named explicitly (--repo / --org)."""
    for org_login in config.extra_orgs:
        member_orgs.setdefault(org_login, OrgRecord(login=org_login, is_member=False))
        for login, client in clients.items():
            merge_repositories(repos, await discover_org_repos(client, org_login, login, coverage))

    for full in config.extra_repos:
        if full in repos:
            repos[full].affiliated = True
            continue
        for login, client in clients.items():
            record = await fetch_repo(client, full, login, affiliated=True)
            if record is not None:
                merge_repositories(repos, [record])
                log.info("included requested repo %s (via %s)", full, login)
                break
        else:
            coverage.warn(f"Requested repository {full} could not be read with any token.")


async def collect(config: AppConfig) -> CollectedData:
    """Discover repos/orgs and collect commits & PRs for the tracked users."""
    coverage = Coverage()
    semaphore = asyncio.Semaphore(config.concurrency)
    started = datetime.now(timezone.utc)
    start, end = _search_window(config)

    async with AsyncExitStack() as stack:
        clients: dict[str, GitHubClient] = {}
        for account in config.accounts:
            session = await stack.enter_async_context(
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=config.request_timeout),
                )
            )
            clients[account.login] = GitHubClient(
                token=account.token,
                session=session,
                semaphore=semaphore,
                login=account.login,
                max_retries=config.max_retries,
                per_page=config.per_page,
                request_timeout=config.request_timeout,
                coverage=coverage,
            )

        # --- validate tokens + discover repositories/orgs -----------------
        repos: dict[str, RepoRecord] = {}
        member_orgs: dict[str, OrgRecord] = {}
        for account in config.accounts:
            client = clients[account.login]
            try:
                actual, scopes = await client.get_viewer()
            except GitHubError as exc:
                log.error("Token %s is invalid: %s", account.token_env, exc)
                raise
            if actual and actual.lower() != account.login.lower():
                coverage.warn(
                    f"Token {account.token_env} belongs to '{actual}', not '{account.login}'. "
                    f"Only what '{actual}' can access was scanned for {account.login}'s work."
                )
            _warn_about_token(client, account, scopes, coverage)

            log.info("[%s] authenticated, discovering repositories…", account.login)
            # One account's discovery failure must never abort the whole run.
            try:
                merge_repositories(
                    repos,
                    await discover_repositories(client, account, max_repos=config.max_repos),
                )
                orgs = await discover_organizations(client, account)
                for org in orgs:
                    member_orgs.setdefault(org.login, org)
                if config.enumerate_org_repos:
                    for org in orgs:
                        merge_repositories(
                            repos,
                            await discover_org_repos(client, org.login, account.login, coverage),
                        )
            except Exception as exc:  # noqa: BLE001 - keep what was found
                coverage.warn(
                    f"[{account.login}] repository discovery did not complete ({exc}); "
                    "repositories it would have listed may be missing."
                )

        await _add_manual_includes(clients, config, repos, member_orgs, coverage)

        # --- search: upstream projects with commits or pull requests ---------
        pr_hits: dict[tuple[str, int], tuple[str, dict]] = {}
        if config.use_search_discovery:
            for account in config.accounts:
                client = clients[account.login]
                names = await discover_repos_via_search(
                    client, config.target_logins, config.author_emails, start, end, coverage
                )
                if config.collect_prs:
                    # Pull requests opened before the period can still hold
                    # commits authored inside it, so only the end is bounded.
                    for login in config.target_logins:
                        for item in await search_pull_requests(client, login, SEARCH_START, end, coverage):
                            full = issue_repo_full_name(item)
                            if full and item.get("number"):
                                names.add(full)
                                pr_hits.setdefault((full, int(item["number"])), (account.login, item))
                await _add_found_repos(client, account.login, names, repos, coverage)

        repo_list = list(repos.values())
        scan_repos = [r for r in repo_list if not (config.skip_forks and r.is_fork)]
        if config.max_repos is not None:
            scan_repos = scan_repos[: config.max_repos]
        if config.skip_forks:
            skipped = len(repo_list) - len(scan_repos)
            if skipped > 0:
                log.info("Skipping %d fork(s) for commit/PR scanning.", skipped)
        scan_by_name = {r.full_name: r for r in scan_repos}

        client_for = _make_client_selector(clients)

        # --- commits ------------------------------------------------------
        commits: list[CommitRecord] = []
        if config.collect_commits and scan_repos:
            coros = [
                _guard(
                    collect_commits_for_repo(
                        client_for(repo),
                        repo,
                        config.target_logins,
                        scan_all_branches=config.scan_all_branches,
                        author_emails=config.author_emails or None,
                        coverage=coverage,
                    ),
                    repo.full_name,
                    coverage,
                )
                for repo in scan_repos
            ]
            for batch in await _gather_with_progress(coros, "Commits"):
                commits.extend(batch)
        commits = _dedupe_commits(commits)
        log.info("collected %d commit(s)", len(commits))

        # --- pull requests ------------------------------------------------
        prs: list[PullRequestRecord] = []
        if config.collect_prs and scan_repos:
            listed = [r for r in scan_repos if r.affiliated]
            listed_names = {r.full_name for r in listed}
            coros: list[Awaitable[list]] = [
                _guard(
                    collect_prs_for_repo(client_for(repo), repo, config.target_logins, coverage),
                    repo.full_name,
                    coverage,
                )
                for repo in listed
            ]
            # Upstream projects: complete the pull requests search found.
            for (full, number), (login, item) in sorted(pr_hits.items()):
                repo = scan_by_name.get(full)
                if repo is None or full in listed_names:
                    continue
                client = clients.get(login) or client_for(repo)
                coros.append(
                    _guard(
                        _as_list(collect_pr_from_search(client, repo, item, coverage)),
                        f"{full}#{number}",
                        coverage,
                    )
                )
            for batch in await _gather_with_progress(coros, "Pull requests"):
                prs.extend(batch)
        prs = _dedupe_prs(prs)
        log.info("collected %d pull request(s)", len(prs))

        # --- commits that live only in pull requests ----------------------
        if config.collect_commits and config.scan_all_branches and prs:
            present = {(c.full_name, c.sha) for c in commits}
            # A pull request last updated before the period cannot hold
            # commits authored inside it.
            wanted = [
                p for p in prs
                if p.full_name in scan_by_name
                and (config.since is None or (p.updated_at or p.created_at or SEARCH_END) >= config.since)
            ]

            async def recover(pr: PullRequestRecord) -> list[CommitRecord]:
                repo = scan_by_name[pr.full_name]
                shas, records = await collect_pr_commits(
                    client_for(repo), repo, pr, config.target_logins, config.author_emails, coverage
                )
                pr.commit_shas = shas
                return records

            recovered = 0
            batches = await _gather_with_progress(
                [_guard(recover(p), f"{p.full_name}#{p.number} commits", coverage) for p in wanted],
                "PR commits",
            )
            for batch in batches:
                for commit in batch:
                    key = (commit.full_name, commit.sha)
                    if key not in present:
                        present.add(key)
                        commits.append(commit)
                        recovered += 1
            if recovered:
                log.info("recovered %d commit(s) found only in pull requests", recovered)

        # --- line-level stats (one request per commit in the period) ------
        if config.fetch_commit_stats and commits:
            in_period = [c for c in commits if _in_period(c.authored_date, config)]
            repo_client: dict[str, GitHubClient] = {
                repo.full_name: client_for(repo) for repo in repo_list
            }
            wanted_shas = len({c.sha for c in in_period if not c.is_merge})
            log.info(
                "Fetching line stats for %d commit(s) (%d extra API request(s)) — "
                "use --no-commit-stats to skip.",
                wanted_shas, wanted_shas,
            )
            await enrich_commits_with_stats(
                repo_client,
                in_period,
                coverage,
                gather=lambda coros: _gather_with_progress(coros, "Line stats"),
            )

        organizations = aggregate_organizations(
            repo_list, commits, prs, list(member_orgs.values())
        )

        total_requests = sum(c.request_count for c in clients.values())
        total_waits = sum(c.rate_limit_waits for c in clients.values())
        log.info(
            "Finished collection: %d API requests, %d rate-limit wait(s).",
            total_requests,
            total_waits,
        )

    meta = {
        "collected_at": started.isoformat(timespec="seconds"),
        "target_logins": list(config.target_logins),
        "author_emails": list(config.author_emails),
        "scan_all_branches": config.scan_all_branches,
        "skip_forks": config.skip_forks,
        "collect_prs": config.collect_prs,
        "fetch_commit_stats": config.fetch_commit_stats,
        "search_discovery": config.use_search_discovery,
        "since": config.since.isoformat() if config.since else None,
        "until": config.until.isoformat() if config.until else None,
        "timezone": config.timezone_name,
        "api_requests": total_requests,
    }
    return CollectedData(
        repos=repo_list,
        commits=commits,
        pull_requests=prs,
        organizations=organizations,
        notes=list(coverage.notes),
        meta=meta,
    )


def _make_client_selector(
    clients: dict[str, GitHubClient],
) -> Callable[[RepoRecord], GitHubClient]:
    fallback = next(iter(clients.values()))

    def selector(repo: RepoRecord) -> GitHubClient:
        for login in sorted(repo.discovered_via):
            client = clients.get(login)
            if client is not None:
                return client
        return fallback

    return selector


def write_collection_file(output_dir: Path, data: CollectedData) -> Path:
    """Save collection metadata + completeness notes for later ``--regen`` runs."""
    path = output_dir / COLLECTION_FILE
    payload = {"meta": data.meta, "notes": data.notes}
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def _period_notes(data: CollectedData, config: AppConfig) -> list[str]:
    """Warn when a ``--regen`` period reaches beyond what was collected."""
    notes: list[str] = []
    collected_since = parse_github_datetime(data.meta.get("since"))
    collected_until = parse_github_datetime(data.meta.get("until"))
    if collected_since and (config.since is None or config.since < collected_since):
        notes.append(
            f"The data was collected from {data.meta['since'][:10]} only; earlier work "
            "is not in this report. Collect again to cover the full period."
        )
    if collected_until and (config.until is None or config.until > collected_until):
        notes.append(
            f"The data was collected up to {data.meta['until'][:10]} only; later work "
            "is not in this report. Collect again to cover the full period."
        )
    return notes


def _period_label(scope: ReportScope) -> str:
    since = scope.since.date().isoformat() if scope.since else ""
    until = scope.until.date().isoformat() if scope.until else ""
    if since and until:
        return f"{since} to {until}"
    if since:
        return f"from {since}"
    if until:
        return f"until {until}"
    return "all time"


def _scope_summary(scope: ReportScope) -> dict[str, object]:
    return {
        "report_period": _period_label(scope),
        "report_timezone": scope.timezone,
        "merge_commits": scope.merge_commits,
        "duplicate_commits_counted_once": scope.duplicates.total,
        "data_completeness_warnings": len(scope.notes),
    }


def generate_outputs(
    data: CollectedData,
    config: AppConfig,
    *,
    export_source_csvs: bool = True,
) -> Statistics:
    """Compute statistics & insights and write every output file.

    ``export_source_csvs=False`` skips rewriting the raw CSVs — used by
    ``--regen``, where the CSVs *are* the input and rewriting them (possibly
    filtered) would destroy the only cached copy of the collected data.
    """
    import pandas as pd

    config.output_dir.mkdir(parents=True, exist_ok=True)
    if export_source_csvs:
        write_collection_file(config.output_dir, data)
    data.notes = list(data.notes) + _period_notes(data, config)

    # Exclusions, period, de-duplication and time zone — before any statistic
    # is computed, so every output tells the same story.
    data, scope = prepare_report_data(data, config)
    if any(scope.excluded.values()):
        log.info(
            "Report excludes %d repo(s), %d commit(s), %d PR(s) owned by: %s.",
            scope.excluded["repos"], scope.excluded["commits"], scope.excluded["pull_requests"],
            ", ".join(scope.excluded_owners),
        )
    if scope.has_period:
        log.info(
            "Report period %s (%s): %d commit(s) and %d PR(s) outside it left out.",
            _period_label(scope), scope.timezone,
            scope.outside_period.get("commits", 0), scope.outside_period.get("pull_requests", 0),
        )

    stats = compute_statistics(data)
    insights = compute_insights(data)

    # Fold the activity insights and the report scope into the headline
    # summary so they also appear in contribution_summary.csv, the Summary
    # sheet and summary_report.txt.
    extra = {**insights.to_summary_dict(), **_scope_summary(scope)}
    stats.summary_dict.update(extra)
    stats.summary = pd.concat(
        [stats.summary, pd.DataFrame([{"metric": k, "value": v} for k, v in extra.items()])],
        ignore_index=True,
    )

    if export_source_csvs:
        export_csvs(config.output_dir, stats)
    export_excel(config.output_dir, stats)
    export_summary_report(config.output_dir, stats)

    if config.make_charts:
        try:
            from .charts import generate_all_charts

            generate_all_charts(stats, config.charts_dir)
        except Exception as exc:  # noqa: BLE001 - charts are a bonus, never fatal
            log.warning("Chart generation failed (continuing): %s", exc)

    # Readable Markdown + HTML reports (embed charts if they were generated).
    try:
        export_reports(
            config.output_dir,
            stats,
            insights,
            stats.summary_dict,
            config.charts_dir,
            include_charts=config.make_charts,
            scope=scope,
        )
    except Exception as exc:  # noqa: BLE001 - reports are non-fatal
        log.warning("Report generation failed (continuing): %s", exc)

    return stats


def run(config: AppConfig) -> Statistics:
    """Synchronous entry point: collect then generate outputs."""
    data = asyncio.run(collect(config))
    return generate_outputs(data, config)
