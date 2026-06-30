"""High-level orchestration: collect -> compute -> export.

``collect`` is async (it talks to the network); ``generate_outputs`` is sync
(pandas / matplotlib).  ``run`` ties them together for the CLI.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Awaitable, Callable

import aiohttp

from .client import GitHubClient, GitHubError
from .commits import collect_commits_for_repo, enrich_commits_with_stats
from .config import AppConfig
from .discovery import (
    discover_org_repos,
    discover_organizations,
    discover_repositories,
    discover_via_search_commits,
    fetch_repo,
    merge_repositories,
)
from .exporters import export_csvs, export_excel, export_summary_report
from .htmlreport import export_reports
from .insights import compute_insights
from .logging_config import get_logger
from .models import (
    CollectedData,
    CommitRecord,
    OrgRecord,
    PullRequestRecord,
    RepoRecord,
)
from .organizations import aggregate_organizations
from .pull_requests import collect_prs_for_repo
from .statistics import Statistics, compute_statistics

log = get_logger("report")


def _tqdm():
    """Return tqdm's async helper, or a no-op fallback if tqdm is absent."""
    try:
        from tqdm.asyncio import tqdm as atqdm  # type: ignore import-not-found

        return atqdm
    except Exception:  # pragma: no cover - tqdm is a hard dependency in practice
        return None


async def _guard(coro: Awaitable[list], label: str) -> list:
    """Run ``coro`` returning [] (and logging) on any unexpected failure."""
    try:
        return await coro
    except Exception as exc:  # noqa: BLE001 - we never want one repo to abort the run
        log.warning("Collection failed for %s: %s", label, exc)
        return []


async def _gather_with_progress(
    coros: list[Awaitable[list]], desc: str
) -> list[list]:
    """Await many coroutines, showing a progress bar when tqdm is available."""
    if not coros:
        return []
    atqdm = _tqdm()
    if atqdm is not None:
        return await atqdm.gather(*coros, desc=desc, unit="repo")
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


def _warn_about_token(client: GitHubClient, account, scopes: str | None) -> None:
    """Emit actionable warnings when a token cannot reach private/org data."""
    kind = client.token_kind
    if kind == "fine-grained":
        log.warning(
            "[%s] %s is a FINE-GRAINED token. It can only read repositories owned "
            "by you (or a single organization it was explicitly granted) and only "
            "those selected for the token. Private repos owned by OTHER users or "
            "organizations (e.g. work repos) will be INVISIBLE. For full coverage "
            "use a CLASSIC token with the 'repo' and 'read:org' scopes.",
            account.login,
            account.token_env,
        )
    elif kind == "classic":
        granted = {s.strip() for s in (scopes or "").split(",") if s.strip()}
        if "repo" not in granted:
            log.warning(
                "[%s] classic token %s lacks the 'repo' scope; private and "
                "collaborator repositories will be missing.",
                account.login,
                account.token_env,
            )
        if "read:org" not in granted and "admin:org" not in granted:
            log.warning(
                "[%s] classic token %s lacks 'read:org'; organization membership "
                "and some org repositories may be missing.",
                account.login,
                account.token_env,
            )


async def _augment_with_search(
    client: GitHubClient,
    account,
    config: AppConfig,
    repos: dict[str, RepoRecord],
) -> None:
    """Add repos discovered via the commit Search API to ``repos``."""
    for login in config.target_logins:
        for full in await discover_via_search_commits(client, login):
            existing = repos.get(full)
            if existing is not None:
                existing.discovered_via.add(account.login)
                continue
            record = await fetch_repo(client, full, account.login)
            if record is not None:
                merge_repositories(repos, [record])
                log.info("[%s] search discovered new repo %s", account.login, full)


async def _add_manual_includes(
    clients: dict[str, GitHubClient],
    config: AppConfig,
    repos: dict[str, RepoRecord],
    member_orgs: dict[str, OrgRecord],
) -> None:
    """Force-include repos/orgs the user named explicitly (--repo / --org)."""
    for org_login in config.extra_orgs:
        member_orgs.setdefault(org_login, OrgRecord(login=org_login, is_member=False))
        for login, client in clients.items():
            merge_repositories(repos, await discover_org_repos(client, org_login, login))

    for full in config.extra_repos:
        if full in repos:
            continue
        for login, client in clients.items():
            record = await fetch_repo(client, full, login)
            if record is not None:
                merge_repositories(repos, [record])
                log.info("included requested repo %s (via %s)", full, login)
                break
        else:
            log.warning("Could not access requested repo '%s' with any token.", full)


async def collect(config: AppConfig) -> CollectedData:
    """Discover repos/orgs and collect commits & PRs for the tracked users."""
    semaphore = asyncio.Semaphore(config.concurrency)

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
                log.warning(
                    "Token %s authenticates as '%s' but is configured for '%s'. "
                    "Proceeding with the configured target login.",
                    account.token_env,
                    actual,
                    account.login,
                )
            _warn_about_token(client, account, scopes)

            log.info("[%s] authenticated, discovering repositories…", account.login)
            # One account's discovery failure must never abort the whole run
            # (mirrors the per-repo _guard used during collection). The token
            # was already validated above, so a failure here is transient.
            try:
                merge_repositories(
                    repos,
                    await discover_repositories(client, account, max_repos=config.max_repos),
                )

                # Organizations + (optionally) every repo inside them.
                orgs = await discover_organizations(client, account)
                for org in orgs:
                    member_orgs.setdefault(org.login, org)
                if config.enumerate_org_repos:
                    for org in orgs:
                        merge_repositories(
                            repos, await discover_org_repos(client, org.login, account.login)
                        )

                # Search-based discovery: repos with commits by any tracked user
                # that the plain repo listing may not surface.
                if config.use_search_discovery:
                    await _augment_with_search(client, account, config, repos)
            except Exception as exc:  # noqa: BLE001 - skip this account, keep the rest
                log.warning(
                    "[%s] discovery did not complete (%s); continuing with what was found.",
                    account.login,
                    exc,
                )

        # --- manual includes (escape hatch) -------------------------------
        await _add_manual_includes(clients, config, repos, member_orgs)

        repo_list = list(repos.values())
        scan_repos = [r for r in repo_list if not (config.skip_forks and r.is_fork)]
        if config.max_repos is not None:
            scan_repos = scan_repos[: config.max_repos]
        if config.skip_forks:
            skipped = len(repo_list) - len(scan_repos)
            if skipped > 0:
                log.info("Skipping %d fork(s) for commit/PR scanning.", skipped)

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
                    ),
                    repo.full_name,
                )
                for repo in scan_repos
            ]
            for batch in await _gather_with_progress(coros, "Commits"):
                commits.extend(batch)
        commits = _dedupe_commits(commits)
        log.info("collected %d commit(s)", len(commits))

        # --- line-level stats (one request per commit) --------------------
        if config.fetch_commit_stats and commits:
            # Build a full_name → client map so each request uses the token
            # that already has read access to that repo.
            repo_client: dict[str, GitHubClient] = {
                repo.full_name: client_for(repo) for repo in repo_list
            }
            log.info(
                "Fetching line stats for %d commit(s) (%d extra API request(s)) — "
                "use --no-commit-stats to skip.",
                len(commits), len(commits),
            )
            enrich_coros = [
                _guard(
                    enrich_commits_with_stats(repo_client, [c]),
                    c.sha[:7],
                )
                for c in commits
            ]
            await _gather_with_progress(enrich_coros, "Line stats")

        # --- pull requests ------------------------------------------------
        prs: list[PullRequestRecord] = []
        if config.collect_prs and scan_repos:
            coros = [
                _guard(
                    collect_prs_for_repo(client_for(repo), repo, config.target_logins),
                    repo.full_name,
                )
                for repo in scan_repos
            ]
            for batch in await _gather_with_progress(coros, "Pull requests"):
                prs.extend(batch)
        prs = _dedupe_prs(prs)
        log.info("collected %d pull request(s)", len(prs))

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

    return CollectedData(
        repos=repo_list,
        commits=commits,
        pull_requests=prs,
        organizations=organizations,
    )


def _make_client_selector(
    clients: dict[str, GitHubClient],
) -> Callable[[RepoRecord], GitHubClient]:
    fallback = next(iter(clients.values()))

    def selector(repo: RepoRecord) -> GitHubClient:
        for login in repo.discovered_via:
            client = clients.get(login)
            if client is not None:
                return client
        return fallback

    return selector


def generate_outputs(data: CollectedData, config: AppConfig) -> Statistics:
    """Compute statistics & insights and write every output file."""
    import pandas as pd

    config.output_dir.mkdir(parents=True, exist_ok=True)
    stats = compute_statistics(data)
    insights = compute_insights(data)

    # Fold the activity insights into the headline summary so they also appear
    # in contribution_summary.csv, the Summary sheet and summary_report.txt.
    extra = insights.to_summary_dict()
    stats.summary_dict.update(extra)
    stats.summary = pd.concat(
        [stats.summary, pd.DataFrame([{"metric": k, "value": v} for k, v in extra.items()])],
        ignore_index=True,
    )

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
        )
    except Exception as exc:  # noqa: BLE001 - reports are non-fatal
        log.warning("Report generation failed (continuing): %s", exc)

    return stats


def run(config: AppConfig) -> Statistics:
    """Synchronous entry point: collect then generate outputs."""
    data = asyncio.run(collect(config))
    return generate_outputs(data, config)
